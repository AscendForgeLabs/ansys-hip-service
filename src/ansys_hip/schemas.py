"""接口契约(冻结版)— 所有请求/响应的 pydantic 模型.

服务已收敛为 passthrough 单通道(类型化方法库移除):参数模型仅剩
PassthroughParams;作业/健康/上传响应模型不变。
每个字段的 description/examples 直接成为 Swagger 文档 — 请写清楚。

单位约定:mm / MPa / s / ℃。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 通用构件
# ---------------------------------------------------------------------------

class Fidelity(str, Enum):
    """结果保真度:passthrough = 忠实转发(服务不背书物理内容,内容由
    上游 .inp 作者负责);smoke = 占位本构的冒烟结果(历史类型化方法口径)。"""

    REAL = "real"
    SMOKE = "smoke"
    PASSTHROUGH = "passthrough"


class JobStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorBody(BaseModel):
    code: str = Field(..., description="机器可读错误码", examples=["LICENSE_UNAVAILABLE"])
    message: str = Field(..., description="人读错误说明(含 MAPDL job.out 关键行)")


# 错误码约定(全服务统一):
#   INVALID_PARAMS / METHOD_DISABLED
#   MAPDL_NOT_FOUND / LICENSE_UNAVAILABLE / CONVERGENCE_FAILED / TIMEOUT / INTERNAL
#   PASSTHROUGH_DISABLED(403,直通通道未开启:与 404 端点不存在、503 方法下线区分)
#   ARTIFACT_NOT_FOUND(404 工件下载缺件;passthrough 声明输出缺失时内核复用此码)
#   PART_NOT_FOUND / GEOMETRY_NOT_FOUND — 已无产生路径(类型化方法与零件配置级已移除),
#   字符串保留仅为历史作业 error 回放与上游兼容,新代码不得再抛出


# ---------------------------------------------------------------------------
# 方法参数模型(方法注册表引用;字段即 Swagger 输入)
# ---------------------------------------------------------------------------

class MethodParamsBase(BaseModel):
    """方法参数模型共享基类:未知顶层键一律拒绝(400 INVALID_PARAMS)。

    手写路由(POST /sim/passthrough)靠它在请求体解析层直接挡掉拼错的
    字段名,不被静默吞掉;泛化路由 _resolve_params 的手工 unknown-key
    检查保留为防御(已知方法流量已被手写路由截获,该分支生产不可达,
    直调单测钉住)。
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 直通通道(方法 passthrough):上游自带 APDL 输入直接交 MAPDL 执行
# ---------------------------------------------------------------------------

# 作业目录根部的簿记文件/目录名:entry/extra 复制进根部、declared_outputs 从根部
# 发布,均不得占用(否则覆盖 state/result 簿记或劫持 progress.csv 阶段进度侧车)
RESERVED_JOB_DIR_NAMES = frozenset({
    "state.json", "resolved-params.json", "result.json", "job.log",
    "job.out", "launcher.log", "progress.csv", "artifacts",
})

# 声明输出数量上限(防一次性发布海量文件刷屏 artifacts 清单)
PASSTHROUGH_MAX_DECLARED_OUTPUTS = 64


def _validate_bare_filename(value: str, field_label: str) -> str:
    """裸文件名规则(沿用 uploads 消毒风格):非空、无路径分隔符、
    非 '.'/'..'、不含不可打印字符;服务簿记保留名拒绝。

    抛 ValueError(pydantic 包装为校验错误 → 400 INVALID_PARAMS)。
    """
    is_illegal = (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(not character.isprintable() for character in value)
    )
    if is_illegal:
        raise ValueError(
            f"{field_label} 必须是裸文件名(禁止路径分隔符/'..'/不可打印字符): '{value}'"
        )
    if value in RESERVED_JOB_DIR_NAMES:
        raise ValueError(f"{field_label} '{value}' 是作业目录保留名,不可占用")
    return value


