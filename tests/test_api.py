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
from ansys_hip.settings import Settings

# 复用 test_api_core 的测试工具(pytest 会把 tests/ 加入 sys.path)
from test_api_core import submit, wait_for_terminal

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
