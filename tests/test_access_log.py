"""请求访问日志测试 — 全量落盘口径 / 中间件 500 兜底 / GET /service/log / handler 重绑.

覆盖:
    1. AccessLogConfig 配置节(默认值 / extra=forbid / access_log_path 派生);
    2. 中间件全量口径:200(/health)、404、未处理异常(500,先留痕再上抛)都记录;
    3. GET /service/log:返回内容与 tail 截尾(与 /jobs/{id}/log 同语义);
    4. 反复 create_app:logger 恰一个 handler,重绑到新文件不累积。

全部不运行 MAPDL;/boom 路由为测试自建,验证中间件在 ServerErrorMiddleware
之内、全局 Exception handler 之外的异常穿透路径。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ansys_hip.access_log import (
    ACCESS_LOG_LOGGER_NAME,
    make_access_log_middleware,
)
from ansys_hip.api import create_app
from ansys_hip.settings import AccessLogConfig, Settings


# ---------------------------------------------------------------------------
# 1. 配置节
# ---------------------------------------------------------------------------

def test_access_log_config_defaults() -> None:
    """默认:var/logs/access.log,按天午夜轮转保留 14 天(服务可不写该节)。"""
    config = AccessLogConfig()
    assert config.file == "var/logs/access.log"
    assert config.retention_days == 14


def test_access_log_config_rejects_unknown_keys() -> None:
    """frozen + extra=forbid:配置键拼错启动即暴露。"""
    with pytest.raises(ValidationError):
        AccessLogConfig.model_validate({"file": "x.log", "oops": 1})


def test_settings_derives_access_log_path(tmp_path: Path) -> None:
    """access_log_path 派生属性(绝对路径,仿 jobs_root)。"""
    target = tmp_path / "deep" / "access.log"
    settings = Settings.model_validate({"access_log": {"file": str(target)}})
    assert settings.access_log_path == target.resolve()


# ---------------------------------------------------------------------------
# 2. 中间件全量口径(200 / 404 / 未处理异常 500)
# ---------------------------------------------------------------------------

def test_health_request_logged(settings_factory, tmp_path: Path) -> None:
    """/health 轮询也一字不漏:方法/路径/协议/状态码齐备。"""
    with TestClient(create_app(settings_factory())) as client:
        response = client.get("/health")
        assert response.status_code == 200
    content = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
    assert '"GET /health HTTP/' in content
    assert " 200 " in content


def test_404_request_logged(settings_factory, tmp_path: Path) -> None:
    """404(路由未命中)同样记录(全量口径,不挑状态码)。"""
    with TestClient(create_app(settings_factory())) as client:
        assert client.get("/no-such-path").status_code == 404
    content = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
    assert '"GET /no-such-path HTTP/' in content
    assert " 404 " in content


def test_unhandled_exception_logged_as_500(settings_factory, tmp_path: Path) -> None:
    """未处理异常先留痕 500 再上抛(由全局 Exception handler 渲染响应体)。"""
    app = create_app(settings_factory())

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
        assert response.status_code == 500
    content = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
    assert '"GET /boom HTTP/' in content
    assert " 500 " in content


def test_cancelled_request_logged_as_499(settings_factory, tmp_path: Path) -> None:
    """客户端断连(asyncio.CancelledError 是 BaseException,except Exception
    接不住)单独留痕 499 后原样上抛 — 直调 dispatch 钉住该分支。"""
    create_app(settings_factory())  # 已把访问日志 logger 重绑到 tmp 文件
    scope = {
        "type": "http", "method": "GET", "path": "/cancelled",
        "headers": [], "query_string": b"", "http_version": "1.1",
        "client": ("testclient", 50000), "scheme": "http",
        "server": ("testserver", 80),
    }
    request = Request(scope)

    async def cancelled_call_next(_request: Request) -> Response:
        raise asyncio.CancelledError()

    dispatch = make_access_log_middleware()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatch(request, cancelled_call_next))
    content = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
    assert '"GET /cancelled HTTP/' in content
    assert " 499 " in content


def test_query_string_logged(settings_factory, tmp_path: Path) -> None:
    """完整路径含 query(?tail=1)——面板排查轮询问题时按参数定位。"""
    with TestClient(create_app(settings_factory())) as client:
        client.get("/service/log?tail=1")
    content = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
    assert '"GET /service/log?tail=1 HTTP/' in content


# ---------------------------------------------------------------------------
# 3. GET /service/log
# ---------------------------------------------------------------------------

def test_service_log_returns_content(settings_factory, tmp_path: Path) -> None:
    """缺省全文:此前每个请求各占一行,全部可见。"""
    with TestClient(create_app(settings_factory())) as client:
        client.get("/health")
        client.get("/health")
        response = client.get("/service/log")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.count('"GET /health') == 2


def test_service_log_tail_truncates(settings_factory, tmp_path: Path) -> None:
    """?tail=N 只取最后 N 行(端点读文件先于自身请求被记录,断言按前请求)。"""
    with TestClient(create_app(settings_factory())) as client:
        client.get("/health")          # 行 1
        client.get("/jobs")            # 行 2(末行)
        response = client.get("/service/log?tail=1")
    lines = response.text.splitlines()
    assert len(lines) == 1
    assert '"GET /jobs ' in lines[0]


def test_service_log_empty_before_any_request(settings_factory, tmp_path: Path) -> None:
    """尚无请求 → 空文本(不 500、不以缺文件报错)。"""
    with TestClient(create_app(settings_factory())) as client:
        response = client.get("/service/log")
    assert response.status_code == 200
    assert response.text == ""


# ---------------------------------------------------------------------------
# 4. 反复 create_app:handler 重绑不累积
# ---------------------------------------------------------------------------

def test_repeated_create_app_rebinds_single_handler(
    settings_factory, tmp_path: Path
) -> None:
    """第二个 app 的请求写进新文件;logger 恰一个 handler 指向新路径。"""

    # 本用例需要在同一测试内两个不同日志文件,显式覆盖 conftest 默认隔离
    def settings_writing_to(subdir: str) -> Settings:
        return settings_factory().model_copy(
            update={"access_log": AccessLogConfig(file=str(tmp_path / subdir / "access.log"))}
        )

    first = settings_writing_to("first")
    second = settings_writing_to("second")
    file_one = tmp_path / "first" / "access.log"
    file_two = tmp_path / "second" / "access.log"

    with TestClient(create_app(first)) as client_one:
        client_one.get("/health")
    assert '"GET /health' in file_one.read_text(encoding="utf-8")
    lines_one = file_one.read_text(encoding="utf-8").splitlines()

    with TestClient(create_app(second)) as client_two:
        client_two.get("/health")
        client_two.get("/jobs")

    # 旧文件不再增长;新文件收到第二个 app 的全部请求
    assert file_one.read_text(encoding="utf-8").splitlines() == lines_one
    content_two = file_two.read_text(encoding="utf-8")
    assert content_two.count('"GET /health') == 1
    assert '"GET /jobs ' in content_two

    logger = logging.getLogger(ACCESS_LOG_LOGGER_NAME)
    assert len(logger.handlers) == 1
    assert Path(logger.handlers[0].baseFilename) == file_two.resolve()


def test_settings_factory_isolates_access_log_to_tmp(
    settings_factory, tmp_path: Path
) -> None:
    """conftest 隔离:默认测试 Settings 的访问日志指到 tmp,不写仓库 var/logs。"""
    settings = settings_factory()
    assert tmp_path in settings.access_log_path.parents
