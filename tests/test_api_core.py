"""API 核心测试 — 假内核全生命周期 / 三级参数合并 / 错误分类 / 上传与健康检查.

全部用假内核(monkeypatch registry.resolve_executor),不运行 MAPDL。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ansys_hip import __version__
from ansys_hip.api import _resolve_artifact_path, create_app
from ansys_hip.registry import KernelError
from ansys_hip.settings import Settings, SettingsError, load_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"

POLL_INTERVAL_S = 0.02
DEFAULT_WAIT_S = 10.0
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

# config/service.yaml defaults.cycle 的时刻序列(合并测试的基准断言)
SERVICE_DEFAULT_CYCLE_TIMES_S = [0, 3600, 14400, 16200, 18000]


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------

class FakeKernel:
    """可控假内核:延时 / 结果 / 抛错 / 写工件,并记录每次调用。"""

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> None:
        self.delay_s = delay_s
        self.result = result if result is not None else {"fidelity": "real", "value": 42}
        self.error = error
        self.artifacts = artifacts or {}
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, params: Any, ctx: Any) -> dict[str, Any]:
        self.calls.append((params, ctx))
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        for name, content in self.artifacts.items():
            (ctx.job_dir / "artifacts" / name).write_text(content, encoding="utf-8")
        return dict(self.result)


def wait_for_terminal(
    client: TestClient, job_id: str, timeout_s: float = DEFAULT_WAIT_S
) -> dict[str, Any]:
    """轮询作业直到终态;超时 fail 并附最后一次状态。"""
    deadline = time.monotonic() + timeout_s
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = client.get(f"/jobs/{job_id}").json()
        if state.get("status") in TERMINAL_STATUSES:
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"作业 {job_id} 在 {timeout_s}s 内未到终态,最后状态: {state}")


def submit(client: TestClient, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """提交作业并断言 202 受理,返回响应体。"""
    response = client.post(f"/sim/{method}", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def read_resolved_params(settings: Settings, job_id: str) -> dict[str, Any]:
    """读作业目录的 resolved-params.json(三级合并的可追溯落盘)。"""
    path = settings.jobs_root / job_id / "resolved-params.json"
    return json.loads(path.read_text(encoding="utf-8"))


def write_part_config(parts_dir: Path, name: str, config: dict[str, Any]) -> None:
    """向临时零件目录写一个零件配置 yaml。"""
    parts_dir.mkdir(parents=True, exist_ok=True)
    (parts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )


# 自定义零件:各节均与主配置默认不同,用于三级合并断言
HOT_SHORT_PART: dict[str, Any] = {
    "geometry": {"capsule_step": "/tmp/hot/capsule.step"},
    "materials": {
        "powder": "tc4",
        "capsule": "20steel",
        "overrides": {"tc4": {"yield_stress_mpa": 800}},
    },
    "cycle": {
        "points": [
            {"time_s": 0, "temperature_c": 20, "pressure_mpa": 0},
            {"time_s": 7200, "temperature_c": 950, "pressure_mpa": 130},
        ]
    },
    "mesh": {"mesh_size_mm": 4.0},
    "numerics": {"time_step_s": 60},
}


# ---------------------------------------------------------------------------
# 全生命周期
# ---------------------------------------------------------------------------

def test_full_lifecycle_submit_poll_result_artifact(
    client, fake_executors, settings
):
    # Arrange
    kernel = FakeKernel(
        result={"fidelity": "real", "value": 42, "artifacts": ["curve.csv"]},
        artifacts={"curve.csv": "time_s,density\n0,0.65\n3600,0.9\n"},
    )
    fake_executors["densification"] = kernel

    # Act
    accepted = submit(client, "densification", {"part": "tc4-demo"})
    state = wait_for_terminal(client, accepted["id"])

    # Assert(受理响应)
    assert accepted["method"] == "densification"
    assert accepted["status"] == "pending"
    assert accepted["fidelity"] == "real"
    assert accepted["status_url"] == f"/jobs/{accepted['id']}"
    # Assert(终态与结果)
    assert state["status"] == "succeeded"
    assert state["error"] is None
    assert state["started_at"] is not None
    assert state["finished_at"] is not None
    result = client.get(f"/jobs/{accepted['id']}/result")
    assert result.status_code == 200
    assert result.json()["value"] == 42
    # Assert(工件下载)
    artifact = client.get(f"/jobs/{accepted['id']}/artifacts/curve.csv")
    assert artifact.status_code == 200
    assert artifact.text.startswith("time_s,density")
    # Assert(内核收到的上下文与参数)
    params_obj, ctx = kernel.calls[0]
    assert ctx.job_dir.name == accepted["id"]
    assert ctx.ansys_np == settings.ansys.np
    assert params_obj.initial_relative_density == 0.65
    # Assert(可追溯落盘)
    state_json = json.loads(
        (settings.jobs_root / accepted["id"] / "state.json").read_text(encoding="utf-8")
    )
    assert state_json["status"] == "succeeded"
    assert state_json["resolved_params"]["initial_relative_density"] == 0.65


def test_kernel_error_marks_job_failed(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel(
        error=KernelError("CONVERGENCE_FAILED", "MAPDL 未收敛: time=3600 步长发散")
    )

    # Act
    accepted = submit(client, "densification", {})
    state = wait_for_terminal(client, accepted["id"])

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "CONVERGENCE_FAILED"
    assert "未收敛" in state["error"]["message"]
    conflict = client.get(f"/jobs/{accepted['id']}/result")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "JOB_FAILED"


def test_unexpected_kernel_error_maps_to_internal(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel(error=ValueError("boom"))

    # Act
    accepted = submit(client, "densification", {})
    state = wait_for_terminal(client, accepted["id"])

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "INTERNAL"
    assert "ValueError" in state["error"]["message"]
    assert "boom" in state["error"]["message"]


def test_kernel_timeout_maps_to_timeout(settings_factory, fake_executors):
    # Arrange
    settings = settings_factory(job_timeout_s=1)
    fake_executors["densification"] = FakeKernel(delay_s=3.0)

    # Act
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "densification", {})
        state = wait_for_terminal(client, accepted["id"], timeout_s=15.0)

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "TIMEOUT"


def test_result_not_ready_returns_conflict_while_running(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel(delay_s=2.0)

    # Act
    accepted = submit(client, "densification", {})

    # Assert
    conflict = client.get(f"/jobs/{accepted['id']}/result")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "RESULT_NOT_READY"


def test_job_log_contains_lifecycle_events_and_tail(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act
    accepted = submit(client, "densification", {})
    wait_for_terminal(client, accepted["id"])
    full_log = client.get(f"/jobs/{accepted['id']}/log")
    tail_log = client.get(f"/jobs/{accepted['id']}/log?tail=1")

    # Assert
    assert full_log.status_code == 200
    assert full_log.headers["content-type"].startswith("text/plain")
    assert "pending → running" in full_log.text
    assert "running → succeeded" in full_log.text
    assert "作业受理" in full_log.text
    assert len(tail_log.text.splitlines()) == 1
    assert "succeeded" in tail_log.text


# ---------------------------------------------------------------------------
# 三级参数合并(内联 > 零件配置 > 主配置 defaults)
# ---------------------------------------------------------------------------

def test_inline_params_override_part_config(client, fake_executors, settings):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act
    accepted = submit(
        client,
        "densification",
        {"part": "tc4-demo", "params": {"initial_relative_density": 0.68}},
    )
    resolved = read_resolved_params(settings, accepted["id"])

    # Assert(内联覆盖零件/主配置)
    assert resolved["initial_relative_density"] == 0.68
    # Assert(与该方法无关的分节被丢弃)
    assert "geometry" not in resolved
    assert "mesh" not in resolved
    # Assert(材料选择落到单数字段 material)
    assert resolved["material"]["powder"] == "tc4"
    assert resolved["material"]["capsule"] == "20steel"


def test_part_cycle_points_replace_service_defaults(
    settings_factory, fake_executors, tmp_path
):
    # Arrange(自定义零件:非空 cycle → points 整体替换)
    parts_dir = tmp_path / "parts-cycle"
    write_part_config(parts_dir, "hot-short", HOT_SHORT_PART)
    fake_executors["axisym-hip"] = FakeKernel()
    settings = settings_factory(parts_dir=parts_dir)

    # Act
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "axisym-hip", {"part": "hot-short"})
        resolved = read_resolved_params(settings, accepted["id"])

    # Assert
    cycle_times = [point["time_s"] for point in resolved["cycle"]["points"]]
    assert cycle_times == [0, 7200]
    assert resolved["cycle"]["points"][1]["temperature_c"] == 950


def test_defaults_cycle_used_when_part_cycle_empty(client, fake_executors, settings):
    # Arrange(tc4-demo 的 cycle 为空 → 继承主配置 900℃/120MPa/3h 曲线)
    fake_executors["densification"] = FakeKernel()

    # Act
    accepted = submit(client, "densification", {"part": "tc4-demo"})
    resolved = read_resolved_params(settings, accepted["id"])

    # Assert
    cycle_times = [point["time_s"] for point in resolved["cycle"]["points"]]
    assert cycle_times == SERVICE_DEFAULT_CYCLE_TIMES_S


def test_mesh_priority_inline_over_part_over_defaults(
    settings_factory, fake_executors, tmp_path
):
    # Arrange
    parts_dir = tmp_path / "parts-mesh"
    write_part_config(parts_dir, "hot-short", HOT_SHORT_PART)
    fake_executors["axisym-hip"] = FakeKernel()
    settings = settings_factory(parts_dir=parts_dir)

    # Act / Assert(零件 mesh 4.0 覆盖主配置 6.0;内联 2.0 再覆盖零件)
    with TestClient(create_app(settings)) as client:
        from_part = submit(client, "axisym-hip", {"part": "hot-short"})
        from_inline = submit(
            client,
            "axisym-hip",
            {"part": "hot-short", "params": {"mesh": {"mesh_size_mm": 2.0}}},
        )
    assert read_resolved_params(settings, from_part["id"])["mesh"]["mesh_size_mm"] == 4.0
    assert (
        read_resolved_params(settings, from_inline["id"])["mesh"]["mesh_size_mm"] == 2.0
    )


def test_densification_defaults_map_to_top_level_fields(client, fake_executors, settings):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act(无零件、无内联 → 纯主配置 defaults)
    accepted = submit(client, "densification", {})
    resolved = read_resolved_params(settings, accepted["id"])

    # Assert
    assert resolved["initial_relative_density"] == 0.65
    assert resolved["limiting_relative_density"] == 0.995


def test_part_sections_geometry_numerics_materials(
    settings_factory, fake_executors, tmp_path
):
    # Arrange
    parts_dir = tmp_path / "parts-sections"
    write_part_config(parts_dir, "hot-short", HOT_SHORT_PART)
    fake_executors["axisym-hip"] = FakeKernel()
    settings = settings_factory(parts_dir=parts_dir)

    # Act
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "axisym-hip", {"part": "hot-short"})
        resolved = read_resolved_params(settings, accepted["id"])

    # Assert(geometry/numerics 仅来自零件配置)
    assert resolved["geometry"]["capsule_step"] == "/tmp/hot/capsule.step"
    assert resolved["numerics"]["time_step_s"] == 60
    # Assert(materials:默认 powder/capsule + 零件 overrides 深合并)
    assert resolved["materials"]["powder"] == "tc4"
    assert resolved["materials"]["capsule"] == "20steel"
    assert resolved["materials"]["overrides"]["tc4"]["yield_stress_mpa"] == 800


def test_service_defaults_materials_used_when_part_omits(
    settings_factory, fake_executors, tmp_path
):
    # Arrange(零件无 materials 节 → 主配置 defaults.materials 兜底)
    parts_dir = tmp_path / "parts-nomaterial"
    write_part_config(parts_dir, "bare", {"mesh": {"mesh_size_mm": 3.0}})
    fake_executors["axisym-hip"] = FakeKernel()
    settings = settings_factory(parts_dir=parts_dir)

    # Act
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "axisym-hip", {"part": "bare"})
        resolved = read_resolved_params(settings, accepted["id"])

    # Assert
    assert resolved["materials"]["powder"] == "tc4"
    assert resolved["materials"]["capsule"] == "20steel"


# ---------------------------------------------------------------------------
# 方法注册表与 /sim/methods
# ---------------------------------------------------------------------------

def test_methods_endpoint_lists_all_twelve(client):
    # Act
    response = client.get("/sim/methods")

    # Assert
    assert response.status_code == 200
    methods = response.json()
    assert len(methods) == 12
    names = {method["name"] for method in methods}
    assert names == {
        "densification", "process-window", "shrinkage-estimate", "material-query",
        "mesh", "axisym-hip", "axisym-thermal", "axisym-mechanical",
        "full3d-hip", "calibrate", "compensate", "sensitivity",
    }
    by_name = {method["name"]: method for method in methods}
    assert by_name["densification"]["group"] == "quick"
    assert by_name["full3d-hip"]["requires_geometry"] is True
    assert by_name["mesh"]["requires_mapdl"] is False
    assert "params_schema" in by_name["densification"]


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

def test_unknown_method_returns_404(client):
    response = client.post("/sim/no-such-method", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "METHOD_NOT_FOUND"


def test_disabled_method_returns_503(settings_factory, fake_executors):
    # Arrange(方法存在且内核已实现,但被管理员下线 → 503 优先)
    settings = settings_factory(disabled=("densification",))
    fake_executors["densification"] = FakeKernel()

    # Act / Assert
    with TestClient(create_app(settings)) as client:
        response = client.post("/sim/densification", json={})
    assert response.status_code == 503
    assert response.json()["code"] == "METHOD_DISABLED"


def test_unimplemented_method_returns_501(client, fake_executors):
    # Arrange(方法在注册表,但未注册内核 → resolve_executor 为 None)
    response = client.post("/sim/densification", json={})
    assert response.status_code == 501
    assert response.json()["code"] == "METHOD_NOT_IMPLEMENTED"


def test_unknown_part_returns_404(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act / Assert
    response = client.post("/sim/densification", json={"part": "no-such-part"})
    assert response.status_code == 404
    assert response.json()["code"] == "PART_NOT_FOUND"


def test_unknown_inline_param_field_rejected(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act
    response = client.post(
        "/sim/densification", json={"params": {"bogus_field": 1}}
    )

    # Assert(内联参数的未知顶层键 → 拒绝,避免拼写错误被静默忽略)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"
    assert "bogus_field" in response.json()["message"]


def test_invalid_param_value_rejected(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act(0 < initial_relative_density < 1 被违反)
    response = client.post(
        "/sim/densification", json={"params": {"initial_relative_density": 1.5}}
    )

    # Assert
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"
    assert "initial_relative_density" in response.json()["message"]


def test_malformed_request_body_rejected(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()

    # Act(params 类型错误 → 请求体校验失败统一 400)
    response = client.post("/sim/densification", json={"params": "not-a-dict"})

    # Assert
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"


def test_unknown_job_returns_404(client):
    response = client.get("/jobs/nonexistent")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# 取消与删除
# ---------------------------------------------------------------------------

def test_delete_pending_job_cancels_and_queue_continues(client, fake_executors, settings):
    # Arrange(单并发:A 运行中,B 排队 pending)
    fake_executors["densification"] = FakeKernel(delay_s=1.0)

    # Act
    running = submit(client, "densification", {})
    queued = submit(client, "densification", {})
    pending_state = client.get(f"/jobs/{queued['id']}").json()
    delete_response = client.delete(f"/jobs/{queued['id']}")
    after_delete = client.get(f"/jobs/{queued['id']}")
    final_running = wait_for_terminal(client, running["id"])

    # Assert
    assert pending_state["status"] == "pending"
    assert delete_response.status_code == 204
    assert after_delete.status_code == 404
    assert not (settings.jobs_root / queued["id"]).exists()  # 目录已清理
    assert final_running["status"] == "succeeded"  # 队列继续工作


def test_delete_running_job_cancels_and_next_job_runs(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel(delay_s=6.0)

    # Act
    first = submit(client, "densification", {})
    second = submit(client, "densification", {})
    wait_for_status(client, first["id"], "running")
    delete_response = client.delete(f"/jobs/{first['id']}")
    second_final = wait_for_terminal(client, second["id"])

    # Assert(运行中的作业被取消后,工作协程继续处理后续作业)
    assert delete_response.status_code == 204
    assert second_final["status"] == "succeeded"


def wait_for_status(
    client: TestClient, job_id: str, expected: str, timeout_s: float = DEFAULT_WAIT_S
) -> dict[str, Any]:
    """轮询作业直到出现指定状态(用于构造『运行中』前置条件)。"""
    deadline = time.monotonic() + timeout_s
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = client.get(f"/jobs/{job_id}").json()
        if state.get("status") == expected:
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"作业 {job_id} 未进入 {expected} 状态,最后状态: {state}")


# ---------------------------------------------------------------------------
# 工件安全
# ---------------------------------------------------------------------------

def test_artifact_name_guard_rejects_traversal(tmp_path):
    # Arrange
    job_dir = tmp_path / "job"
    (job_dir / "artifacts").mkdir(parents=True)

    # Act / Assert
    for bad_name in ("../state.json", "a/b.txt", "a\\b.txt", "..", "."):
        with pytest.raises(HTTPException) as excinfo:
            _resolve_artifact_path(job_dir, bad_name)
        assert excinfo.value.status_code == 400


def test_artifact_endpoint_blocks_path_traversal(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel(
        result={"fidelity": "real", "artifacts": ["out.csv"]},
        artifacts={"out.csv": "x,y\n1,2\n"},
    )
    accepted = submit(client, "densification", {})
    wait_for_terminal(client, accepted["id"])

    # Act(编码后的穿越尝试)
    traversal_urls = (
        f"/jobs/{accepted['id']}/artifacts/%2e%2e%2fstate.json",
        f"/jobs/{accepted['id']}/artifacts/..%2f..%2fetc%2fpasswd",
    )

    # Assert(无论被路由层或守卫拦截,都绝不回显作业目录外内容)
    for url in traversal_urls:
        response = client.get(url)
        assert response.status_code in (400, 404)
        assert "resolved_params" not in response.text


def test_missing_artifact_returns_404(client, fake_executors):
    # Arrange
    fake_executors["densification"] = FakeKernel()
    accepted = submit(client, "densification", {})
    wait_for_terminal(client, accepted["id"])

    # Act / Assert
    response = client.get(f"/jobs/{accepted['id']}/artifacts/nope.csv")
    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"


# ---------------------------------------------------------------------------
# /health 与 /parts
# ---------------------------------------------------------------------------

def test_health_degraded_when_ansys_missing(client):
    response = client.get("/health")
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "degraded"
    assert report["mapdl_found"] is False
    assert report["license_env_set"] is False
    assert report["queue_running"] == 0
    assert report["queue_pending"] == 0
    assert report["version"] == __version__


def test_health_ok_when_bin_and_license_exist(settings_factory, tmp_path):
    # Arrange(伪造可执行文件与许可文件)
    fake_bin = tmp_path / "ansys221"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    fake_license = tmp_path / "ansyslmd.lic"
    fake_license.write_text("LICENSE\n", encoding="utf-8")
    settings = settings_factory(ansys_bin=str(fake_bin), license_file=str(fake_license))

    # Act / Assert
    with TestClient(create_app(settings)) as client:
        report = client.get("/health").json()
    assert report["status"] == "ok"
    assert report["mapdl_found"] is True
    assert report["license_env_set"] is True


def test_health_counts_pending_jobs(settings_factory, fake_executors):
    # Arrange
    settings = settings_factory()
    fake_executors["densification"] = FakeKernel(delay_s=3.0)

    # Act
    with TestClient(create_app(settings)) as client:
        submit(client, "densification", {})
        submit(client, "densification", {})
        report = client.get("/health").json()

    # Assert(单并发:1 运行 + 1 排队)
    assert report["queue_running"] == 1
    assert report["queue_pending"] == 1


def test_parts_listing_and_detail(client):
    # Act
    listing = client.get("/parts")
    detail = client.get("/parts/tc4-demo")
    missing = client.get("/parts/zzz")

    # Assert
    assert listing.status_code == 200
    names = [part["name"] for part in listing.json()]
    assert "tc4-demo" in names
    assert detail.status_code == 200
    assert detail.json()["config"]["geometry"]["capsule_step"] == "/home/yushen/tempt/capsule.step"
    assert missing.status_code == 404
    assert missing.json()["code"] == "PART_NOT_FOUND"


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

def test_upload_accepts_step_and_feeds_sim(client, fake_executors, settings):
    # Arrange
    fake_executors["mesh"] = FakeKernel()
    step_bytes = b"ISO-10303-21 fake step content"

    # Act
    upload = client.post(
        "/uploads",
        files={"file": ("capsule.step", step_bytes, "application/octet-stream")},
    )
    accepted = submit(
        client, "mesh", {"params": {"geometry": {"capsule_step": upload.json()["path"]}}}
    )

    # Assert
    assert upload.status_code == 200
    body = upload.json()
    assert body["path"].endswith("_capsule.step")
    assert body["size_bytes"] == len(step_bytes)
    assert Path(body["path"]).is_file()
    assert Path(body["path"]).parent == settings.uploads_root
    assert accepted["status"] == "pending"


def test_upload_rejects_disallowed_extension(client):
    response = client.post(
        "/uploads",
        files={"file": ("wing.iges", b"data", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"
    assert ".iges" in response.json()["message"] or "iges" in response.json()["message"]


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def test_load_settings_missing_file_clear_error(tmp_path):
    with pytest.raises(SettingsError, match="不存在"):
        load_settings(tmp_path / "nope.yaml")


def test_env_overrides_config_values(monkeypatch, tmp_path):
    # Arrange
    jobs_dir = tmp_path / "env-jobs"
    monkeypatch.setenv("HIP_SERVICE_JOBS_DIR", str(jobs_dir))
    monkeypatch.setenv("ANSYS_BIN", "/custom/ansys221")

    # Act
    settings = load_settings(REAL_CONFIG_PATH)

    # Assert
    assert settings.storage.jobs_dir == str(jobs_dir)
    assert settings.ansys.bin == "/custom/ansys221"