class PassthroughParams(MethodParamsBase):
    """直通通道参数(方法 passthrough,需 config passthrough.enabled=true)。

    入口与附属文件先经 POST /uploads/apdl 上传,此处引用服务端路径;
    服务按原名复制进作业目录根部后以 MAPDL 批处理执行(入口内的相对引用
    自然解析)。服务不做任何仿真逻辑,只治理作业/队列/超时/工件。
    """

    entry_file: str = Field(
        ...,
        min_length=1,
        description="APDL 入口文件的服务端路径(POST /uploads/apdl 返回的 path);以 MAPDL -i 直接执行,须位于上传目录内",
        examples=["/var/uploads/aB3xK9_job.inp"],
    )
    extra_files: list[str] = Field(
        default_factory=list,
        description="附属文件的服务端路径(.cdb/.mac/.csv/.txt 等);按原名复制进作业目录根部,供入口 /INPUT、*GET 等相对引用",
        examples=[["/var/uploads/cD9mQ2_capsule.cdb"]],
    )
    declared_outputs: list[str] = Field(
        ...,
        min_length=1,
        max_length=PASSTHROUGH_MAX_DECLARED_OUTPUTS,
        description="执行结束后必须存在的输出文件名(裸文件名,写在作业目录根部,如 final.cdb);缺失 → 作业 failed(ARTIFACT_NOT_FOUND);重复条目去重(保序)",
        examples=[["final.cdb", "results.csv"]],
    )
    timeout_s: int | None = Field(
        default=None, gt=0,
        description="本作业执行超时(秒);None 用全局 ansys.job_timeout_s;实际生效值 = min(本值, 全局上限)",
        examples=[7200],
    )
    workflow: str | None = Field(
        default=None,
        description="纯溯源标注(上游工作流名/版本号);仅随参数落盘 resolved-params.json,服务不据此分支",
        examples=["hip-demo/1.2"],
    )

    @field_validator("declared_outputs")
    @classmethod
    def _dedupe_declared_outputs(cls, value: list[str]) -> list[str]:
        """逐条按裸文件名校验(含保留名),再保序去重(归一结果落盘可追溯)。"""
        validated = [
            _validate_bare_filename(name, "declared_outputs 条目") for name in value
        ]
        return list(dict.fromkeys(validated))

    @model_validator(mode="after")
    def _validate_file_basenames(self) -> "PassthroughParams":
        """entry/extra 的 basename 同受保留名约束(复制进根部会占用同名簿记文件)。"""
        for label, paths in (
            ("entry_file", (self.entry_file,)),
            ("extra_files 条目", self.extra_files),
        ):
            for path in paths:
                _validate_bare_filename(Path(path).name, label)
        return self


# ---------------------------------------------------------------------------
# 请求/响应信封
# ---------------------------------------------------------------------------

class SimRequest(BaseModel):
    """POST /sim/{method} 泛化兜底请求体(仅未知方法到达此路由 → 404;
    已知方法被手写路由截获,模型仅保底解析)。

    内联参数经方法参数模型校验后写入 job 目录 resolved-params.json,保证可追溯。
    """

    model_config = ConfigDict(extra="allow")

    params: dict[str, Any] | None = Field(
        default=None,
        description="内联参数(对应方法参数模型的顶层字段)",
    )


class PassthroughSimRequest(BaseModel):
    """POST /sim/passthrough 请求体:{params}(无 part 概念 — 直通不引用零件配置)。

    HIPForm AnsysClient.submit(method, params) 发送的正是 {"params": {...}},
    该形态对本端点零改动可用;extra=forbid 防顶层键拼错被静默吞掉
    (params 缺失由下游统一 400 INVALID_PARAMS)。
    """

    model_config = ConfigDict(extra="forbid")

    params: PassthroughParams | None = Field(
        default=None,
        description="直通通道参数(必给;字段与校验规则见 PassthroughParams)",
    )


class SimAccepted(BaseModel):
    """作业受理响应(202)。"""

    id: str = Field(..., description="作业 ID", examples=["9d2f..."])
    method: str = Field(..., description="方法名", examples=["passthrough"])
    status: JobStatusEnum = Field(default=JobStatusEnum.PENDING, description="受理后固定为 pending")
    fidelity: Fidelity | None = Field(
        default=None,
        description="方法标称保真度:passthrough=忠实转发(服务不背书物理内容) / "
                    "real=真实内核 / smoke=占位本构冒烟(历史口径)",
    )
    status_url: str = Field(..., description="轮询地址(GET /jobs/{id})", examples=["/jobs/9d2f..."])


