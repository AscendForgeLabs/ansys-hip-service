"""运维面板挂载测试 — /panel 静态可达 / 根路径重定向 / openapi 内省.

断言只钉结构(状态码/媒体类型/重定向目标),不断言面板文案与内容 —
静态文件由前端轨独立演进,本文件只保证挂载合同稳定。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ansys_hip.api import create_app
from ansys_hip.settings import Settings


def test_panel_index_served(client: TestClient) -> None:
    """GET /panel/ → 200,text/html,非空(html=True 由目录内 index.html 服务)。"""
    response = client.get("/panel/")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert response.text.strip()


def test_panel_assets_served(client: TestClient) -> None:
    """/panel/app.js 与 /panel/style.css 可达(200)。"""
    for asset in ("app.js", "style.css"):
        response = client.get(f"/panel/{asset}")
        assert response.status_code == 200, asset
        assert response.text.strip(), asset


def test_panel_without_trailing_slash_redirects(client: TestClient) -> None:
    """GET /panel → 307 → /panel/(挂载规范化重定向)。"""
    response = client.get("/panel", follow_redirects=False)
    assert response.status_code == 307, response.text
    assert response.headers["location"] == "/panel/"


def test_root_redirects_to_panel(client: TestClient) -> None:
    """GET / → 307 → /panel/(单跳,不经 /panel 二次中转)。"""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307, response.text
    assert response.headers["location"] == "/panel/"


def test_openapi_includes_new_paths(settings: Settings) -> None:
    """openapi 内省:列表与服务日志端点在册(延续 test_progress 内省模式)。"""
    spec = create_app(settings).openapi()
    paths = spec["paths"]
    assert "/jobs" in paths, "GET /jobs 列表端点应入 openapi"
    assert "/service/log" in paths, "GET /service/log 应入 openapi"
    assert "/jobs/{job_id}/log" in paths
    source_param = paths["/jobs/{job_id}/log"]["get"].get("parameters", [])
    by_name = {param.get("name"): param for param in source_param}
    assert by_name.get("source", {}).get("description"), "source 参数应带描述"


def test_openapi_includes_cancel_endpoint(settings: Settings) -> None:
    """openapi 内省:强制中断端点在册且带描述(保留现场语义进 Swagger)。"""
    spec = create_app(settings).openapi()
    cancel = spec["paths"]["/jobs/{job_id}/cancel"]["post"]
    assert cancel.get("summary"), "cancel 端点应带 summary"
    assert "保留" in cancel.get("description", ""), "cancel 描述应写明保留现场语义"
