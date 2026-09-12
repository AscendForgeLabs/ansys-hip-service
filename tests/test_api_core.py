"""API 核心测试 — 假内核全生命周期 / 错误分类 / 工件安全 / 健康检查与配置加载.

全部用假内核(monkeypatch registry.resolve_executor),不运行 MAPDL;
直通提交用例统一走 enabled_client(开关开启)+ 指向上传目录内的 entry 路径
(API 只做包含性判定,文件不必真实存在 — 真文件校验在内核侧)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ansys_hip import __version__
from ansys_hip.api import _resolve_artifact_path, create_app
from ansys_hip.registry import KernelError
from ansys_hip.settings import (
    PassthroughConfig,
    Settings,
    SettingsError,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"

POLL_INTERVAL_S = 0.02
DEFAULT_WAIT_S = 10.0
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


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


def submit(client: TestClient, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """提交作业并断言 202 受理,返回响应体。"""
    response = client.post(f"/sim/{method}", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def read_resolved_params(settings: Settings, job_id: str) -> dict[str, Any]:
    """读作业目录的 resolved-params.json(内联参数的可追溯落盘)。"""
    path = settings.jobs_root / job_id / "resolved-params.json"
    return json.loads(path.read_text(encoding="utf-8"))


def passthrough_payload(settings: Settings, **overrides: Any) -> dict[str, Any]:
    """直通提交载荷:entry 指向上传目录内(API 仅做包含性判定,文件可不存在)。"""
    params: dict[str, Any] = {
        "entry_file": str(settings.uploads_root / "fake_job.inp"),
        "declared_outputs": ["final.cdb"],
    }
    params.update(overrides)
    return {"params": params}


@pytest.fixture
def enabled_settings(settings_factory) -> Settings:
    """passthrough 开启的测试 Settings(直通提交测试的公共前置)。"""
    return settings_factory().model_copy(
        update={"passthrough": PassthroughConfig(enabled=True)}
    )


@pytest.fixture
def enabled_client(enabled_settings):
    """passthrough 开启、进入 lifespan 的 TestClient。"""
    with TestClient(create_app(enabled_settings)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 全生命周期
# ---------------------------------------------------------------------------

def test_full_lifecycle_submit_poll_result_artifact(
    enabled_client, fake_executors, enabled_settings
):
    # Arrange
    kernel = FakeKernel(
        result={"fidelity": "passthrough", "artifacts": ["curve.csv"]},
        artifacts={"curve.csv": "time_s,density\n0,0.65\n3600,0.9\n"},
    )
    fake_executors["passthrough"] = kernel

    # Act
    accepted = submit(
        enabled_client, "passthrough", passthrough_payload(enabled_settings)
    )
    state = wait_for_terminal(enabled_client, accepted["id"])

    # Assert(受理响应)
    assert accepted["method"] == "passthrough"
    assert accepted["status"] == "pending"
    assert accepted["status_url"] == f"/jobs/{accepted['id']}"
    # Assert(终态与结果)
    assert state["status"] == "succeeded"
    assert state["error"] is None
    assert state["started_at"] is not None
    assert state["finished_at"] is not None
    result = enabled_client.get(f"/jobs/{accepted['id']}/result")
    assert result.status_code == 200
    assert result.json()["artifacts"] == ["curve.csv"]
    # Assert(工件下载)
    artifact = enabled_client.get(f"/jobs/{accepted['id']}/artifacts/curve.csv")
    assert artifact.status_code == 200
    assert artifact.text.startswith("time_s,density")
    # Assert(内核收到的上下文与参数)
    params_obj, ctx = kernel.calls[0]
    assert ctx.job_dir.name == accepted["id"]
    assert ctx.ansys_np == enabled_settings.ansys.np
    assert params_obj.declared_outputs == ["final.cdb"]
    # Assert(可追溯落盘)
    state_json = json.loads(
        (enabled_settings.jobs_root / accepted["id"] / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state_json["status"] == "succeeded"
    assert state_json["resolved_params"]["declared_outputs"] == ["final.cdb"]


def test_kernel_error_marks_job_failed(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel(
        error=KernelError("CONVERGENCE_FAILED", "MAPDL 未收敛: time=3600 步长发散")
    )

    # Act
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )
    state = wait_for_terminal(enabled_client, accepted["id"])

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "CONVERGENCE_FAILED"
    assert "未收敛" in state["error"]["message"]
    conflict = enabled_client.get(f"/jobs/{accepted['id']}/result")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "JOB_FAILED"


def test_unexpected_kernel_error_maps_to_internal(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel(error=ValueError("boom"))

    # Act
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )
    state = wait_for_terminal(enabled_client, accepted["id"])

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "INTERNAL"
    assert "ValueError" in state["error"]["message"]
    assert "boom" in state["error"]["message"]


def test_kernel_timeout_maps_to_timeout(settings_factory, fake_executors):
    # Arrange
    settings = settings_factory(job_timeout_s=1).model_copy(
        update={"passthrough": PassthroughConfig(enabled=True)}
    )
    fake_executors["passthrough"] = FakeKernel(delay_s=3.0)

    # Act
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "passthrough", passthrough_payload(settings))
        state = wait_for_terminal(client, accepted["id"], timeout_s=15.0)

    # Assert
    assert state["status"] == "failed"
    assert state["error"]["code"] == "TIMEOUT"


def test_result_not_ready_returns_conflict_while_running(
    enabled_client, fake_executors
):
    # Arrange
    fake_executors["passthrough"] = FakeKernel(delay_s=2.0)

    # Act
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )

    # Assert
    conflict = enabled_client.get(f"/jobs/{accepted['id']}/result")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "RESULT_NOT_READY"


def test_job_log_contains_lifecycle_events_and_tail(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel()

    # Act
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )
    wait_for_terminal(enabled_client, accepted["id"])
    full_log = enabled_client.get(f"/jobs/{accepted['id']}/log")
    tail_log = enabled_client.get(f"/jobs/{accepted['id']}/log?tail=1")

    # Assert
    assert full_log.status_code == 200
    assert full_log.headers["content-type"].startswith("text/plain")
    assert "pending → running" in full_log.text
    assert "running → succeeded" in full_log.text
    assert "作业受理" in full_log.text
    assert len(tail_log.text.splitlines()) == 1
    assert "succeeded" in tail_log.text


# ---------------------------------------------------------------------------
# 方法注册表与 /sim/methods
# ---------------------------------------------------------------------------

def test_methods_endpoint_lists_single_passthrough(client):
    # Act
    response = client.get("/sim/methods")

    # Assert
    assert response.status_code == 200
    methods = response.json()
    assert len(methods) == 1
    entry = methods[0]
    assert entry["name"] == "passthrough"
    assert entry["group"] == "passthrough"
    assert entry["group_label"]
    assert entry["requires_mapdl"] is True
    assert entry["requires_geometry"] is False
    assert entry["params_schema"]["properties"]["entry_file"]


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

def test_unknown_method_returns_404(client):
    response = client.post("/sim/no-such-method", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "METHOD_NOT_FOUND"


def test_disabled_method_returns_503(settings_factory, fake_executors):
    # Arrange(方法存在且内核已实现,但被管理员下线 → 503;开关开启以证明
    # 503 判定发生在 403 直通关闸之后)
    settings = settings_factory(disabled=("passthrough",)).model_copy(
        update={"passthrough": PassthroughConfig(enabled=True)}
    )
    fake_executors["passthrough"] = FakeKernel()

    # Act / Assert
    with TestClient(create_app(settings)) as client:
        response = client.post("/sim/passthrough", json=passthrough_payload(settings))
    assert response.status_code == 503
    assert response.json()["code"] == "METHOD_DISABLED"


def test_unimplemented_method_returns_501(enabled_client, fake_executors):
    # Arrange(方法在注册表,但未注册内核 → resolve_executor 为 None)
    response = enabled_client.post(
        "/sim/passthrough",
        json=passthrough_payload(enabled_client.app.state.settings),
    )
    assert response.status_code == 501
    assert response.json()["code"] == "METHOD_NOT_IMPLEMENTED"


def test_unknown_inline_param_field_rejected(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel()

    # Act
    response = enabled_client.post(
        "/sim/passthrough", json={"params": {"bogus_field": 1}}
    )

    # Assert(内联参数的未知顶层键 → 拒绝,避免拼写错误被静默忽略)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"
    assert "bogus_field" in response.json()["message"]


def test_invalid_param_value_rejected(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel()

    # Act(timeout_s 须 > 0 被违反)
    response = enabled_client.post(
        "/sim/passthrough",
        json={"params": {
            "entry_file": "/var/uploads/x_job.inp",
            "declared_outputs": ["results.csv"],
            "timeout_s": 0,
        }},
    )

    # Assert
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"
    assert "timeout_s" in response.json()["message"]


def test_malformed_request_body_rejected(client, fake_executors):
    # Arrange(params 类型错误 → 框架层请求体校验先于 handler,统一 400)
    response = client.post("/sim/passthrough", json={"params": "not-a-dict"})

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

def test_delete_pending_job_cancels_and_queue_continues(
    enabled_client, fake_executors, enabled_settings
):
    # Arrange(单并发:A 运行中,B 排队 pending)
    fake_executors["passthrough"] = FakeKernel(delay_s=1.0)

    # Act
    payload = passthrough_payload(enabled_settings)
    running = submit(enabled_client, "passthrough", payload)
    queued = submit(enabled_client, "passthrough", payload)
    pending_state = enabled_client.get(f"/jobs/{queued['id']}").json()
    delete_response = enabled_client.delete(f"/jobs/{queued['id']}")
    after_delete = enabled_client.get(f"/jobs/{queued['id']}")
    final_running = wait_for_terminal(enabled_client, running["id"])

    # Assert
    assert pending_state["status"] == "pending"
    assert delete_response.status_code == 204
    assert after_delete.status_code == 404
    assert not (enabled_settings.jobs_root / queued["id"]).exists()  # 目录已清理
    assert final_running["status"] == "succeeded"  # 队列继续工作


def test_delete_running_job_cancels_and_next_job_runs(
    enabled_client, fake_executors, enabled_settings
):
    # Arrange
    fake_executors["passthrough"] = FakeKernel(delay_s=6.0)

    # Act
    payload = passthrough_payload(enabled_settings)
    first = submit(enabled_client, "passthrough", payload)
    second = submit(enabled_client, "passthrough", payload)
    wait_for_status(enabled_client, first["id"], "running")
    delete_response = enabled_client.delete(f"/jobs/{first['id']}")
    second_final = wait_for_terminal(enabled_client, second["id"])

    # Assert(运行中的作业被取消后,工作协程继续处理后续作业)
    assert delete_response.status_code == 204
    assert second_final["status"] == "succeeded"


def test_cancel_running_job_preserves_dir_and_queue_continues(
    enabled_client, fake_executors, enabled_settings
):
    # Arrange — 与 DELETE 的关键差异:中断保留排障现场(目录/state.json/job.log)
    fake_executors["passthrough"] = FakeKernel(delay_s=6.0)

    # Act
    payload = passthrough_payload(enabled_settings)
    first = submit(enabled_client, "passthrough", payload)
    second = submit(enabled_client, "passthrough", payload)
    wait_for_status(enabled_client, first["id"], "running")
    cancel_response = enabled_client.post(f"/jobs/{first['id']}/cancel")
    second_final = wait_for_terminal(enabled_client, second["id"])

    # Assert — 响应即取消后的状态;作业目录保留;队列继续处理后续作业
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"
    job_dir = enabled_settings.jobs_root / first["id"]
    assert (job_dir / "state.json").is_file()
    assert (job_dir / "job.log").is_file()
    assert enabled_client.get(f"/jobs/{first['id']}").json()["status"] == "cancelled"
    assert second_final["status"] == "succeeded"


def test_cancel_terminal_job_is_idempotent_noop(enabled_client, fake_executors):
    # Arrange — 已成功作业再 cancel:幂等返回当前状态,不迁移
    fake_executors["passthrough"] = FakeKernel()
    payload = passthrough_payload(enabled_client.app.state.settings)
    accepted = submit(enabled_client, "passthrough", payload)
    final = wait_for_terminal(enabled_client, accepted["id"])
    assert final["status"] == "succeeded"

    # Act
    cancel_response = enabled_client.post(f"/jobs/{accepted['id']}/cancel")

    # Assert
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "succeeded"


def test_cancel_missing_job_returns_404(enabled_client) -> None:
    # Act / Assert
    response = enabled_client.post("/jobs/nonexistent/cancel")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


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


def test_artifact_endpoint_blocks_path_traversal(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel(
        result={"fidelity": "passthrough", "artifacts": ["out.csv"]},
        artifacts={"out.csv": "x,y\n1,2\n"},
    )
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )
    wait_for_terminal(enabled_client, accepted["id"])

    # Act(编码后的穿越尝试)
    traversal_urls = (
        f"/jobs/{accepted['id']}/artifacts/%2e%2e%2fstate.json",
        f"/jobs/{accepted['id']}/artifacts/..%2f..%2fetc%2fpasswd",
    )

    # Assert(无论被路由层或守卫拦截,都绝不回显作业目录外内容)
    for url in traversal_urls:
        response = enabled_client.get(url)
        assert response.status_code in (400, 404)
        assert "resolved_params" not in response.text


def test_missing_artifact_returns_404(enabled_client, fake_executors):
    # Arrange
    fake_executors["passthrough"] = FakeKernel()
    accepted = submit(
        enabled_client, "passthrough",
        passthrough_payload(enabled_client.app.state.settings),
    )
    wait_for_terminal(enabled_client, accepted["id"])

    # Act / Assert
    response = enabled_client.get(f"/jobs/{accepted['id']}/artifacts/nope.csv")
    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"


# ---------------------------------------------------------------------------
# /health
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
    assert report["passthrough_enabled"] is False
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
    settings = settings_factory().model_copy(
        update={"passthrough": PassthroughConfig(enabled=True)}
    )
    fake_executors["passthrough"] = FakeKernel(delay_s=3.0)
    payload = passthrough_payload(settings)

    # Act
    with TestClient(create_app(settings)) as client:
        submit(client, "passthrough", payload)
        submit(client, "passthrough", payload)
        report = client.get("/health").json()

    # Assert(单并发:1 运行 + 1 排队)
    assert report["queue_running"] == 1
    assert report["queue_pending"] == 1


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
