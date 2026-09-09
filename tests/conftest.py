"""共享 fixtures — 临时存储目录的 Settings + 假内核注册表(不碰 MAPDL)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from ansys_hip import registry
from ansys_hip.api import create_app
from ansys_hip.settings import (
    AnsysConfig,
    MethodsConfig,
    Settings,
    StorageConfig,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"
REAL_PARTS_DIR = REPO_ROOT / "config" / "parts"

# 测试默认指向不存在的 ANSYS 路径 → /health 为 degraded(不依赖部署机)
MISSING_ANSYS_BIN = "/nonexistent/ansys221"
MISSING_LICENSE = "/nonexistent/ansyslmd.lic"


def make_settings(
    tmp_path: Path,
    *,
    parts_dir: Path | None = None,
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
            "parts_dir": parts_dir if parts_dir is not None else REAL_PARTS_DIR,
        }
    )


@pytest.fixture
def settings_factory(tmp_path):
    """按需构造测试 Settings(默认用真实零件配置目录)。"""
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


@pytest.fixture
def fake_executors(monkeypatch) -> dict[str, Callable]:
    """按方法名注册假内核;未注册的方法解析为 None(模拟内核未实现)。"""
    table: dict[str, Callable] = {}
    monkeypatch.setattr(
        registry, "resolve_executor", lambda method: table.get(method)
    )
    return table
