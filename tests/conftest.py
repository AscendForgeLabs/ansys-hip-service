"""共享 fixtures — 临时存储目录的 Settings + 假内核注册表(不碰 MAPDL)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from ansys_hip import registry
from ansys_hip.api import create_app
from ansys_hip.settings import (
    AccessLogConfig,
    AnsysConfig,
    AuthConfig,
    MethodsConfig,
    ServiceLogConfig,
    Settings,
    StorageConfig,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"

# 测试默认指向不存在的 ANSYS 路径 → /health 为 degraded(不依赖部署机)
MISSING_ANSYS_BIN = "/nonexistent/ansys221"
MISSING_LICENSE = "/nonexistent/ansyslmd.lic"

# 鉴权 fail-closed:测试 Settings 默认配一个 key,TestClient 默认头携带同一个
TEST_API_KEY = "test-api-key"


def make_settings(
    tmp_path: Path,
    *,
    disabled: tuple[str, ...] = (),
    ansys_bin: str = MISSING_ANSYS_BIN,
    license_file: str = MISSING_LICENSE,
    job_timeout_s: int = 14400,
) -> Settings:
    """基于真实主配置构造测试 Settings:存储指到 tmp,其余可按需覆盖。"""
    base = load_settings(REAL_CONFIG_PATH)
    return base.model_copy(
        update={
            "ansys": AnsysConfig(
                bin=ansys_bin,
                license_file=license_file,
                np=base.ansys.np,
                job_timeout_s=job_timeout_s,
            ),
            "storage": StorageConfig(
                jobs_dir=str(tmp_path / "jobs"),
                uploads_dir=str(tmp_path / "uploads"),
                retention_days=base.storage.retention_days,
            ),
            "methods": MethodsConfig(disabled=disabled),
            # 鉴权默认配测试 key(fail-closed 下不带头的请求一律 401,
            # 现有用例经 _default_api_key_header 自动携带)
            "auth": AuthConfig(api_keys=(TEST_API_KEY,)),
            # 访问日志同样隔离到 tmp:测试运行不写仓库 var/logs/access.log
            "access_log": AccessLogConfig(
                file=str(tmp_path / "logs" / "access.log")
            ),
            # 服务运行日志(root logger 接管)同样隔离,防测试写仓库 var/logs/service.log
            "service_log": ServiceLogConfig(
                file=str(tmp_path / "logs" / "service.log")
            ),
        }
    )


@pytest.fixture
def settings_factory(tmp_path):
    """按需构造测试 Settings(默认 passthrough 关闭,按需在用例内开启)。"""
    def _factory(**kwargs: Any) -> Settings:
        return make_settings(tmp_path, **kwargs)

    return _factory


@pytest.fixture
def settings(settings_factory) -> Settings:
    return settings_factory()


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    """进入 lifespan 的 TestClient(队列工作协程随上下文启停)。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _default_api_key_header(monkeypatch):
    """全部 TestClient 默认携带测试 API key(鉴权 fail-closed 下不带即 401)。

    自建 TestClient(create_app(...)) 调用点遍布 7 个测试文件,逐处补头不可
    维护,故统一在构造入口注入默认头。仅当调用方未显式传 headers 时注入;
    显式 headers(含空 dict)原样透传,test_auth.py 借此完全控制鉴权行为。
    请求级 headers 仍可覆盖客户端默认头(httpx 语义)。
    """
    original = TestClient.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("headers", {"X-API-Key": TEST_API_KEY})
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", patched)


@pytest.fixture
def fake_executors(monkeypatch) -> dict[str, Callable]:
    """按方法名注册假内核;未注册的方法解析为 None(模拟内核未实现)。"""
    table: dict[str, Callable] = {}
    monkeypatch.setattr(
        registry, "resolve_executor", lambda method: table.get(method)
    )
    return table
