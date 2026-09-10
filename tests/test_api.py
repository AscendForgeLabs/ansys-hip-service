"""lead 集成测试 — 真实内核接线 / 注册表收敛 / 工件列表端点 / _resolve_params 钉测.

与 test_api_core.py(假内核全生命周期)互补:
  - 这里走**真实** resolve_executor(不 monkeypatch),验证 kernels ↔ registry 接线;
  - GET /jobs/{id}/artifacts 集合端点(与 JobState.artifacts_url 对应);
  - 手写直通路由(exclude_unset dump)与 _resolve_params 直调的参数等价性。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ansys_hip import registry
from ansys_hip.api import ApiError, _resolve_params

# 复用 test_api_core 的测试工具与 fixtures(pytest 会把 tests/ 加入 sys.path)
from test_api_core import (
    enabled_client,
    enabled_settings,
    passthrough_payload,
    read_resolved_params,
    submit,
    wait_for_terminal,
)


# ---------------------------------------------------------------------------
# 注册表 ↔ 内核契约:passthrough 能解析到真实执行器
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(registry.REGISTRY))
def test_every_method_resolves_real_executor(name: str) -> None:
    """不 monkeypatch:真实懒加载 kernels.<mod>.run_<method>,无 501 方法。"""
    executor = registry.resolve_executor(name)
    assert callable(executor), f"方法 {name} 未解析到执行器"
    assert executor.__name__ == "run_" + name.replace("-", "_")


def test_registry_covers_exactly_one_method() -> None:
    """类型化方法库移除后,注册表收敛为 passthrough 单方法。"""
    assert set(registry.REGISTRY) == {"passthrough"}
    assert registry.REGISTRY["passthrough"].group in registry.GROUP_LABELS


# ---------------------------------------------------------------------------
# GET /jobs/{id}/artifacts 集合端点
# ---------------------------------------------------------------------------

def test_artifacts_listing_endpoint(
    enabled_client: TestClient, fake_executors, enabled_settings
) -> None:
    """artifacts_url 指向集合端点;返回按名排序的工件文件列表。"""

    def kernel_with_artifacts(params: Any, ctx: Any) -> dict[str, Any]:
        for artifact_name in ("curve.csv", "summary.txt"):
            (ctx.job_dir / "artifacts" / artifact_name).write_text(
                "data", encoding="utf-8"
            )
        return {"fidelity": "passthrough", "artifacts": ["curve.csv", "summary.txt"]}

    fake_executors["passthrough"] = kernel_with_artifacts
    accepted = submit(
        enabled_client, "passthrough", passthrough_payload(enabled_settings)
    )
    state = wait_for_terminal(enabled_client, accepted["id"])
    assert state["status"] == "succeeded", state
    assert state["artifacts_url"] == f"/jobs/{accepted['id']}/artifacts"

    listing = enabled_client.get(state["artifacts_url"])
    assert listing.status_code == 200
    assert listing.json() == ["curve.csv", "summary.txt"]  # 按名排序

    # 无工件作业 → 空列表(而非 404)
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    bare = submit(enabled_client, "passthrough", passthrough_payload(enabled_settings))
    bare_state = wait_for_terminal(enabled_client, bare["id"])
    assert bare_state["status"] == "succeeded", bare_state
    assert enabled_client.get(f"/jobs/{bare['id']}/artifacts").json() == []


def test_artifacts_listing_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/jobs/nonexistent/artifacts")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# 手写直通路由 ↔ _submit 管线等价性
# ---------------------------------------------------------------------------

def test_passthrough_route_matches_resolve_params(
    enabled_client: TestClient, fake_executors, enabled_settings
) -> None:
    """手写路由(exclude_unset dump)与 _resolve_params 直调(原始 payload dict)
    对同一内联参数产出完全一致的模型实例 — 机制上即泛化处理器的请求处理体。"""
    calls: list[Any] = []

    def capture(params: Any, ctx: Any) -> dict[str, Any]:
        calls.append(params)
        return {"fidelity": "passthrough"}

    fake_executors["passthrough"] = capture
    payload = passthrough_payload(enabled_settings)
    accepted = submit(enabled_client, "passthrough", payload)
    state = wait_for_terminal(enabled_client, accepted["id"])
    assert state["status"] == "succeeded", state

    expected = _resolve_params(
        registry.REGISTRY["passthrough"], payload["params"]
    )
    assert calls and calls[0] == expected
    assert read_resolved_params(enabled_settings, accepted["id"]) == expected.model_dump(
        mode="json"
    )


def test_generic_submit_route_hidden_from_openapi(client: TestClient) -> None:
    """泛化 POST /sim/{method} 运行时保留(URL 形态兼容)但不出现在 Swagger;
    提交路由仅剩手写 POST /sim/passthrough。"""
    spec = client.app.openapi()
    assert "/sim/{method}" not in spec["paths"]
    sim_posts = sorted(
        path for path, item in spec["paths"].items()
        if path.startswith("/sim/") and "post" in item
    )
    assert sim_posts == ["/sim/passthrough"]


def test_wrapper_unknown_top_level_key_rejected(client: TestClient) -> None:
    """wrapper extra=forbid:顶层键拼错("paramz")不再被静默吞掉 —
    否则 params 整体丢失,作业以默认参数"成功"跑完,零告警。"""
    response = client.post(
        "/sim/passthrough",
        json={"paramz": {"entry_file": "/var/uploads/x_job.inp"}},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "paramz" in body["message"]


def test_resolve_params_manual_unknown_key_check_pinned() -> None:
    """_resolve_params 手工 unknown-key 检查在生产不可达(泛化路由只收未知方法,
    手写路由的 params 已过 pydantic forbid)— 直调钉住其语义,防重构无声丢失。"""
    spec = registry.REGISTRY["passthrough"]
    with pytest.raises(ApiError) as exc_info:
        _resolve_params(spec, {"bogus": 1})
    assert exc_info.value.status_code == 400
    assert "bogus" in str(exc_info.value.detail)
