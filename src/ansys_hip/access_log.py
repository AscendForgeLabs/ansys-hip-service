"""请求访问日志 — 独立完整服务日志(全部请求一字不漏).

- logger 名 `ansys_hip.access`、`propagate=False`(不混入 uvicorn 控制台体系);
- `TimedRotatingFileHandler` 按天午夜轮转,保留 `access_log.retention_days` 天;
- 每次调用 `configure_access_logging` 先清空旧 handler 再挂新 handler
  (测试反复 create_app 时重绑到新路径,不累积);
- 访问日志写失败(logging 模块默认不抛)不影响请求处理;
- 中间件位于 ServerErrorMiddleware 之内、全局 Exception handler 之外:
  未处理异常以异常形式穿过,必须兜住记 500 再抛(由外层渲染响应体);
  客户端断连的 `asyncio.CancelledError`(BaseException,except Exception
  接不住)单独兜住记 499(nginx 惯例)再抛。

已知边界(部署注记):
- 每请求一次**同步**磁盘写发生在异步中间件内 — 内网面板/上游轮询规模
  (保留期 × 单并发)完全可承受,不为公网高吞吐场景优化;
- `TimedRotatingFileHandler` 的午夜轮转**非多进程安全** — 部署假设
  uvicorn 单进程单 worker(config/service.yaml 现状),多 worker 需改
  每进程独立文件或外部轮转(logrotate);
- 尾读不在本模块:见 logtail.read_tail(反向 seek,供两个日志端点共用)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Awaitable, Callable

from fastapi import Request, Response

from .settings import Settings

ACCESS_LOG_LOGGER_NAME = "ansys_hip.access"


def configure_access_logging(settings: Settings) -> logging.Logger:
    """配置访问日志 logger(每次调用重绑到 settings 的文件,不累积 handler)。"""
    access_logger = logging.getLogger(ACCESS_LOG_LOGGER_NAME)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    for stale in list(access_logger.handlers):
        stale.close()
    access_logger.handlers.clear()
    target = settings.access_log_path
    target.parent.mkdir(parents=True, exist_ok=True)
    access_logger.addHandler(
        TimedRotatingFileHandler(
            target,
            when="midnight",
            backupCount=settings.access_log.retention_days,
            encoding="utf-8",
        )
    )
    return access_logger


def make_access_log_middleware() -> Callable[..., Awaitable[Response]]:
    """构造访问日志中间件(`app.middleware("http")` 形态的 dispatch 函数)。"""

    async def dispatch(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            # 客户端断连:BaseException,须先于 except Exception 单独接住留痕
            _log_access(request, 499, time.perf_counter() - started)
            raise
        except Exception:
            _log_access(request, 500, time.perf_counter() - started)
            raise
        _log_access(request, response.status_code, time.perf_counter() - started)
        return response

    return dispatch


def _log_access(request: Request, status_code: int, elapsed_s: float) -> None:
    """写一行访问日志:`[ISO] host:port "METHOD path?query PROTO" status 耗时ms`。"""
    client = request.client
    peer = f"{client.host}:{client.port}" if client else "-"
    query = request.scope.get("query_string", b"").decode("latin-1")
    target = f"{request.url.path}?{query}" if query else request.url.path
    protocol = request.scope.get("http_version", "?")
    logging.getLogger(ACCESS_LOG_LOGGER_NAME).info(
        '[%s] %s "%s %s HTTP/%s" %d %.1fms',
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        peer,
        request.method,
        target,
        protocol,
        status_code,
        elapsed_s * 1000,
    )
