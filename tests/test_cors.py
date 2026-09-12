"""开关式 CORS(server.cors_origins,默认关闭)— 供前端页面跨域嵌入回放组件拉取数据。

只放行 GET(回放组件仅需只读端点);默认空 = 行为与未加 CORS 完全一致。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ansys_hip.api import create_app
from ansys_hip.settings import ServerConfig, Settings, load_settings

REAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "service.yaml"


def _client_with_origins(settings: Settings, origins: tuple[str, ...]) -> TestClient:
    """以指定 cors_origins 构造进入 lifespan 的 TestClient。"""
    patched = settings.model_copy(
        update={"server": settings.server.model_copy(update={"cors_origins": origins})}
    )
    return TestClient(create_app(patched))


def test_cors_disabled_by_default(client: TestClient) -> None:
    """默认配置:任何 Origin 都不带 CORS 响应头(与未挂 CORS 中间件一致)。"""
    resp = client.get("/health", headers={"Origin": "http://front.example:3000"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_allowed_origin_gets_cors_headers(settings: Settings) -> None:
    """配置允许源后:该源的 GET 响应回显 Access-Control-Allow-Origin。"""
    origin = "http://front.example:3000"
    with _client_with_origins(settings, (origin,)) as client:
        resp = client.get("/health", headers={"Origin": origin})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == origin


def test_disallowed_origin_gets_no_cors_headers(settings: Settings) -> None:
    """非允许源:请求正常处理但无 CORS 头(浏览器侧即拒绝跨域读)。"""
    with _client_with_origins(settings, ("http://front.example:3000",)) as client:
        resp = client.get("/health", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


def test_preflight_rejects_non_get_methods(settings: Settings) -> None:
    """预检只放行 GET:POST 预检被拒(400),GET 预检通过并回显方法。"""
    origin = "http://front.example:3000"
    with _client_with_origins(settings, (origin,)) as client:
        post_preflight = client.options(
            "/jobs",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert post_preflight.status_code == 400

        get_preflight = client.options(
            "/jobs",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert get_preflight.status_code == 200
        assert get_preflight.headers["access-control-allow-methods"] == "GET"


def test_env_override_comma_separated(monkeypatch) -> None:
    """HIP_SERVICE_CORS_ORIGINS 逗号分隔覆盖(空白容忍)。"""
    monkeypatch.setenv(
        "HIP_SERVICE_CORS_ORIGINS", "http://a.example:1, http://b.example:2"
    )
    loaded = load_settings(REAL_CONFIG_PATH)
    assert loaded.server.cors_origins == ("http://a.example:1", "http://b.example:2")


def test_server_config_accepts_yaml_list_and_string() -> None:
    """yaml 列表与环境变量字符串两种形态都能解析为 tuple。"""
    assert ServerConfig(cors_origins=["a", "b"]).cors_origins == ("a", "b")
    assert ServerConfig(cors_origins="a, b").cors_origins == ("a", "b")
    assert ServerConfig().cors_origins == ()
