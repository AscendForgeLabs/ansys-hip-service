"""服务运行日志接管测试 — root/uvicorn/uvicorn.access 的重绑、静默与幂等.

接管契约见 uvicorn_log.py 模块 docstring:root 只动自己挂的 handler,
uvicorn 名下的默认 stream handler 无条件清空,uvicorn.access 与自研访问
日志重复故静默;应用 INFO 日志经 root 落文件(修复历史丢弃缺陷的钉子)。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from ansys_hip import uvicorn_log
from ansys_hip.api import create_app
from ansys_hip.settings import ServiceLogConfig, Settings
from ansys_hip.uvicorn_log import configure_uvicorn_logging


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _flush_managed() -> None:
    for handler in uvicorn_log._MANAGED_ROOT_HANDLERS:
        handler.flush()


def _install_fake_uvicorn_defaults() -> None:
    """预装 uvicorn 默认 dictConfig 装出的 stream handler 形状(接管对象)。"""
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [logging.StreamHandler()]
    uvicorn_logger.propagate = False
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = [logging.StreamHandler()]
    access_logger.propagate = False


def test_takeover_replaces_default_handlers(settings_factory):
    # Arrange(uvicorn 体系处于"默认 dictConfig 后"的状态)
    _install_fake_uvicorn_defaults()
    settings = settings_factory()

    # Act
    configure_uvicorn_logging(settings)

    # Assert(root:恰一个受管 handler 指向配置文件)
    assert len(uvicorn_log._MANAGED_ROOT_HANDLERS) == 1
    managed = uvicorn_log._MANAGED_ROOT_HANDLERS[0]
    assert managed in logging.getLogger().handlers
    assert isinstance(managed, TimedRotatingFileHandler)
    assert managed.baseFilename == str(settings.service_log_path)
    # Assert(uvicorn:默认 handler 已摘,改向 root 传播 → 文件)
    uvicorn_logger = logging.getLogger("uvicorn")
    assert uvicorn_logger.handlers == []
    assert uvicorn_logger.propagate is True
    # Assert(uvicorn.access:仅 NullHandler 且不传播 → 静默)
    access_logger = logging.getLogger("uvicorn.access")
    assert len(access_logger.handlers) == 1
    assert isinstance(access_logger.handlers[0], logging.NullHandler)
    assert access_logger.propagate is False


def test_repeated_configure_rebinds_single_handler(settings_factory, tmp_path):
    # Arrange
    first = settings_factory()
    second_target = tmp_path / "logs2" / "service.log"
    second = first.model_copy(
        update={"service_log": ServiceLogConfig(file=str(second_target))}
    )
    configure_uvicorn_logging(first)
    stale = uvicorn_log._MANAGED_ROOT_HANDLERS[0]

    # Act(测试反复 create_app 的同一形态:重复配置不累积)
    configure_uvicorn_logging(second)

    # Assert(root 仅一个受管 handler,指向新路径;旧 handler 已 close)
    assert len(uvicorn_log._MANAGED_ROOT_HANDLERS) == 1
    rebound = uvicorn_log._MANAGED_ROOT_HANDLERS[0]
    assert rebound is not stale
    assert rebound.baseFilename == str(second_target)
    assert stale not in logging.getLogger().handlers
    assert stale._closed is True  # 旧 handler 已 close,释放文件句柄


def test_foreign_root_handler_preserved(settings_factory):
    # Arrange(pytest/外部挂的 root handler 不是接管对象,不许误伤)
    sentinel = logging.Handler()
    root = logging.getLogger()
    root.addHandler(sentinel)
    try:
        # Act
        configure_uvicorn_logging(settings_factory())

        # Assert
        assert sentinel in root.handlers
    finally:
        root.removeHandler(sentinel)


def test_uvicorn_error_captured_access_silenced(settings_factory):
    # Arrange
    settings = settings_factory()
    configure_uvicorn_logging(settings)

    # Act
    logging.getLogger("uvicorn.error").info("ERROR-CHANNEL-MARK")
    logging.getLogger("uvicorn.access").info("ACCESS-CHANNEL-MARK")
    _flush_managed()

    # Assert(uvicorn.error → 文件;uvicorn.access 重复通道 → 不落)
    text = _read(settings.service_log_path)
    assert "ERROR-CHANNEL-MARK" in text
    assert "ACCESS-CHANNEL-MARK" not in text


def test_app_info_now_captured(settings_factory):
    """钉住历史缺陷修复:应用自身 INFO 日志此前被根级 WARNING 整批丢弃。"""
    # Arrange
    settings = settings_factory()
    configure_uvicorn_logging(settings)

    # Act
    logging.getLogger("ansys_hip.queue").info("队列启动标记")
    _flush_managed()

    # Assert
    assert "队列启动标记" in _read(settings.service_log_path)


def test_create_app_wires_service_log(settings_factory):
    # Arrange / Act
    settings: Settings = settings_factory()
    create_app(settings)

    # Assert(装配即接管,指向配置文件)
    root_handlers = logging.getLogger().handlers
    assert any(
        handler in uvicorn_log._MANAGED_ROOT_HANDLERS for handler in root_handlers
    )
    assert (
        uvicorn_log._MANAGED_ROOT_HANDLERS[0].baseFilename
        == str(settings.service_log_path)
    )
