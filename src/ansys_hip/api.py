"""HTTP API — 应用工厂与全部端点(FastAPI).

端点分组(tags):meta(health)/ sim(passthrough 提交与方法自描述)/
jobs(作业管理)/ uploads(APDL 上传)。

错误响应统一为 ErrorBody {code, message}:业务方抛 ApiError(detail={code, message}),
其余 HTTPException(如路由未命中)按状态码兜底映射;请求体校验失败统一 400。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__, registry
from .access_log import configure_access_logging, make_access_log_middleware
from .logtail import read_log_view
from .queue import (
    ACTIVE_STATUSES,
    ARTIFACTS_DIRNAME,
    JobQueue,
    JobSnapshot,
    LOG_FILENAME,
    RESULT_FILENAME,
)
from .results import OUT_FILENAME
from .schemas import (
    ErrorBody,
    HealthReport,
    JobState,
    JobStatusEnum,
    PassthroughParams,
    PassthroughSimRequest,
    SimAccepted,
    SimRequest,
    UploadAccepted,
)
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)

SERVICE_TITLE = "ansys-hip-service"
SERVICE_DESCRIPTION = (
    "HIP 仿真计算直通服务 — 面向 HIPForm 的 ANSYS/MAPDL 作业治理通道"
    "(类型化方法库已移除,唯一方法为 passthrough)。\n\n"
    "**调用模式**:入口/附属 APDL 文件先经 `POST /uploads/apdl` 上传 →\n"
    "`POST /sim/passthrough` 提交作业(202)→ 轮询 `GET /jobs/{id}` →\n"
    "`GET /jobs/{id}/result` 取结果 JSON / `GET /jobs/{id}/artifacts/{name}` 下载工件。\n\n"
    "服务不做任何仿真逻辑,只治理作业/队列/超时/工件;参数原样落盘作业目录\n"
    "`resolved-params.json`,保证可追溯。\n\n"
    "内置运维面板:`/panel`(作业列表/日志/服务请求日志,根路径 `/` 重定向至面板)。\n\n"
    "单位约定:mm / MPa / s / ℃。内网免鉴权部署,请勿暴露公网。"
)

# 内嵌运维面板静态资源目录(原生 JS 单页,零构建;目录缺失时挂载即启动失败,
# 打包遗漏会在测试/启动第一时间暴露)
_PANEL_DIR = Path(__file__).resolve().parent / "static"

# 直通通道上传白名单:MAPDL 文本类输入(入口 .inp/.mac、模型 .cdb、数据 .csv/.txt);
# 可执行/二进制格式一律拒绝
APDL_UPLOAD_SUFFIXES = frozenset({".inp", ".cdb", ".mac", ".csv", ".txt"})
UPLOAD_ID_TOKEN_BYTES = 6
UPLOAD_CHUNK_BYTES = 1024 * 1024
# 单文件上传字节上限(防未鉴权磁盘填充;.cdb 大网格留足余量)。
# 刻意不入 service.yaml:这是代码级护栏,不随部署配置放宽
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
LOG_TAIL_MAX_LINES = 10_000
ARTIFACT_FORBIDDEN_PATTERNS = ("/", "\\", "..")
# /jobs/{id}/log 的日志源白名单:精确匹配即防穿越/防读根部簿记文件
# (job.out 运行中只在作业目录根部,内核结束/失败后才发布进 artifacts/)
LOG_SOURCE_WHITELIST = frozenset({LOG_FILENAME, OUT_FILENAME})


def _tail_query() -> Any:
    """tail 参数共用工厂(两个日志端点同形:1 ≤ N ≤ LOG_TAIL_MAX_LINES,缺省全文)。"""
    return Query(
        default=None, ge=1, le=LOG_TAIL_MAX_LINES,
        description="只取最后 N 行;缺省返回全文(文件超 2MB 自动截尾 2000 行,"
                    "响应带 X-Log-Truncated: true)",
    )


def _log_response(path: Path, tail: int | None) -> PlainTextResponse:
    """日志端点共用出口:read_log_view 读取,截尾时带 X-Log-Truncated 头。"""
    text, truncated = read_log_view(path, tail)
    return PlainTextResponse(
        text,
        media_type="text/plain",
        headers={"X-Log-Truncated": "true"} if truncated else None,
    )

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
    """装配应用:配置 → 队列 → 路由。"""
    resolved_settings = settings if settings is not None else load_settings()
    queue = JobQueue(resolved_settings)
    _ensure_storage_dirs(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if resolved_settings.passthrough.enabled:
            logger.warning(
                "passthrough 直通通道已开启(config passthrough.enabled=true):"
                "任意 APDL 输入将直接交 MAPDL 执行(APDL 可读写文件、起系统命令),"
                "仅限受控内网 + 明确信任上游"
            )
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
    # 请求访问日志(独立完整服务日志):先配置 logger 再挂中间件,全部请求落盘
    configure_access_logging(resolved_settings)
    # 开关式 CORS(server.cors_origins,默认空 = 不挂,行为不变):供前端页面
    # (如 hip-playback 回放组件)跨域拉取工件;只放行 GET(只读端点足够)。
    # 注意挂载顺序:add_middleware 是前插(insert(0)),后挂者在外层 —— 访问日志
    # 必须最后挂,预检 OPTIONS 才会被 CORSMiddleware 短路之前先落日志
    # (实测:顺序反了预检不落日志,违背"全部请求一字不漏"承诺)。
    if resolved_settings.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.server.cors_origins),
            allow_methods=["GET"],
        )
    app.middleware("http")(make_access_log_middleware())
    app.include_router(_health_router(resolved_settings, queue))
    # sim 路由不统一打 tags:提交端点按方法组(注册表 GROUP_LABELS)折叠展示
    # (passthrough 手写路由,见 _sim_router)
    app.include_router(_sim_router(resolved_settings, queue), prefix="/sim")
    app.include_router(_jobs_router(queue), prefix="/jobs", tags=["jobs"])
    app.include_router(_uploads_router(resolved_settings), prefix="/uploads", tags=["uploads"])
    # 运维面板(静态单页,挂载在 API 路由之后:既有 API 路径不受影响)
    app.mount("/panel", StaticFiles(directory=_PANEL_DIR, html=True), name="panel")
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
            passthrough_enabled=settings.passthrough.enabled,
            version=__version__,
        )

    @router.get(
        "/service/log",
        response_class=PlainTextResponse,
        summary="服务请求日志(尾部)",
    )
    def get_service_log(tail: int | None = _tail_query()) -> PlainTextResponse:
        """请求访问日志(var/logs/access.log,按天午夜轮转保留 14 天)尾部
        N 行;缺省全文(超 2MB 自动截尾 2000 行,带 X-Log-Truncated 头)。"""
        return _log_response(settings.access_log_path, tail)

    @router.get("/", include_in_schema=False)
    @router.get("/panel", include_in_schema=False)
    def redirect_to_panel() -> RedirectResponse:
        """根路径与 /panel(无尾斜杠)→ 307 至 /panel/(显式相对 Location —
        框架自动斜杠重定向给绝对 URL,反向代理下可能拼错主机)。"""
        return RedirectResponse(url="/panel/")

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

_SUBMIT_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": ErrorBody, "description": "参数校验失败(INVALID_PARAMS)"},
    501: {"model": ErrorBody, "description": "内核尚未实现(METHOD_NOT_IMPLEMENTED)"},
    503: {"model": ErrorBody, "description": "方法已临时下线(METHOD_DISABLED)"},
}

# 直通端点专用:在共用错误响应之上补 403(开关关闭;区别于 404/503,
# 上游可据此区分"端点不存在"与"策略性关闭")
_PASSTHROUGH_SUBMIT_RESPONSES: dict[int, dict[str, Any]] = {
    **_SUBMIT_RESPONSES,
    403: {"model": ErrorBody, "description": "直通通道未开启(PASSTHROUGH_DISABLED)"},
}

# 提交端点的 Swagger 请求示例(值一律取自真实 e2e/测试用例,不编造)
_PASSTHROUGH_ROUTE_EXAMPLES: dict[str, dict[str, Any]] = {
    "直通执行入口 inp": {
        "summary": "上传的入口 .inp + 附属 .cdb,声明产出与超时(test_passthrough 用例)",
        "value": {
            "params": {
                "entry_file": "/var/uploads/aB3xK9_job.inp",
                "extra_files": ["/var/uploads/cD9mQ2_capsule.cdb"],
                "declared_outputs": ["final.cdb", "results.csv"],
                "timeout_s": 7200,
                "workflow": "hip-demo/1.2",
            }
        },
    },
}


def _passthrough_route_description(spec: registry.MethodSpec) -> str:
    """直通端点 description:安全前提/调用流程/结果形态(取自 REGISTRY 单一事实来源)。"""
    lines = [
        spec.summary,
        "",
        f"- 返回:`{spec.returns}`",
        f"- 典型耗时:{spec.typical_runtime}",
        "- 安全前提:开启即暴露任意 APDL 执行面,仅限受控内网 + 明确信任上游;"
        "关闭时本端点回 `403 PASSTHROUGH_DISABLED`。",
        "- 调用流程:入口/附属文件先经 `POST /uploads/apdl` 上传,再以服务端路径"
        "引用;执行结束后声明输出缺失 → 作业 failed(`ARTIFACT_NOT_FOUND`)。",
        "- `workflow` 为纯溯源标注(落盘 resolved-params.json),服务不据此分支。",
    ]
    return "\n".join(lines)


def _examples_openapi_extra(
    examples: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """把命名示例包成 openapi_extra 的 requestBody 结构(无示例 → None)。"""
    if not examples:
        return None
    return {"requestBody": {"content": {"application/json": {"examples": examples}}}}


def _sim_router(settings: Settings, queue: JobQueue) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/methods",
        response_model=list[dict[str, Any]],
        summary="方法自描述清单(passthrough 单方法)",
        tags=["sim"],
    )
    def list_methods() -> list[dict[str, Any]]:
        """按名排序的全部方法元数据,含方法参数模型的 JSON Schema。"""
        return registry.methods_payload()

    # 直通通道(手写类型化路由,Swagger 可见):请求体 = PassthroughSimRequest{params},
    # 与 HIPForm AnsysClient.submit(method, params) 发送的 {"params": {...}} 同形。
    # 开闸检查在 _submit 管线内(泛化路径同样生效)。
    passthrough_spec = registry.REGISTRY[registry.PASSTHROUGH_METHOD]

    @router.post(
        "/passthrough",
        response_model=SimAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        summary=passthrough_spec.summary,
        description=_passthrough_route_description(passthrough_spec),
        tags=[registry.GROUP_LABELS[passthrough_spec.group]],
        responses=_PASSTHROUGH_SUBMIT_RESPONSES,
        openapi_extra=_examples_openapi_extra(_PASSTHROUGH_ROUTE_EXAMPLES),
    )
    def submit_passthrough(request: PassthroughSimRequest) -> SimAccepted:
        """提交直通作业:入口 APDL 原样交 MAPDL 执行,服务只治理作业/超时/工件。"""
        # exclude_unset + mode="json":只取显式给出的字段,dump 与泛化路由
        # 收到的原生 JSON dict 完全同型。
        inline = (
            request.params.model_dump(exclude_unset=True, mode="json")
            if request.params is not None
            else None
        )
        return _submit(passthrough_spec, inline, settings, queue)

    # 泛化兜底(后注册,不在 Swagger 展示):已知方法已被上面的手写路由截获,
    # 本处理器实际只对未知方法名可达(404);端点保留是为维持既有 URL 形态 —
    # HIPForm 对已知方法的调用由手写端点经同一条 _submit 管线等价服务。
    @router.post(
        "/{method}",
        response_model=SimAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        summary="提交仿真作业(泛化兜底,不在 Swagger 展示)",
        tags=["sim"],
        include_in_schema=False,
        responses=_SUBMIT_RESPONSES,
    )
    def submit_simulation(
        method: str = PathParam(description="方法名(可用方法见 GET /sim/methods)"),
        request: SimRequest = ...,
    ) -> SimAccepted:
        """提交作业并入队;参数经方法参数模型校验后入队。"""
        spec = registry.get_method(method)
        if spec is None:
            raise ApiError(
                404, "METHOD_NOT_FOUND",
                f"未知方法 '{method}';可用方法见 GET /sim/methods",
            )
        return _submit(spec, request.params, settings, queue)

    return router


def _require_passthrough_enabled(settings: Settings) -> None:
    """passthrough 直通关闸(提交与上传两面共用的 403 门控与文案)。"""
    if not settings.passthrough.enabled:
        raise ApiError(
            403, "PASSTHROUGH_DISABLED",
            "直通通道未开启(config/service.yaml passthrough.enabled=false;"
            "安全前提:开启即暴露任意 APDL 执行面)",
        )


def _submit(
    spec: registry.MethodSpec,
    inline_params: dict[str, Any] | None,
    settings: Settings,
    queue: JobQueue,
) -> SimAccepted:
    """两套提交入口共用的校验+入队管线。

    顺序:403 直通关闸(先于一切 — 策略性关闭不泄露后续任何校验行为)→
    503 下线 → 501 未实现 → 400 参数(passthrough 随后附加上传目录限定);
    手写路由的"未知方法"在路由匹配层就不可达,由泛化兜底 404。
    """
    if spec.name == registry.PASSTHROUGH_METHOD:
        _require_passthrough_enabled(settings)
    if spec.name in settings.methods.disabled:
        raise ApiError(
            503, "METHOD_DISABLED",
            f"方法 '{spec.name}' 已被临时下线(config/service.yaml methods.disabled)",
        )
    executor = registry.resolve_executor(spec.name)
    if executor is None:
        raise ApiError(501, "METHOD_NOT_IMPLEMENTED", f"方法 '{spec.name}' 的内核尚未实现")
    params = _resolve_params(spec, inline_params)
    if spec.name == registry.PASSTHROUGH_METHOD:
        _validate_passthrough_uploads(params, settings)
    record = queue.submit(spec=spec, params=params, part=None, executor=executor)
    return SimAccepted(
        id=record.id,
        method=spec.name,
        status=record.status,
        fidelity=record.fidelity,
        status_url=f"/jobs/{record.id}",
    )


def _resolve_params(
    spec: registry.MethodSpec,
    inline_params: dict[str, Any] | None,
) -> BaseModel:
    """方法参数模型校验(内联 params,无配置级合并);
    失败 → 400 INVALID_PARAMS(含错误摘要)。"""
    if inline_params:
        unknown = sorted(
            key for key in inline_params if key not in spec.params_model.model_fields
        )
        if unknown:
            raise ApiError(
                400, "INVALID_PARAMS",
                f"方法 '{spec.name}' 不存在参数字段: {unknown};合法字段见 GET /sim/methods",
            )
    try:
        return spec.params_model.model_validate(inline_params or {})
    except ValidationError as exc:
        raise ApiError(400, "INVALID_PARAMS", _validation_summary(exc)) from exc


def _validate_passthrough_uploads(params: PassthroughParams, settings: Settings) -> None:
    """直通参数的 API 边界限定:entry/extra 必须位于上传目录内。

    这些文件会被复制进作业目录根部并作为工件发布 — 若放开任意服务端路径,
    即构成"任意可读文件 → 工件下载"的外泄面。resolve 后做包含性判定
    (与 _resolve_artifact_path 同模式,符号链接越界同样被挡)。
    """
    uploads_root = settings.uploads_root
    labeled = [
        ("entry_file", params.entry_file),
        *((f"extra_files[{index}]", path) for index, path in enumerate(params.extra_files)),
    ]
    for label, path_text in labeled:
        if uploads_root not in Path(path_text).resolve().parents:
            raise ApiError(
                400, "INVALID_PARAMS",
                f"{label} 必须位于上传目录内(先经 POST /uploads/apdl 上传): {path_text}",
            )
    # 声明输出与输入同名会被复制进根部的输入"自我满足",缺件检查被短路
    # (MAPDL 零产出也算 succeeded)→ 显式拒绝;交集计算由内核模块导出
    # (与 _stage_inputs 的 basename 复制语义同源,懒加载同 registry 内核口径)
    from .kernels.passthrough import declared_output_overlap  # noqa: PLC0415

    overlapped = declared_output_overlap(params)
    if overlapped:
        raise ApiError(
            400, "INVALID_PARAMS",
            f"declared_outputs 不得与入口/附属文件同名(否则零产出也会通过检查): {overlapped}",
        )


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

    def require_job(job_id: str, *, include_stages: bool = True) -> JobSnapshot:
        """取作业快照(内存实时;服务重启后回退盘上历史只读视图)。

        include_stages=False:不需要 stages 的端点(日志/工件/结果/删除)
        跳过 progress.csv 的读盘与逐行解析。
        """
        snapshot = queue.snapshot(job_id, include_stages=include_stages)
        if snapshot is None:
            raise ApiError(
                404, "JOB_NOT_FOUND",
                f"作业 '{job_id}' 不存在(可能已删除或超出保留期被清扫)",
            )
        return snapshot

    @router.get("", response_model=list[JobState], summary="作业列表(队列 + 历史)")
    def list_jobs() -> list[JobState]:
        """内存活跃(实时,含 stages 投影)+ 盘上历史(state.json 只读末帧),
        按 (created_at, id) 倒序;服务重启后历史作业仍可见。"""
        return queue.list_jobs()

    @router.get("/{job_id}", response_model=JobState, summary="查询作业状态")
    def get_job(job_id: str = PathParam(description="作业 ID(受理响应的 id)")) -> JobState:
        """作业状态机快照:pending → running → succeeded/failed/cancelled。"""
        return require_job(job_id).state

    @router.get(
        "/{job_id}/log",
        response_class=PlainTextResponse,
        summary="作业日志(支持 tail 与日志源切换)",
    )
    def get_job_log(
        job_id: str = PathParam(description="作业 ID"),
        tail: int | None = _tail_query(),
        source: str = Query(
            default=LOG_FILENAME,
            description="日志源:job.log(状态事件)/ job.out(MAPDL 求解输出;"
                        "运行中仅存在于作业目录根部,可实时查看)",
        ),
    ) -> PlainTextResponse:
        """job.log / job.out 全文;?tail=N 只取最后 N 行(缺省全文对超 2MB
        文件自动截尾 2000 行,响应带 X-Log-Truncated 头)。"""
        snapshot = require_job(job_id, include_stages=False)
        if source not in LOG_SOURCE_WHITELIST:
            raise ApiError(
                400, "INVALID_PARAMS",
                f"不支持的日志源 '{source}';允许: {sorted(LOG_SOURCE_WHITELIST)}",
            )
        return _log_response(snapshot.job_dir / source, tail)

    @router.get(
        "/{job_id}/result",
        summary="取结果 JSON",
        responses={
            404: {"model": ErrorBody, "description": "作业不存在"},
            409: {"model": ErrorBody, "description": "未完成/已取消/已失败,无结果"},
        },
    )
    def get_job_result(job_id: str = PathParam(description="作业 ID")) -> Response:
        """内核返回的结果 JSON;仅 succeeded 状态可取。"""
        snapshot = require_job(job_id, include_stages=False)
        if snapshot.state.status in (JobStatusEnum.PENDING, JobStatusEnum.RUNNING):
            raise ApiError(
                409, "RESULT_NOT_READY",
                f"作业尚未完成(当前状态 {snapshot.state.status.value})",
            )
        if snapshot.state.status is JobStatusEnum.CANCELLED:
            raise ApiError(409, "RESULT_NOT_READY", "作业已取消,无结果")
        if snapshot.state.status is JobStatusEnum.FAILED:
            error = snapshot.state.error or ErrorBody(
                code="INTERNAL", message="未知失败原因"
            )
            raise ApiError(409, "JOB_FAILED", f"[{error.code}] {error.message}")
        result_path = snapshot.job_dir / RESULT_FILENAME
        if not result_path.is_file():
            raise ApiError(500, "INTERNAL", f"result.json 缺失: {result_path}")
        # 已落盘的 result.json 原样直通,省一轮 parse→serialize(纯搬运)
        return Response(
            content=result_path.read_text(encoding="utf-8"),
            media_type="application/json",
        )

    @router.get(
        "/{job_id}/artifacts",
        response_model=list[str],
        summary="列出工件文件",
        responses={404: {"model": ErrorBody, "description": "作业不存在"}},
    )
    def list_artifacts(job_id: str = PathParam(description="作业 ID")) -> list[str]:
        """作业 artifacts/ 目录内的工件文件名(按名排序),供 /artifacts/{name} 下载。"""
        snapshot = require_job(job_id, include_stages=False)
        artifacts_dir = snapshot.job_dir / ARTIFACTS_DIRNAME
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
    def get_artifact(
        job_id: str = PathParam(description="作业 ID"),
        name: str = PathParam(description="工件文件名(取自 GET /jobs/{id}/artifacts)"),
    ) -> FileResponse:
        """下载作业 artifacts/ 目录内的工件(名称禁止路径分隔符与 '..')。"""
        snapshot = require_job(job_id, include_stages=False)
        artifact_path = _resolve_artifact_path(snapshot.job_dir, name)
        return FileResponse(artifact_path, filename=name)

    @router.delete(
        "/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="取消并删除作业",
        responses={404: {"model": ErrorBody, "description": "作业不存在"}},
    )
    def delete_job(job_id: str = PathParam(description="作业 ID")) -> Response:
        """pending/running 先取消(运行中会防御性终止内核),再清理作业目录
        (含服务重启后的盘上历史作业目录)。"""
        snapshot = require_job(job_id, include_stages=False)
        if snapshot.state.status in ACTIVE_STATUSES:
            queue.cancel(job_id)
        queue.remove(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/{job_id}/cancel",
        response_model=JobState,
        summary="取消/强制中断作业(保留现场)",
        responses={404: {"model": ErrorBody, "description": "作业不存在"}},
    )
    def cancel_job(job_id: str = PathParam(description="作业 ID")) -> JobState:
        """强制中断 pending/running 作业:终止 MAPDL 进程组(SIGTERM → 宽限 →
        SIGKILL)并置 cancelled,响应返回即进程树已死。与 DELETE 的区别:
        **不清理作业目录** — state.json/job.log/job.out 全部保留供排障
        (NERR 失控、进程挂死等事故的现场取证)。终态作业幂等返回当前状态。"""
        require_job(job_id, include_stages=False)
        queue.cancel(job_id)
        return require_job(job_id).state

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
# uploads:APDL 上传(直通通道入口与附属文件)
# ---------------------------------------------------------------------------

def _uploads_router(settings: Settings) -> APIRouter:
    uploads_root = settings.uploads_root
    router = APIRouter()

    @router.post(
        "/apdl",
        response_model=UploadAccepted,
        summary="上传 APDL 文件(直通通道入口/附属)",
        responses={
            400: {"model": ErrorBody, "description": "扩展名不允许/文件名非法(INVALID_PARAMS)"},
            403: {"model": ErrorBody, "description": "直通通道未开启(PASSTHROUGH_DISABLED)"},
            413: {"model": ErrorBody, "description": "超过单文件上传上限(PAYLOAD_TOO_LARGE)"},
        },
    )
    async def upload_apdl(
        file: UploadFile = File(
            ..., description="APDL 文件,扩展名 .inp/.cdb/.mac/.csv/.txt"
        ),
    ) -> UploadAccepted:
        """multipart 上传 → var/uploads/<id>_<原名>;返回服务端绝对路径供
        `POST /sim/passthrough` 的 entry_file / extra_files 引用(白名单只收
        MAPDL 文本类输入,可执行/二进制格式拒绝)。与提交端点同受
        passthrough 开关门控(关闭时 403,不留旁路上传面)。"""
        _require_passthrough_enabled(settings)
        return await _accept_upload(file, uploads_root, APDL_UPLOAD_SUFFIXES, kind="APDL")

    return router


async def _accept_upload(
    upload: UploadFile,
    uploads_root: Path,
    allowed_suffixes: frozenset[str],
    *,
    kind: str,
) -> UploadAccepted:
    """上传落盘共用管线:消毒文件名 → 后缀白名单 → <token>_<原名> 写入上传目录。"""
    original_name = Path(upload.filename or "").name  # 消毒:去掉任何路径部分
    suffix = Path(original_name).suffix.lower()
    # 不可打印字符(如 NUL)过白名单后会让 open() 抛 ValueError → 500,在此先拒
    is_printable = original_name and all(c.isprintable() for c in original_name)
    if not is_printable or suffix not in allowed_suffixes:
        shown = upload.filename or "(未提供文件名)"
        raise ApiError(
            400, "INVALID_PARAMS",
            f"不支持的文件 '{shown}';允许的扩展名: {sorted(allowed_suffixes)}",
        )
    target = uploads_root / f"{secrets.token_urlsafe(UPLOAD_ID_TOKEN_BYTES)}_{original_name}"
    size_bytes = await _save_upload(upload, target)
    logger.info("%s上传: %s(%d 字节)", kind, target, size_bytes)
    return UploadAccepted(path=str(target.resolve()), size_bytes=size_bytes)


async def _save_upload(upload: UploadFile, target: Path) -> int:
    """分块写出上传内容并关闭句柄;超上限中断并清掉半写文件,返回字节数。"""
    size_bytes = 0
    oversize = False
    try:
        with target.open("wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_BYTES:
                    oversize = True
                    break
                out.write(chunk)
    finally:
        await upload.close()
        if oversize:
            target.unlink(missing_ok=True)  # 半写文件不留守卫磁盘
    if oversize:
        raise ApiError(
            413, "PAYLOAD_TOO_LARGE",
            f"上传超过单文件上限 {MAX_UPLOAD_BYTES} 字节: '{target.name}'",
        )
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
