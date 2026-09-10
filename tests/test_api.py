"""lead 集成测试 — 真实内核接线 / 12 方法执行器全命中 / calibrate 默认曲线注入 / 工件列表端点.

与 test_api_core.py(假内核全生命周期)互补:
  - 这里优先走**真实** resolve_executor 与真实快速内核(densification /
    material-query 不依赖 MAPDL,秒级完成),验证 kernels ↔ api ↔ queue 接线;
  - calibrate 的默认曲线注入是合并层的显式回归(设计如此,见 settings._cycle_section);
  - GET /jobs/{id}/artifacts 集合端点(与 JobState.artifacts_url 对应)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ansys_hip import registry
from ansys_hip.api import ApiError, _resolve_params, create_app
from ansys_hip.settings import Settings, load_parts

# 复用 test_api_core 的测试工具(pytest 会把 tests/ 加入 sys.path)
from test_api_core import (
    HOT_SHORT_PART,
    read_resolved_params,
    submit,
    wait_for_terminal,
    write_part_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 注册表 ↔ 内核契约:12 个方法全部能解析到真实执行器
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(registry.REGISTRY))
def test_every_method_resolves_real_executor(name: str) -> None:
    """不 monkeypatch:真实懒加载 kernels.<mod>.run_<method>,阶段 1 无 501 方法。"""
    executor = registry.resolve_executor(name)
    assert callable(executor), f"方法 {name} 未解析到执行器"
    assert executor.__name__ == "run_" + name.replace("-", "_")


def test_registry_covers_exactly_twelve_methods() -> None:
    assert len(registry.REGISTRY) == 12


def test_full3d_hip_phase1_tag_is_solid45() -> None:
    """阶段 1 一阶 SOLID45(冒烟);标签写错会误导 HIPForm 侧选型,锁死。"""
    tags = registry.REGISTRY["full3d-hip"].tags
    assert "SOLID45" in tags
    assert "SOLID187" not in tags


# ---------------------------------------------------------------------------
# calibrate:默认工艺曲线注入(合并层回归)
# ---------------------------------------------------------------------------

def test_calibrate_receives_service_default_cycle(
    client: TestClient, fake_executors, settings: Settings
) -> None:
    """CalibrateParams 有顶层 cycle 字段 → 无零件配置时注入 defaults.cycle(900℃/120MPa/3h 全曲线)。"""
    calls: list[Any] = []

    def run_calibrate(params: Any, ctx: Any) -> dict[str, Any]:
        calls.append(params)
        return {"fidelity": "real", "residual_rms": 0.0}

    fake_executors["calibrate"] = run_calibrate
    accepted = submit(
        client,
        "calibrate",
        {
            "params": {
                "experimental": [
                    {"time_s": 0, "relative_density": 0.65},
                    {"time_s": 5400, "relative_density": 0.90},
                    {"time_s": 14400, "relative_density": 0.99},
                ]
            }
        },
    )
    state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state

    resolved = json.loads(
        (settings.jobs_root / accepted["id"] / "resolved-params.json").read_text(
            encoding="utf-8"
        )
    )
    assert resolved["cycle"]["points"] == settings.defaults.cycle["points"]
    assert calls and calls[0].cycle is not None


def test_calibrate_inline_cycle_overrides_default(
    client: TestClient, fake_executors, settings: Settings
) -> None:
    """内联 cycle 深合并:points 列表整体替换(不逐点合并)。"""
    fake_executors["calibrate"] = lambda params, ctx: {"fidelity": "real"}
    inline_points = [
        {"time_s": 0, "temperature_c": 20, "pressure_mpa": 0},
        {"time_s": 7200, "temperature_c": 920, "pressure_mpa": 100},
    ]
    accepted = submit(
        client,
        "calibrate",
        {
            "params": {
                "experimental": [
                    {"time_s": 0, "relative_density": 0.65},
                    {"time_s": 3600, "relative_density": 0.9},
                    {"time_s": 7200, "relative_density": 0.95},
                ],
                "cycle": {"points": inline_points},
            }
        },
    )
    state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state

    resolved = json.loads(
        (settings.jobs_root / accepted["id"] / "resolved-params.json").read_text(
            encoding="utf-8"
        )
    )
    assert resolved["cycle"]["points"] == inline_points


# ---------------------------------------------------------------------------
# GET /jobs/{id}/artifacts 集合端点
# ---------------------------------------------------------------------------

def test_artifacts_listing_endpoint(
    client: TestClient, fake_executors, settings: Settings
) -> None:
    """artifacts_url 指向集合端点;返回按名排序的工件文件列表。"""

    def kernel_with_artifacts(params: Any, ctx: Any) -> dict[str, Any]:
        for artifact_name in ("curve.csv", "summary.txt"):
            (ctx.job_dir / "artifacts" / artifact_name).write_text(
                "data", encoding="utf-8"
            )
        return {"fidelity": "real", "artifacts": ["curve.csv", "summary.txt"]}

    fake_executors["densification"] = kernel_with_artifacts
    accepted = submit(client, "densification", {})
    state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state
    assert state["artifacts_url"] == f"/jobs/{accepted['id']}/artifacts"

    listing = client.get(state["artifacts_url"])
    assert listing.status_code == 200
    assert listing.json() == ["curve.csv", "summary.txt"]  # 按名排序

    # 无工件作业 → 空列表(而非 404)
    fake_executors["material-query"] = lambda params, ctx: {"fidelity": "real"}
    bare = submit(client, "material-query", {})
    bare_state = wait_for_terminal(client, bare["id"])
    assert bare_state["status"] == "succeeded", bare_state
    assert client.get(f"/jobs/{bare['id']}/artifacts").json() == []


def test_artifacts_listing_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/jobs/nonexistent/artifacts")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# 真实快速内核端到端(不依赖 MAPDL)
# ---------------------------------------------------------------------------

def test_real_densification_end_to_end_meets_spec(client: TestClient) -> None:
    """空参数全继承默认(900℃/120MPa/3h)→ 0.65 → ≥0.97,验证 kernels↔queue↔api 接线。"""
    accepted = submit(client, "densification", {})
    state = wait_for_terminal(client, accepted["id"], timeout_s=60.0)
    assert state["status"] == "succeeded", state

    result = client.get(f"/jobs/{accepted['id']}/result").json()
    assert result["fidelity"] == "real"
    assert result["densities"][0] == pytest.approx(0.65)
    assert result["final_density"] >= 0.97
    assert result["reached_097"] is True
    assert len(result["times_s"]) == len(result["densities"])
    assert result["times_s"] == sorted(result["times_s"])


def test_real_material_query_end_to_end(client: TestClient) -> None:
    accepted = submit(
        client,
        "material-query",
        {"params": {"material": "20steel", "temperatures_c": [20, 900]}},
    )
    state = wait_for_terminal(client, accepted["id"], timeout_s=60.0)
    assert state["status"] == "succeeded", state

    result = client.get(f"/jobs/{accepted['id']}/result").json()
    assert result["material"] == "20steel"
    assert [row["temperature_c"] for row in result["rows"]] == [20, 900]
    for row in result["rows"]:
        assert "young_modulus_gpa" in row["properties"]
    assert result["source_notes"]


def test_real_compensate_end_to_end_in_worker_thread(
    client: TestClient, tmp_path: Path
) -> None:
    """回归:gmsh.initialize 默认注册 SIGINT 处理器,仅主线程合法 —
    服务内核经 asyncio.to_thread 在工作线程执行,必须 interruptible=False。"""
    import gmsh

    gmsh.initialize()
    try:
        gmsh.model.occ.addBox(0, 0, 0, 10, 20, 30)
        gmsh.model.occ.synchronize()
        gmsh.write(str(tmp_path / "cavity.step"))
    finally:
        gmsh.finalize()

    accepted = submit(
        client,
        "compensate",
        {"params": {"geometry": {"cavity_step": str(tmp_path / "cavity.step")}}},
    )
    state = wait_for_terminal(client, accepted["id"], timeout_s=120.0)
    assert state["status"] == "succeeded", state

    result = client.get(f"/jobs/{accepted['id']}/result").json()
    assert result["fidelity"] == "real"
    assert "scale_factors" in result
    assert result["artifacts"], "compensate 应产出补偿后 STEP 工件"
    artifact_name = result["artifacts"][0] if isinstance(result["artifacts"][0], str) \
        else result["artifacts"][0]["name"]
    download = client.get(f"/jobs/{accepted['id']}/artifacts/{artifact_name}")
    assert download.status_code == 200
    assert b"MANIFOLD_SOLID_BREP" in download.content[:4000]


# ---------------------------------------------------------------------------
# 类型化提交路由(REGISTRY 生成 × 12)— openapi 形态 / 管线等价 / 合并语义
# ---------------------------------------------------------------------------

TYPED_METHODS = sorted(registry.REGISTRY)


def _wrapper_model_name(method: str) -> str:
    """独立复算包装模型名(api._camel_case 的镜像,防实现与断言同源)。"""
    return "".join(part.capitalize() for part in method.split("-")) + "SimRequest"


@pytest.mark.parametrize("name", TYPED_METHODS)
def test_typed_route_present_in_openapi(client: TestClient, name: str) -> None:
    """每个方法一个 POST /sim/{name}:引用正确的包装/参数模型,带真实示例与分组标签。"""
    spec = client.app.openapi()
    operation = spec["paths"][f"/sim/{name}"]["post"]

    wrapper_name = _wrapper_model_name(name)
    schema_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert schema_ref == f"#/components/schemas/{wrapper_name}"

    params_model_name = registry.REGISTRY[name].params_model.__name__
    wrapper = spec["components"]["schemas"][wrapper_name]
    params_ref = wrapper["properties"]["params"]["anyOf"][0]["$ref"]
    assert params_ref == f"#/components/schemas/{params_model_name}"

    # 参数模型的字段级 description 真的进了 schema(Swagger 表单提示,R1)
    params_schema = spec["components"]["schemas"][params_model_name]
    described = [
        key for key, prop in params_schema.get("properties", {}).items()
        if "description" in prop
    ]
    assert described, f"{params_model_name} 字段缺少 description"

    examples = operation["requestBody"]["content"]["application/json"].get("examples")
    assert examples, f"{name} 缺请求示例"

    group = registry.REGISTRY[name].group
    assert operation["tags"] == [registry.GROUP_LABELS[group]]
    assert operation["summary"] == registry.REGISTRY[name].summary


def test_generic_submit_route_hidden_from_openapi(client: TestClient) -> None:
    """泛化 POST /sim/{method} 运行时保留(HIPForm 兼容)但不出现在 Swagger。"""
    spec = client.app.openapi()
    assert "/sim/{method}" not in spec["paths"]
    sim_posts = sorted(
        path for path, item in spec["paths"].items()
        if path.startswith("/sim/") and "post" in item
    )
    assert sim_posts == [f"/sim/{name}" for name in TYPED_METHODS]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("densification", {"part": "tc4-demo", "params": {"initial_relative_density": 0.68}}),
        ("material-query", {"params": {"material": "20steel", "temperatures_c": [20, 900]}}),
    ],
)
def test_typed_route_matches_resolve_params(
    client: TestClient,
    fake_executors,
    settings: Settings,
    name: str,
    payload: dict[str, Any],
) -> None:
    """类型化路由(exclude_unset dump)与合并层语义(原始 payload dict 直调
    _resolve_params)对同一 (part, 内联参数) 产出完全一致的合并结果。

    注:泛化路由对已知方法已不可达(类型化路由先注册截获),此处对照的是
    _resolve_params 直调路径 — 机制上即泛化处理器的请求处理体。"""
    calls: list[Any] = []

    def capture(params: Any, ctx: Any) -> dict[str, Any]:
        calls.append(params)
        return {"fidelity": "real"}

    fake_executors[name] = capture
    accepted = submit(client, name, payload)
    state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state

    part = payload.get("part")
    part_config = load_parts(settings.parts_dir).get(part) if part else None
    expected = _resolve_params(
        registry.REGISTRY[name], settings, part_config, payload["params"]
    )
    assert calls and calls[0] == expected
    assert read_resolved_params(settings, accepted["id"]) == expected.model_dump(mode="json")


def test_typed_route_unset_fields_inherit_part_config(
    settings_factory, fake_executors, tmp_path: Path
) -> None:
    """exclude_unset 语义(方案 §4.3):只给 nlgeom,零件的 mesh 4.0 / cycle /
    numerics 不被模型默认值(mesh_size_mm=6.0)压掉 — 防默认值覆盖配置回归。"""
    parts_dir = tmp_path / "parts-typed"
    write_part_config(parts_dir, "hot-short", HOT_SHORT_PART)
    fake_executors["axisym-hip"] = lambda params, ctx: {"fidelity": "smoke"}
    settings = settings_factory(parts_dir=parts_dir)

    with TestClient(create_app(settings)) as client:
        accepted = submit(
            client, "axisym-hip", {"part": "hot-short", "params": {"nlgeom": True}}
        )
        resolved = read_resolved_params(settings, accepted["id"])

    assert resolved["mesh"]["mesh_size_mm"] == 4.0
    assert resolved["numerics"]["time_step_s"] == 60
    assert [p["time_s"] for p in resolved["cycle"]["points"]] == [0, 7200]
    assert resolved["materials"]["powder"] == "tc4"
    assert resolved["nlgeom"] is True


def test_typed_route_nested_unset_keeps_sibling_from_part(
    settings_factory, fake_executors, tmp_path: Path
) -> None:
    """嵌套 exclude_unset:内联只给 geometry.capsule_step,不以 cavity_step=None
    顶掉零件配置的型腔路径(手拼 dict 做不到的增强)。"""
    part_config = {
        **HOT_SHORT_PART,
        "geometry": {
            "capsule_step": "/tmp/hot/capsule.step",
            "cavity_step": "/tmp/hot/cavity.step",
        },
    }
    parts_dir = tmp_path / "parts-nested"
    write_part_config(parts_dir, "hot-short", part_config)
    fake_executors["mesh"] = lambda params, ctx: {"fidelity": "real"}
    settings = settings_factory(parts_dir=parts_dir)

    with TestClient(create_app(settings)) as client:
        accepted = submit(
            client,
            "mesh",
            {"part": "hot-short", "params": {"geometry": {"capsule_step": "/tmp/hot/c2.step"}}},
        )
        resolved = read_resolved_params(settings, accepted["id"])

    assert resolved["geometry"]["capsule_step"] == "/tmp/hot/c2.step"   # 显式内联
    assert resolved["geometry"]["cavity_step"] == "/tmp/hot/cavity.step"  # 零件配置保留


def test_typed_route_full3d_part_only_submit(
    settings_factory, fake_executors, tmp_path: Path
) -> None:
    """params 可省略(方案 §4.2 修正点:可选而非必填):full3d-hip 的必填
    geometry 由零件配置补齐后通过校验,零件全继承提交形态可用。"""
    parts_dir = tmp_path / "parts-f3d"
    write_part_config(parts_dir, "hot-short", HOT_SHORT_PART)
    fake_executors["full3d-hip"] = lambda params, ctx: {"fidelity": "smoke"}
    settings = settings_factory(parts_dir=parts_dir)

    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "full3d-hip", {"part": "hot-short"})
        resolved = read_resolved_params(settings, accepted["id"])

    assert resolved["geometry"]["capsule_step"] == "/tmp/hot/capsule.step"


def test_typed_route_unknown_field_rejected(client: TestClient) -> None:
    """类型化路由传拼错字段 → pydantic extra=forbid → 400 INVALID_PARAMS
    (与泛化路由的手工 unknown-key 检查同码,不被静默吞掉)。"""
    response = client.post(
        "/sim/densification",
        json={"params": {"initial_relative_dens": 0.68}},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "initial_relative_dens" in body["message"]


def test_typed_route_unknown_part_returns_404(
    client: TestClient, fake_executors
) -> None:
    fake_executors["densification"] = lambda params, ctx: {"fidelity": "real"}
    response = client.post("/sim/densification", json={"part": "no-such-part"})
    assert response.status_code == 404
    assert response.json()["code"] == "PART_NOT_FOUND"


def test_typed_route_disabled_method_returns_503(
    settings_factory, fake_executors
) -> None:
    """下线检查先于参数校验(§6.3 顺序):空请求体也直接 503。"""
    fake_executors["mesh"] = lambda params, ctx: {"fidelity": "real"}
    settings = settings_factory(disabled=("mesh",))
    with TestClient(create_app(settings)) as client:
        response = client.post("/sim/mesh", json={})
    assert response.status_code == 503
    assert response.json()["code"] == "METHOD_DISABLED"


def test_typed_route_unimplemented_kernel_returns_501(
    client: TestClient, fake_executors
) -> None:
    """fake_executors 未注册的方法 → resolve_executor 为 None → 501。"""
    response = client.post(
        "/sim/axisym-hip",
        json={"params": {"profile": {"outer_radius_mm": 50, "height_mm": 150}}},
    )
    assert response.status_code == 501
    assert response.json()["code"] == "METHOD_NOT_IMPLEMENTED"


def test_fallback_route_unknown_method_returns_404(client: TestClient) -> None:
    """未知方法名落泛化兜底路由 → 404 METHOD_NOT_FOUND(类型化路由只截获已知方法)。"""
    response = client.post("/sim/no-such-method", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "METHOD_NOT_FOUND"


# ---------------------------------------------------------------------------
# CR 修正钉测:wrapper 顶层 forbid / 嵌套 base forbid / 探针数上限 / 手工检查防御
# ---------------------------------------------------------------------------

def test_typed_route_wrapper_unknown_top_level_key_rejected(
    client: TestClient,
) -> None:
    """wrapper extra=forbid:顶层键拼错("paramz")不再被静默吞掉 —
    否则 params 整体丢失,作业以零件/默认参数"成功"跑完,零告警。"""
    response = client.post(
        "/sim/densification",
        json={"paramz": {"initial_relative_density": 0.68}},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "paramz" in body["message"]


def test_nested_base_unknown_key_rejected(client: TestClient) -> None:
    """嵌套 base 随继承 forbid:DensificationParams 亦被 process-window /
    sensitivity 嵌套引用,base 内拼错键 → 400(其余嵌套构件仍忽略未知子键)。"""
    response = client.post(
        "/sim/process-window",
        json={"params": {"base": {"initial_relative_dens": 0.7}}},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "initial_relative_dens" in body["message"]


def test_probe_points_capped_at_99(client: TestClient) -> None:
    """探针数 ≤99 是写出契约(series 探针列宽 A2,3 位数探针号被截断撞键)→
    校验层强制拒绝,不再静默产出高号探针数据丢失的"成功"结果。"""
    response = client.post(
        "/sim/axisym-thermal",
        json={
            "params": {
                "profile": {"outer_radius_mm": 50.0, "height_mm": 150.0},
                "probe_points": [[0.0, 0.5]] * 100,
            }
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "probe_points" in body["message"]


def test_resolve_params_manual_unknown_key_check_pinned(settings: Settings) -> None:
    """_resolve_params 手工 unknown-key 检查在生产不可达(泛化路由只收未知方法,
    类型化路由的 params 已过 pydantic forbid)— 直调钉住其语义,防重构无声丢失。"""
    spec = registry.REGISTRY["densification"]
    with pytest.raises(ApiError) as exc_info:
        _resolve_params(spec, settings, None, {"bogus": 1})
    assert exc_info.value.status_code == 400
    assert "bogus" in str(exc_info.value.detail)
