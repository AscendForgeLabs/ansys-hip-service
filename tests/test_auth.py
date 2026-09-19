"""API Key 鉴权中间件 — fail-closed(X-API-Key,除 /health 与 OPTIONS 外全保护)。

三种拒绝情形共用错误码 UNAUTHORIZED、各自独立中文 message;多 key 并存
支持配置级轮换。客户端一律显式传 headers,绕开 conftest 的默认头注入,
以完全控制鉴权输入。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansys_hip.api import create_app
from ansys_hip.settings import AuthConfig, Settings, load_settings

REAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "service.yaml"


def _auth_settings(settings: Settings, keys: tuple[str, ...]) -> Settings:
    """替换 auth.api_keys(其余配置沿用 settings fixture 的 tmp 隔离)。"""
    return settings.model_copy(update={"auth": AuthConfig(api_keys=keys)})


def _plain(keys: tuple) -> tuple[str, ...]:
    """SecretStr 元组取明文(仅测试断言用)。"""
    return tuple(key.get_secret_value() for key in keys)


def _client(settings: Settings, headers: dict[str, str]) -> TestClient:
    """显式 headers 构造进入 lifespan 的 TestClient(headers={} 即不带 key)。"""
    return TestClient(create_app(settings), headers=headers)


def test_missing_header_rejected(settings: Settings) -> None:
    """配置了 key 但请求不带 X-API-Key → 401,message 指明缺头。"""
    with _client(_auth_settings(settings, ("secret-key",)), headers={}) as client:
        resp = client.get("/sim/methods")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    assert "缺少" in resp.json()["message"]


def test_wrong_key_rejected(settings: Settings) -> None:
    """带了 key 但值不对 → 401,message 指明无效。"""
    with _client(
        _auth_settings(settings, ("secret-key",)),
        headers={"X-API-Key": "nope"},
    ) as client:
        resp = client.get("/sim/methods")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    assert "无效" in resp.json()["message"]


def test_valid_key_passes(settings: Settings) -> None:
    """正确 key → 业务端点正常(只读端点 200)。"""
    with _client(
        _auth_settings(settings, ("secret-key",)),
        headers={"X-API-Key": "secret-key"},
    ) as client:
        assert client.get("/sim/methods").status_code == 200
        assert client.get("/jobs").status_code == 200


def test_second_configured_key_passes(settings: Settings) -> None:
    """多 key 并存:第二个 key 同样放行(配置级轮换语义)。"""
    with _client(
        _auth_settings(settings, ("key-a", "key-b")),
        headers={"X-API-Key": "key-b"},
    ) as client:
        assert client.get("/sim/methods").status_code == 200


def test_fail_closed_when_no_keys_configured(settings: Settings) -> None:
    """fail-closed:api_keys 为空时,带不带 key 一律 401,message 指明未配置。"""
    empty = _auth_settings(settings, ())
    with _client(empty, headers={}) as client:
        resp = client.get("/sim/methods")
    assert resp.status_code == 401
    assert "未配置" in resp.json()["message"]
    with _client(empty, headers={"X-API-Key": "anything"}) as client:
        resp = client.get("/sim/methods")
    assert resp.status_code == 401
    assert "未配置" in resp.json()["message"]


def test_health_exempt_without_key(settings: Settings) -> None:
    """/health 豁免:无 key 也 200(探针不带自定义头;degraded 同样是 200)。"""
    with _client(
        _auth_settings(settings, ("secret-key",)), headers={}
    ) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_options_exempt(settings: Settings) -> None:
    """OPTIONS 豁免:CORS 预检不带自定义头,须穿过鉴权交给路由/CORS。"""
    with _client(
        _auth_settings(settings, ("secret-key",)), headers={}
    ) as client:
        resp = client.options("/sim/passthrough")
    assert resp.status_code == 405  # 穿过鉴权后由路由判定方法不允许(≠401)


def test_preflight_passes_with_cors_enabled(settings: Settings) -> None:
    """开启 CORS 后的正式预检:无 key 也 200(CORSMiddleware 在鉴权外层短路)。"""
    patched = _auth_settings(settings, ("secret-key",)).model_copy(
        update={
            "server": settings.server.model_copy(
                update={"cors_origins": ("http://front.example:3000",)}
            )
        }
    )
    with _client(patched, headers={}) as client:
        resp = client.options(
            "/jobs",
            headers={
                "Origin": "http://front.example:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-methods"] == "GET"


@pytest.mark.parametrize(
    "path",
    ["/", "/docs", "/openapi.json", "/panel/", "/jobs", "/sim/methods", "/service/log"],
)
def test_protected_surface(settings: Settings, path: str) -> None:
    """保护面覆盖:除 /health 外全部端点无 key 一律 401(含文档/面板/根重定向)。"""
    with _client(
        _auth_settings(settings, ("secret-key",)), headers={}
    ) as client:
        assert client.get(path).status_code == 401, f"{path} 未被鉴权保护"


def test_unauthorized_envelope_shape(settings: Settings) -> None:
    """401 响应体与全局错误信封同构:{code, message} 两字段。"""
    with _client(_auth_settings(settings, ("secret-key",)), headers={}) as client:
        resp = client.get("/sim/methods")
    assert set(resp.json()) == {"code", "message"}


def test_unauthorized_logged_in_access_log(settings: Settings) -> None:
    """401 攻击也落访问日志(中间件顺序:访问日志最外层,一字不漏)。"""
    with _client(_auth_settings(settings, ("secret-key",)), headers={}) as client:
        client.get("/sim/methods")
    log_text = settings.access_log_path.read_text(encoding="utf-8")
    assert "GET /sim/methods" in log_text
    assert " 401 " in log_text


def test_default_client_fixture_carries_key(client: TestClient) -> None:
    """conftest 默认头注入与 settings 默认 key 成对生效:普通 client 正常访问。"""
    assert client.get("/sim/methods").status_code == 200


def test_env_override_comma_separated(monkeypatch) -> None:
    """HIP_SERVICE_API_KEYS 逗号分隔覆盖(空白容忍),与 cors_origins 同构。"""
    monkeypatch.setenv("HIP_SERVICE_API_KEYS", "k1, k2")
    loaded = load_settings(REAL_CONFIG_PATH)
    assert _plain(loaded.auth.api_keys) == ("k1", "k2")


def test_auth_config_accepts_yaml_list_and_string() -> None:
    """yaml 列表与环境变量字符串两种形态都能解析为 tuple。"""
    assert _plain(AuthConfig(api_keys=["a", "b"]).api_keys) == ("a", "b")
    assert _plain(AuthConfig(api_keys="a, b").api_keys) == ("a", "b")
    assert AuthConfig().api_keys == ()


def test_auth_config_masks_secrets() -> None:
    """key 为 SecretStr:配置对象 repr 不含明文(防意外落日志)。"""
    config = AuthConfig(api_keys=("s3cret",))
    assert "s3cret" not in repr(config)
