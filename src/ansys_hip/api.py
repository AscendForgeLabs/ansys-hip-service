"""HTTP API — 应用工厂与全部端点(FastAPI).

端点分组(tags):meta(health)/ sim(方法提交与自描述)/ jobs(作业管理)/
parts(零件配置)/ uploads(几何上传)。

错误响应统一为 ErrorBody {code, message}:业务方抛 ApiError(detail={code, message}),
其余 HTTPException(如路由未命中)按状态码兜底映射;请求体校验失败统一 400。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ValidationError

from . import __version__, registry
from .queue import ACTIVE_STATUSES, ARTIFACTS_DIRNAME, JobQueue, LOG_FILENAME, RESULT_FILENAME
from .schemas import (
    ErrorBody,
    HealthReport,
    JobState,
    JobStatusEnum,
    PartInfo,
    SimAccepted,
    SimRequest,
    UploadAccepted,
)
from .settings import Settings, load_parts, load_settings, merge_params

logger = logging.getLogger(__name__)

SERVICE_TITLE = "ansys-hip-service"
SERVICE_DESCRIPTION = (
    "HIP 仿真计算方法提供方 — 面向 HIPForm 的方法级 ANSYS/MAPDL 计算服务。\n\n"
    "**调用模式**:`POST /sim/{method}` 提交作业(202)→ 轮询 `GET /jobs/{id}` →\n"
    "`GET /jobs/{id}/result` 取结果 JSON / `GET /jobs/{id}/artifacts/{name}` 下载工件。\n\n"
    "**参数三级覆盖**:请求内联 `params` > `config/parts/<零件>.yaml` > `config/service.yaml` defaults;\n"
    "合并结果写入作业目录 `resolved-params.json`,保证可追溯。\n\n"
    "单位约定:mm / MPa / s / ℃。内网免鉴权部署,请勿暴露公网。"
)

ALLOWED_UPLOAD_SUFFIXES = frozenset({".step", ".stp", ".stl"})
UPLOAD_ID_TOKEN_BYTES = 6
UPLOAD_CHUNK_BYTES = 1024 * 1024
LOG_TAIL_MAX_LINES = 10_000
ARTIFACT_FORBIDDEN_PATTERNS = ("/", "\\", "..")

# 非业务 HTTPException(路由未命中等)按状态码兜底的错误码
_CODE_BY_STATUS: dict[int, str] = {
    400: "INVALID_PARAMS",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    422: "INVALID_PARAMS",
    500: "INTERNAL",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(HTTPException):
    """业务错误:detail 携带 {code, message},由全局 handler 渲染为 ErrorBody。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用:配置 → 零件配置 → 队列 → 路由。"""
    resolved_settings = settings if settings is not None else load_settings()
    parts = load_parts(resolved_settings.parts_dir)
    queue = JobQueue(resolved_settings)
    _ensure_storage_dirs(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()

    app = FastAPI(
        title=SERVICE_TITLE,
        version=__version__,
        description=SERVICE_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.queue = queue
    app.include_router(_health_router(resolved_settings, queue))
    app.include_router(_sim_router(resolved_settings, parts, queue), prefix="/sim", tags=["sim"])
    app.include_router(_jobs_router(queue), prefix="/jobs", tags=["jobs"])
    app.include_router(_parts_router(parts), prefix="/parts", tags=["parts"])
    app.include_router(_uploads_router(resolved_settings), prefix="/uploads", tags=["uploads"])
    _install_error_handlers(app)
    return app


def _ensure_storage_dirs(settings: Settings) -> None:
    """启动前确保 jobs/uploads 根目录可创建可写(失败即启动失败)。"""
    for directory in (settings.jobs_root, settings.uploads_root):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"存储目录不可创建: {directory}({exc})") from exc


# ---------------------------------------------------------------------------
# meta:健康检查
# ---------------------------------------------------------------------------

def _health_router(settings: Settings, queue: JobQueue) -> APIRouter:
    router = APIRouter(tags=["meta"])

    @router.get("/health", response_model=HealthReport, summary="健康检查")
    def health() -> HealthReport:
        """MAPDL 可执行与许可文件状态 + 队列计数;两者齐备为 ok,否则 degraded。"""
        mapdl_found = _is_executable_file(settings.ansys.bin)
        license_env_set = _is_license_spec(settings.ansys.license_file)
        queue_running, queue_pending = queue.counts()
        return HealthReport(
            status="ok" if mapdl_found and license_env_set else "degraded",
            mapdl_found=mapdl_found,
            license_env_set=license_env_set,
            queue_running=queue_running,
            queue_pending=queue_pending,
            version=__version__,
        )

    return router


def _is_executable_file(path: str) -> bool:
    """路径存在、是普通文件且具可执行位。"""
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def _is_license_spec(spec: str) -> bool:
    """许可源已配置:.lic 文件路径存在,或为 FlexLM port@host 形式(如 1055@localhost)。"""
    if not spec:
        return False
    if Path(spec).is_file():
        return True
    return re.fullmatch(r"\d{1,5}@[\w.\-]+", spec) is not None


# ---------------------------------------------------------------------------
# sim:方法自描述 + 作业提交
# ---------------------------------------------------------------------------

def _sim_router(
    settings: Settings,
    parts: dict[str, dict[str, Any]],
    queue: JobQueue,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/methods",
        response_model=list[dict[str, Any]],
        summary="方法自描述清单(12 个)",
    )
    def list_methods() -> list[dict[str, Any]]:
        """按组排序的全部方法元数据,含各方法参数模型的 JSON Schema。"""
        return registry.methods_payload()

    @router.post(
        "/{method}",
        response_model=SimAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        summary="提交仿真作业",
        responses={
            400: {"model": ErrorBody, "description": "参数校验失败(INVALID_PARAMS)"},
            404: {"model": ErrorBody, "description": "方法或零件不存在"},
            501: {"model": ErrorBody, "description": "内核尚未实现(METHOD_NOT_IMPLEMENTED)"},
            503: {"model": ErrorBody, "description": "方法已临时下线(METHOD_DISABLED)"},
        },
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "零件配置全继承": {
                                "summary": "tc4-demo(几何/材料/曲线继承零件配置)",
                                "value": {"part": "tc4-demo"},
                            },
                            "零件配置+内联覆盖": {
                                "summary": "在零件配置基础上覆盖初始相对密度",
                                "value": {
                                    "part": "tc4-demo",
                                    "params": {"initial_relative_density": 0.68},
                                },
                            },
                            "纯内联参数": {
                                "summary": "不带零件,直接给参数与曲线",
                                "value": {
                                    "params": {
                                        "initial_relative_density": 0.62,
                                        "cycle": {
                                            "points": [
                                                {"time_s": 0, "temperature_c": 20, "pressure_mpa": 0},
                                                {"time_s": 3600, "temperature_c": 900, "pressure_mpa": 120},
                                                {"time_s": 10800, "temperature_c": 900, "pressure_mpa": 120},
                                            ]
                                        },
                                    }
                                },
                            },
                        }
                    }
                }
            }
        },
    )
    def submit_simulation(method: str, request: SimRequest) -> SimAccepted:
        """提交作业并入队;合并优先级 params > part 配置 > 主配置 defaults。"""
        spec = registry.get_method(method)
        if spec is None:
            raise ApiError(
                404, "METHOD_NOT_FOUND",
                f"未知方法 '{method}';可用方法见 GET /sim/methods",
            )
        if method in settings.methods.disabled:
            raise ApiError(
                503, "METHOD_DISABLED",
                f"方法 '{method}' 已被临时下线(config/service.yaml methods.disabled)",
            )
        executor = registry.resolve_executor(method)
        if executor is None:
            raise ApiError(501, "METHOD_NOT_IMPLEMENTED", f"方法 '{method}' 的内核尚未实现")
        part_config = None
        if request.part is not None:
            part_config = parts.get(request.part)
            if part_config is None:
                raise ApiError(
                    404, "PART_NOT_FOUND",
                    f"零件配置 '{request.part}' 不存在;可用: {sorted(parts)}",
                )
        params = _resolve_params(spec, settings, part_config, request.params)
        record = queue.submit(spec=spec, params=params, part=request.part, executor=executor)
        return SimAccepted(
            id=record.id,
            method=spec.name,
            status=record.status,
            fidelity=record.fidelity,
            status_url=f"/jobs/{record.id}",
        )

    return router


def _resolve_params(
    spec: registry.MethodSpec,
    settings: Settings,
    part_config: dict[str, Any] | None,
    inline_params: dict[str, Any] | None,
) -> BaseModel:
    """三级合并 + 方法参数模型校验;失败 → 400 INVALID_PARAMS(含错误摘要)。"""
    if inline_params:
        unknown = sorted(
            key for key in inline_params if key not in spec.params_model.model_fields
        )
        if unknown:
            raise ApiError(
                400, "INVALID_PARAMS",
                f"方法 '{spec.name}' 不存在参数字段: {unknown};合法字段见 GET /sim/methods",
            )
    merged = merge_params(spec, settings, part_config, inline_params)
    try:
        return spec.params_model.model_validate(merged)
    except ValidationError as exc:
        raise ApiError(400, "INVALID_PARAMS", _validation_summary(exc)) from exc


def _validation_summary(exc: ValidationError) -> str:
    """pydantic 校验错误 → '路径: 原因' 拼接的单行摘要。"""
    items = [
        f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    ]
    return "参数校验失败 → " + "; ".join(items)


# ---------------------------------------------------------------------------
# jobs:状态/日志/结果/工件/删除
# ---------------------------------------------------------------------------

def _jobs_router(queue: JobQueue) -> APIRouter:
    router = APIRouter()

    def require_job(job_id: str):
        record = queue.get(job_id)
        if record is None:
            raise ApiError(
                404, "JOB_NOT_FOUND",
                f"作业 '{job_id}' 不存在(可能已删除;服务重启后历史作业不可见)",
            )
        return record

    @router.get("/{job_id}", response_model=JobState, summary="查询作业状态")
    def get_job(job_id: str) -> JobState:
        """作业状态机快照:pending → running → succeeded/failed/cancelled。"""
        return queue.state(require_job(job_id))

    @router.get(
        "/{job_id}/log",
        response_class=PlainTextResponse,
        summary="作业日志(支持 tail)",
    )
    def get_job_log(
        job_id: str,
        tail: int | None = Query(default=None, ge=1, le=LOG_TAIL_MAX_LINES),
    ) -> PlainTextResponse:
        """job.log 全文;?tail=N 只取最后 N 行。"""
        record = require_job(job_id)
        log_path = record.job_dir / LOG_FILENAME
        text = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.is_file()
            else ""
        )
        if tail is not None:
            lines = text.splitlines(keepends=True)
            text = "".join(lines[-tail:])
        return PlainTextResponse(text, media_type="text/plain")

    @router.get(
        "/{job_id}/result",
        summary="取结果 JSON",
        responses={
            404: {"model": ErrorBody, "description": "作业不存在"},
            409: {"model": ErrorBody, "description": "未完成/已取消/已失败,无结果"},
        },
    )
    def get_job_result(job_id: str) -> JSONResponse:
        """内核返回的结果 JSON;仅 succeeded 状态可取。"""
        record = require_job(job_id)
        if record.status in (JobStatusEnum.PENDING, JobStatusEnum.RUNNING):
            raise ApiError(
                409, "RESULT_NOT_READY",
                f"作业尚未完成(当前状态 {record.status.value})",
            )
        if record.status is JobStatusEnum.CANCELLED:
            raise ApiError(409, "RESULT_NOT_READY", "作业已取消,无结果")
        if record.status is JobStatusEnum.FAILED:
            error = record.error or ErrorBody(code="INTERNAL", message="未知失败原因")
            raise ApiError(409, "JOB_FAILED", f"[{error.code}] {error.message}")
        result_path = record.job_dir / RESULT_FILENAME
        if not result_path.is_file():
            raise ApiError(500, "INTERNAL", f"result.json 缺失: {result_path}")
        return JSONResponse(content=json.loads(result_path.read_text(encoding="utf-8")))

    @router.get(
        "/{job_id}/artifacts",
        response_model=list[str],
        summary="列出工件文件",
        responses={404: {"model": ErrorBody, "description": "作业不存在"}},
    )
    def list_artifacts(job_id: str) -> list[str]:
        """作业 artifacts/ 目录内的工件文件名(按名排序),供 /artifacts/{name} 下载。"""
        record = require_job(job_id)
        artifacts_dir = record.job_dir / ARTIFACTS_DIRNAME
        if not artifacts_dir.is_dir():
            return []
        return sorted(path.name for path in artifacts_dir.iterdir() if path.is_file())

    @router.get(
        "/{job_id}/artifacts/{name}",
        summary="下载工件文件",
        responses={
            400: {"model": ErrorBody, "description": "非法工件名(路径穿越拒绝)"},
            404: {"model": ErrorBody, "description": "作业或工件不存在"},
        },
    )
    def get_artifact(job_id: str, name: str) -> FileResponse:
        """下载作业 artifacts/ 目录内的工件(名称禁止路径分隔符与 '..')。"""
        record = require_job(job_id)
        artifact_path = _resolve_artifact_path(record.job_dir, name)
        return FileResponse(artifact_path, filename=name)

    @router.delete(
        "/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="取消并删除作业",
        responses={404: {"model": ErrorBody, "description": "作业不存在"}},
    )
    def delete_job(job_id: str) -> Response:
        """pending/running 先取消(运行中会防御性终止内核),再清理作业目录。"""
        record = require_job(job_id)
        if record.status in ACTIVE_STATUSES:
            queue.cancel(job_id)
        queue.remove(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _resolve_artifact_path(job_dir: Path, name: str) -> Path:
    """校验工件名(防路径穿越)并解析到 artifacts 目录内的既有文件。"""
    is_illegal = (
        not name
        or name in {".", ".."}
        or any(pattern in name for pattern in ARTIFACT_FORBIDDEN_PATTERNS)
    )
    if is_illegal:
        raise ApiError(
            400, "INVALID_PARAMS",
            f"非法工件名 '{name}'(禁止路径分隔符与 '..')",
        )
    artifacts_root = (job_dir / ARTIFACTS_DIRNAME).resolve()
    artifact_path = (artifacts_root / name).resolve()
    if artifact_path == artifacts_root or artifacts_root not in artifact_path.parents:
        raise ApiError(400, "INVALID_PARAMS", f"工件路径越界: '{name}'")
    if not artifact_path.is_file():
        raise ApiError(404, "ARTIFACT_NOT_FOUND", f"工件 '{name}' 不存在")
    return artifact_path


# ---------------------------------------------------------------------------
# parts:零件配置
# ---------------------------------------------------------------------------

def _parts_router(parts: dict[str, dict[str, Any]]) -> APIRouter:
    router = APIRouter()

    @router.get("", response_model=list[PartInfo], summary="零件配置清单")
    def list_parts() -> list[PartInfo]:
        """config/parts/*.yaml 全部零件配置(按名排序)。"""
        return [PartInfo(name=name, config=config) for name, config in sorted(parts.items())]

    @router.get(
        "/{name}",
        response_model=PartInfo,
        summary="零件配置详情",
        responses={404: {"model": ErrorBody, "description": "零件不存在(PART_NOT_FOUND)"}},
    )
    def get_part(name: str) -> PartInfo:
        """单个零件的完整配置 dict(几何/材料/曲线/网格/numerics)。"""
        config = parts.get(name)
        if config is None:
            raise ApiError(
                404, "PART_NOT_FOUND",
                f"零件配置 '{name}' 不存在;可用: {sorted(parts)}",
            )
        return PartInfo(name=name, config=config)

    return router


# ---------------------------------------------------------------------------
# uploads:几何上传
# ---------------------------------------------------------------------------

def _uploads_router(settings: Settings) -> APIRouter:
    uploads_root = settings.uploads_root
    router = APIRouter()

    @router.post(
        "",
        response_model=UploadAccepted,
        summary="上传几何文件(STEP/STL)",
        responses={400: {"model": ErrorBody, "description": "扩展名不允许(INVALID_PARAMS)"}},
    )
    async def upload_geometry(
        file: UploadFile = File(..., description="几何文件,扩展名 .step/.stp/.stl"),
    ) -> UploadAccepted:
        """multipart 上传 → var/uploads/<id>_<原名>;返回服务端绝对路径供 geometry 引用。"""
        original_name = Path(file.filename or "").name  # 消毒:去掉任何路径部分
        suffix = Path(original_name).suffix.lower()
        if not original_name or suffix not in ALLOWED_UPLOAD_SUFFIXES:
            shown = file.filename or "(未提供文件名)"
            raise ApiError(
                400, "INVALID_PARAMS",
                f"不支持的文件 '{shown}';允许的扩展名: {sorted(ALLOWED_UPLOAD_SUFFIXES)}",
            )
        target = uploads_root / f"{secrets.token_urlsafe(UPLOAD_ID_TOKEN_BYTES)}_{original_name}"
        size_bytes = await _save_upload(file, target)
        logger.info("几何上传: %s(%d 字节)", target, size_bytes)
        return UploadAccepted(path=str(target.resolve()), size_bytes=size_bytes)

    return router


async def _save_upload(upload: UploadFile, target: Path) -> int:
    """分块写出上传内容并关闭句柄;返回字节数。"""
    size_bytes = 0
    try:
        with target.open("wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                size_bytes += len(chunk)
                out.write(chunk)
    finally:
        await upload.close()
    return size_bytes


# ---------------------------------------------------------------------------
# 全局错误处理(统一 ErrorBody)
# ---------------------------------------------------------------------------

def _install_error_handlers(app: FastAPI) -> None:
    """全部错误响应统一渲染为 ErrorBody {code, message}。"""

    @app.exception_handler(HTTPException)
    async def render_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        body = _error_body_from_detail(exc)
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def render_request_invalid(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        items = [
            f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        body = ErrorBody(code="INVALID_PARAMS", message="请求体校验失败 → " + "; ".join(items))
        return JSONResponse(status_code=400, content=body.model_dump())

    @app.exception_handler(Exception)
    async def render_unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        body = ErrorBody(code="INTERNAL", message=f"服务内部错误: {type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content=body.model_dump())


def _error_body_from_detail(exc: HTTPException) -> ErrorBody:
    """detail 为 {code, message} 时直接采用;其余按状态码兜底映射。"""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return ErrorBody.model_validate(detail)
    code = _CODE_BY_STATUS.get(exc.status_code, "INTERNAL")
    return ErrorBody(code=code, message=str(detail) or code)
