"""API Key 鉴权中间件 — X-API-Key 请求头,fail-closed.

- 豁免:`/health`(探针/容器健康检查不带自定义头)、OPTIONS(CORS 预检
  无法携带自定义头;含 CORS 未挂载时的裸 OPTIONS)、根 `/`(仅 307 跳面板)
  与 `/panel` 前缀(运维面板静态壳,浏览器不带自定义头加载不出壳,面板 JS
  的 key 输入逻辑就无从运行;壳内数据端点 /jobs 等仍全鉴权);
- fail-closed:`auth.api_keys` 为空 = 除豁免外全部 401 —— 空配置是
  "拒绝一切"而非"关闭鉴权",公网暴露下忘配置也不会裸奔;
- 三种拒绝情形(未配置/缺头/错 key)共用错误码 UNAUTHORIZED、各自独立
  中文 message,便于合法调用方与运维排障;
- 401 响应直接构造 ErrorBody 信封:中间件位于全局 exception handler
  之外,抛 ApiError 会穿到 ServerErrorMiddleware 变 500;
- 比较用 `secrets.compare_digest`(bytes 形态,str 版要求 ASCII);
  多 key 逐个比较支持配置级轮换(追加新 key → 客户端切换 → 移除旧 key)。
"""

from __future__ import annotations

import secrets
from typing import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from .schemas import ErrorBody
from .settings import AuthConfig

UNAUTHORIZED = "UNAUTHORIZED"
# 精确豁免路径(探针用精确路径,不做尾斜杠宽容);根 / 仅 307 重定向到面板壳
EXEMPT_PATHS = frozenset({"/", "/health"})
# 前缀豁免:/panel 静态挂载(HTML/JS/CSS,无敏感数据;StaticFiles 独占该前缀,
# 不会命中业务端点,无旁路)
EXEMPT_PREFIXES = ("/panel",)


def _is_exempt(path: str) -> bool:
    """请求路径是否落在鉴权豁免面(精确路径或 /panel 前缀)。"""
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def _unauthorized(message: str) -> JSONResponse:
    """构造 401 错误信封(与全局错误体 ErrorBody{code,message} 同构)。"""
    return JSONResponse(
        status_code=401,
        content=ErrorBody(code=UNAUTHORIZED, message=message).model_dump(),
        headers={"WWW-Authenticate": 'ApiKey realm="ansys-hip-service"'},
    )


def make_api_key_middleware(auth: AuthConfig) -> Callable[..., Awaitable[Response]]:
    """构造鉴权中间件(`app.middleware("http")` 形态的 dispatch 函数)。

    挂载顺序约束(见 api.py):本中间件最先挂 = 三件套最内层 —— CORS 在
    外层短路预检,401 回包出站经 CORS 补 Access-Control-Allow-* 头(跨域
    前端可读错误体),访问日志最外层保证 401 攻击也一字不漏落日志。
    """
    # SecretStr 此处取明文做字节比较(仅内存中,不落日志)
    keys = tuple(key.get_secret_value() for key in auth.api_keys)

    async def dispatch(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "OPTIONS" or _is_exempt(request.url.path):
            return await call_next(request)
        provided = request.headers.get("x-api-key", "")
        if not keys:
            return _unauthorized(
                "服务未配置 API Key(auth.api_keys 为空,fail-closed):"
                "除 /health 与面板壳外全部拒绝;请配置 auth.api_keys"
                "或环境变量 HIP_SERVICE_API_KEYS 后重启"
            )
        if not provided:
            return _unauthorized("缺少 X-API-Key 请求头")
        candidate = provided.encode("utf-8")
        if not any(secrets.compare_digest(candidate, key.encode("utf-8")) for key in keys):
            return _unauthorized("X-API-Key 无效")
        return await call_next(request)

    return dispatch