class JobStage(BaseModel):
    """作业阶段进度条目(progress.csv 侧车的读时投影)。"""

    model_config = ConfigDict(frozen=True)

    label: str = Field(
        ...,
        min_length=1,
        max_length=8,
        description="阶段短标签(APDL *VWRITE 字符字面量 ≤8 字符约束,如 HEAT/HOLD/COOL)",
        examples=["HEAT"],
    )
    time_s: float = Field(
        ...,
        ge=0,
        description="该阶段完成时刻的累计耗时(秒)",
        examples=[3600],
    )


class JobState(BaseModel):
    """GET /jobs/{id} 响应。"""

    id: str = Field(..., description="作业 ID")
    method: str = Field(..., description="方法名")
    part: str | None = Field(default=None, description="兼容保留字段:恒为 None(零件配置级已随类型化方法移除)")
    status: JobStatusEnum = Field(..., description="状态机:pending → running → succeeded / failed / cancelled")
    fidelity: Fidelity | None = Field(
        default=None,
        description="结果保真度:passthrough=忠实转发(服务不背书物理内容) / "
                    "real=真实内核 / smoke=占位本构冒烟(历史口径)",
    )
    created_at: str = Field(..., description="受理时刻(ISO 8601)")
    started_at: str | None = Field(default=None, description="开始执行时刻;排队中为 None")
    finished_at: str | None = Field(default=None, description="结束时刻;未结束为 None")
    error: ErrorBody | None = Field(
        default=None, description="失败原因(仅 failed 时非空;含机器可读错误码)"
    )
    log_url: str | None = Field(default=None, description="日志端点(GET /jobs/{id}/log,纯文本)")
    result_url: str | None = Field(default=None, description="结果端点(GET /jobs/{id}/result;仅 succeeded 可取)")
    artifacts_url: str | None = Field(
        default=None, description="工件列表端点(GET /jobs/{id}/artifacts,含下载链接拼法)"
    )
    stages: list[JobStage] | None = Field(
        default=None,
        description="已完成阶段序列(progress.csv 侧车读时投影):pending 或无侧车的作业为 "
                    "None(如全部既有方法),running 期间实时反映,终态为末帧快照;"
                    "当前阶段与百分比由客户端按序列派生,服务不猜测",
        examples=[[{"label": "HEAT", "time_s": 3600}, {"label": "HOLD", "time_s": 7200}]],
    )


class HealthReport(BaseModel):
    """GET /health 响应。"""

    status: Literal["ok", "degraded"] = Field(
        ..., description="ok=MAPDL 与许可齐备;degraded=缺任一(此时提交仿真作业会失败)"
    )
    mapdl_found: bool = Field(..., description="MAPDL 可执行文件存在且具可执行位")
    license_env_set: bool = Field(..., description="许可已配置(.lic 文件存在,或 port@host 形式)")
    queue_running: int = Field(..., description="正在运行的作业数")
    queue_pending: int = Field(..., description="排队等待的作业数")
    passthrough_enabled: bool = Field(
        ...,
        description="直通通道开关(config passthrough.enabled);关闭时 POST /sim/passthrough 回 403 PASSTHROUGH_DISABLED",
    )
    version: str = Field(..., description="服务版本号")


class UploadAccepted(BaseModel):
    """POST /uploads/apdl(multipart)响应 — 返回服务端路径供直通参数引用。"""

    path: str = Field(..., description="服务端保存路径(填入 passthrough 参数的 entry_file / extra_files 引用)")
    size_bytes: int = Field(..., description="文件大小(字节)")


# ---------------------------------------------------------------------------
# 内核执行契约
# ---------------------------------------------------------------------------

class RunContext(BaseModel):
    """队列调用内核时的上下文(不可变)。

    内核签名约定(注册到 registry 的 executor):
        def run_<method>(params: <Method>Params, ctx: RunContext) -> dict
    - 同步函数,队列在线程池中运行;
    - 返回 dict = result.json 内容(须含 "fidelity" 键);
    - 工件文件写到 ctx.job_dir,并在返回 dict 的 "artifacts": [文件名...];
    - 抛 KernelError(code, message) → 作业 failed,error 原样返回。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_dir: Path          # 作业目录(日志/工件/解析后参数都放这里)
    ansys_bin: str
    license_file: str
    ansys_np: int
    job_timeout_s: int
