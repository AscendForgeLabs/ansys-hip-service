"""服务运行日志接管 — uvicorn/应用 logger → service_log.file 按天轮转.

背景:此前 uvicorn 进程日志靠启动命令的 shell 重定向(uvicorn.log),
无轮转、路径不受配置控制;且根 logger 无 handler、有效级别 WARNING,
应用自身 INFO 日志(如「作业队列已启动」)被整批丢弃。本模块在 create_app
内接管,一并修复两个问题;自定义日志位置即改 service_log.file。

时序前提(已对照部署机 uvicorn 源码):Config.__init__ 的 configure_logging
先于应用导入执行,create_app 内接管生效后,"Started server process" 起的
全部 uvicorn 消息走本 handler;应用导入前的极早期输出(import 失败等)仍
落 console,属可接受边界。启动命令不得再传 --log-config(会与本接管互抢),
也不需要 shell 重定向(日志已全在文件)。

幂等(仿 access_log.py 的先清后挂套路,root 多一层约束):
- root 只摘自己挂的 handler(_MANAGED_ROOT_HANDLERS 追踪,测试反复
  create_app 重绑不累积),pytest/外部 handler 一律不动;
- uvicorn / uvicorn.access 名下的 handler 无条件清空 — 那是 uvicorn 默认
  dictConfig 装出的 stream handler,即接管对象。

uvicorn.access 与自研访问日志(ansys_hip.access,access_log.py)完全重复
(每请求一行)→ 静默(NullHandler + 不传播);ansys_hip.access 自带轮转
handler 且 propagate=False,双轨互不干扰。uvicorn.error 本就无 handler、
传播至 uvicorn → root 文件,无需处理。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

from .settings import Settings

# 只追踪自己挂到 root 的 handler(幂等替换;外部 handler 一律不动)
_MANAGED_ROOT_HANDLERS: list[logging.Handler] = []


def configure_uvicorn_logging(settings: Settings) -> None:
    """接管 root/uvicorn/uvicorn.access → service_log 文件(重复调用重绑不累积)。"""
    _rebind_managed_root_handler(settings)
    _redirect_uvicorn_to_root()
    _silence_uvicorn_access()


def _rebind_managed_root_handler(settings: Settings) -> None:
    """root:摘旧受管 handler,挂按天轮转文件 handler,级别放到 INFO。"""
    root = logging.getLogger()
    for stale in _MANAGED_ROOT_HANDLERS:
        root.removeHandler(stale)
        stale.close()
    _MANAGED_ROOT_HANDLERS.clear()
    # 修复历史缺陷:根 logger 默认 WARNING,应用 INFO 日志曾被整批丢弃
    root.setLevel(logging.INFO)
    target = settings.service_log_path
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        target,
        when="midnight",
        backupCount=settings.service_log.retention_days,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root.addHandler(handler)
    _MANAGED_ROOT_HANDLERS.append(handler)


def _redirect_uvicorn_to_root() -> None:
    """uvicorn:摘默认 stderr stream handler,改向 root 传播(→ 文件)。"""
    uvicorn_logger = logging.getLogger("uvicorn")
    for stale in list(uvicorn_logger.handlers):
        stale.close()
    uvicorn_logger.handlers.clear()
    uvicorn_logger.propagate = True


def _silence_uvicorn_access() -> None:
    """uvicorn.access:与自研访问日志完全重复 → 静默(NullHandler 不传播)。"""
    access_logger = logging.getLogger("uvicorn.access")
    for stale in list(access_logger.handlers):
        stale.close()
    access_logger.handlers.clear()
    access_logger.addHandler(logging.NullHandler())
    access_logger.propagate = False
