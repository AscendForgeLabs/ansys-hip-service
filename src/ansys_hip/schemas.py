"""接口契约(冻结版)— 所有请求/响应的 pydantic 模型.

字段命名与 HIPForm config YAML 对齐(cycle/materials/mesh/numerics),便于 HIPForm 侧映射。
每个字段的 description/examples 直接成为 Swagger 文档 — 请写清楚。

单位约定:mm / MPa / s / ℃(与 docs/materials-process.md 一致)。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 通用构件
# ---------------------------------------------------------------------------

class CyclePoint(BaseModel):
    """工艺曲线上的一个时刻(分段线性,按 time_s 升序)。"""

    time_s: float = Field(..., ge=0, description="时刻(秒)", examples=[3600])
    temperature_c: float = Field(..., ge=0, le=2500, description="温度(℃)", examples=[900])
    pressure_mpa: float = Field(..., ge=0, le=500, description="压力(MPa)", examples=[120])


class Cycle(BaseModel):
    """HIP 工艺曲线(温度/压力-时间历程)。"""

    points: list[CyclePoint] = Field(
        ...,
        min_length=2,
        description="按时间升序的控制点;相邻点间线性过渡",
        examples=[[
            {"time_s": 0, "temperature_c": 20, "pressure_mpa": 0},
            {"time_s": 3600, "temperature_c": 900, "pressure_mpa": 120},
            {"time_s": 14400, "temperature_c": 900, "pressure_mpa": 120},
            {"time_s": 18000, "temperature_c": 20, "pressure_mpa": 0},
        ]],
    )


class MaterialSelection(BaseModel):
    """材料选定与覆盖(引用 material_lib,可逐参数覆盖)。"""

    powder: str = Field(default="tc4", description="粉末材料名(material_lib 键)", examples=["tc4"])
    capsule: str = Field(default="20steel", description="包套材料名", examples=["20steel"])
    overrides: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="按材料名覆盖参数,例 {tc4: {yield_stress_mpa: 800}}",
        examples=[{"tc4": {"yield_stress_mpa": 800}}],
    )


class MeshSettings(BaseModel):
    """网格参数。"""

    mesh_size_mm: float = Field(default=6.0, gt=0, description="特征单元尺寸(mm)", examples=[6.0])


class Numerics(BaseModel):
    """数值求解参数。"""

    time_step_s: float | None = Field(
        default=None, gt=0,
        description="时间步长(秒);None 用内核默认(曲线总长/120)",
        examples=[120],
    )


class GeometryRef(BaseModel):
    """几何引用(绝对路径;内网同机/共享盘部署时直接路径引用)。"""

    capsule_step: str | None = Field(
        default=None,
        description="包套 STEP 绝对路径(或先 POST /uploads 上传取得路径)",
        examples=["/home/yushen/tempt/capsule.step"],
    )
    cavity_step: str | None = Field(
        default=None,
        description="目标型腔 STEP 绝对路径",
        examples=["/home/yushen/tempt/cavity.step"],
    )


class Fidelity(str, Enum):
    """结果保真度:smoke = 真实几何+占位本构的冒烟结果(阶段 2 升级,API 不变)。"""

    REAL = "real"
    SMOKE = "smoke"


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
#   INVALID_PARAMS / PART_NOT_FOUND / GEOMETRY_NOT_FOUND / METHOD_DISABLED
#   MAPDL_NOT_FOUND / LICENSE_UNAVAILABLE / CONVERGENCE_FAILED / TIMEOUT / INTERNAL
#   PASSTHROUGH_DISABLED(403,直通通道未开启:与 404 端点不存在、503 方法下线区分)
#   ARTIFACT_NOT_FOUND(404 工件下载缺件;passthrough 声明输出缺失时内核复用此码)


# ---------------------------------------------------------------------------
# 各方法参数模型(方法注册表引用;字段即 Swagger 输入)
# ---------------------------------------------------------------------------

class MethodParamsBase(BaseModel):
    """方法参数模型共享基类:未知顶层键一律拒绝(400 INVALID_PARAMS)。

    类型化路由(POST /sim/{name})靠它在请求体解析层直接挡掉拼错的字段名,
    不被静默吞掉;泛化路由 _resolve_params 的手工 unknown-key 检查保留为防御
    (已知方法流量已被类型化路由截获,该分支生产不可达,直调单测钉住)。
    嵌套构件(Cycle/GeometryRef 等)不继承、保持默认(未知子键忽略);
    例外:DensificationParams 亦作为 process-window / sensitivity 的 base
    嵌套引用,forbid 随继承带入 — base 内未知键同样 400。
    """

    model_config = ConfigDict(extra="forbid")


class DensificationParams(MethodParamsBase):
    """致密化曲线参数(方法 densification)。"""

    initial_relative_density: float = Field(default=0.65, gt=0, lt=1, description="初始相对密度", examples=[0.65])
    limiting_relative_density: float = Field(default=0.995, gt=0, le=1, description="极限相对密度", examples=[0.995])
    cycle: Cycle | None = Field(default=None, description="工艺曲线;None 继承配置链默认(900℃/120MPa/3h)")
    material: MaterialSelection | None = Field(default=None, description="材料;None 用默认 TC4")
    kinetics: dict[str, float] = Field(
        default_factory=dict,
        description="Arrhenius 动力学参数覆盖: k_ref[1/s], q_j_per_mol, pressure_exponent, t_ref_c, p_ref_mpa",
        examples=[{"k_ref": 3.4e-4, "q_j_per_mol": 150000, "pressure_exponent": 1.5}],
    )


class ProcessWindowParams(MethodParamsBase):
    """工艺窗口扫参(方法 process-window):温度×压力×保温时长 → 终态密度。"""

    base: DensificationParams = Field(default_factory=DensificationParams, description="基准参数")
    temperatures_c: list[float] = Field(default=[880, 900, 920, 940], description="温度扫参点(℃)")
    pressures_mpa: list[float] = Field(default=[100, 110, 120, 130, 140], description="压力扫参点(MPa)")
    hold_times_s: list[float] = Field(default=[7200, 10800, 14400], description="保温时长扫参点(秒)")


class ShrinkageEstimateParams(MethodParamsBase):
    """均匀收缩估算(方法 shrinkage-estimate)。"""

    initial_relative_density: float = Field(default=0.65, gt=0, lt=1, description="初始相对密度")
    final_relative_density: float = Field(default=0.97, gt=0, le=1, description="终态相对密度(或目标)")
    characteristic_lengths_mm: dict[str, float] = Field(
        ...,
        min_length=1,
        description="命名特征尺寸(mm)→ 各自的收缩量,例 {height: 150, outer_diameter: 100}",
        examples=[{"height": 150, "outer_diameter": 100}],
    )


class MaterialQueryParams(MethodParamsBase):
    """材料性能查询(方法 material-query)。"""

    material: str = Field(default="tc4", description="材料名:tc4 | 20steel", examples=["tc4"])
    temperatures_c: list[float] = Field(default=[20, 400, 600, 800, 900], description="查询温度点(℃)")
    properties: list[str] | None = Field(
        default=None,
        description="限定属性(如 young_modulus_gpa, yield_stress_mpa);None 返回全部",
    )


class MeshMethodParams(MethodParamsBase):
    """网格转换(方法 mesh):STEP → 粉末域 → Gmsh → .cdb。"""

    geometry: GeometryRef = Field(..., description="几何引用(capsule_step 必填)")
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    derive_powder_domain: bool = Field(default=True, description="是否推导粉末域(cad_workflow 逻辑)")
    output_formats: list[Literal["cdb", "msh", "stl"]] = Field(default=["cdb"], description="输出格式")


class AxisymProfile(BaseModel):
    """轴对称 2D 剖面(mm);缺省从几何包围盒自动生成等效矩形环。"""

    inner_radius_mm: float | None = Field(default=None, gt=0, description="内半径(mm);实心粉末为 None/0", examples=[20])
    outer_radius_mm: float | None = Field(default=None, gt=0, description="外半径(mm)", examples=[50])
    height_mm: float | None = Field(default=None, gt=0, description="轴向高度(mm)", examples=[150])


class AxisymHipParams(MethodParamsBase):
    """2D 轴对称 HIP 全过程(方法 axisym-hip;阶段 1 冒烟=线性占位本构)。"""

    geometry: GeometryRef | None = Field(default=None, description="几何(用于自动剖面);None 需给 profile")
    profile: AxisymProfile | None = Field(default=None, description="显式 2D 剖面;优先于 geometry 自动推导")
    cycle: Cycle | None = Field(default=None, description="工艺曲线;None 继承配置链默认(900℃/120MPa/3h)")
    materials: MaterialSelection | None = Field(default=None, description="材料;None 用默认 TC4 + 20 钢")
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    numerics: Numerics = Field(default_factory=Numerics)
    nlgeom: bool = Field(default=False, description="大变形开关(冒烟阶段建议 False)")


class AxisymThermalParams(MethodParamsBase):
    """升温段温度场(方法 axisym-thermal;真实内核:纯热瞬态)。"""

    geometry: GeometryRef | None = Field(default=None, description="几何(用于自动剖面);None 需给 profile")
    profile: AxisymProfile | None = Field(default=None, description="显式 2D 剖面;优先于 geometry 自动推导")
    cycle: Cycle | None = Field(default=None, description="取其温度 ramp;压力忽略")
    materials: MaterialSelection | None = Field(default=None, description="材料;None 用默认 TC4 + 20 钢")
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    numerics: Numerics = Field(default_factory=Numerics)
    probe_points: list[tuple[float, float]] = Field(
        default=[(0.0, 0.5), (0.0, 0.0)],
        max_length=99,
        description="探针点 (r, z) 归一化坐标 → 输出 T-t 曲线(缺省芯部+底面);≤99 个(探针号受 APDL 写出宽度限制,超限在校验层拒绝而非静默截断撞键)",
    )


class AxisymMechanicalParams(MethodParamsBase):
    """保温段应力/密度分布(方法 axisym-mechanical;阶段 1 冒烟=线弹性)。"""

    geometry: GeometryRef | None = Field(default=None, description="几何(用于自动剖面);None 需给 profile")
    profile: AxisymProfile | None = Field(default=None, description="显式 2D 剖面;优先于 geometry 自动推导")
    hold_temperature_c: float = Field(default=900, description="保温温度(℃)")
    hold_pressure_mpa: float = Field(default=120, description="保温压力(MPa)")
    materials: MaterialSelection | None = Field(default=None, description="材料;None 用默认 TC4 + 20 钢")
    mesh: MeshSettings = Field(default_factory=MeshSettings)


class Full3dHipParams(MethodParamsBase):
    """3D 全模型 HIP(方法 full3d-hip;阶段 1 冒烟=真实网格+线性占位本构)。"""

    geometry: GeometryRef = Field(..., description="capsule_step 必填")
    cycle: Cycle | None = Field(default=None, description="工艺曲线;None 继承配置链默认(900℃/120MPa/3h)")
    materials: MaterialSelection | None = Field(default=None, description="材料;None 用默认 TC4 + 20 钢")
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    nlgeom: bool = Field(default=False, description="大变形开关(冒烟阶段建议 False)")


class ExperimentalPoint(BaseModel):
    """实验密度数据点。"""

    time_s: float = Field(..., ge=0, description="采样时刻(秒)")
    relative_density: float = Field(..., gt=0, le=1, description="相对密度(0-1)")


class CalibrateParams(MethodParamsBase):
    """本构参数标定(方法 calibrate;阶段 1=Arrhenius 最小二乘)。"""

    experimental: list[ExperimentalPoint] = Field(..., min_length=3, description="实验 D-t 数据(≥3 点)")
    initial_relative_density: float = Field(default=0.65, gt=0, lt=1, description="初始相对密度")
    limiting_relative_density: float = Field(default=0.995, gt=0, le=1, description="极限相对密度")
    cycle: Cycle | None = Field(default=None, description="实验对应的工艺曲线;None 继承配置链默认")
    fit_params: list[Literal["k_ref", "q_j_per_mol", "pressure_exponent"]] = Field(
        default=["k_ref", "q_j_per_mol"],
        description="拟合哪些动力学参数(其余固定)",
    )


class CompensateParams(MethodParamsBase):
    """型腔预变形补偿(方法 compensate;阶段 1=均匀收缩缩放)。"""

    geometry: GeometryRef = Field(..., description="cavity_step=目标型腔(必填)")
    initial_relative_density: float = Field(default=0.65, gt=0, lt=1, description="初始相对密度")
    final_relative_density: float = Field(default=0.97, gt=0, le=1, description="或由 densification 结果取终态密度")
    scale_axis: Literal["uniform", "per_axis"] = Field(default="uniform", description="均匀/各向异性缩放")


class SensitivityParams(MethodParamsBase):
    """参数敏感性(方法 sensitivity;阶段 1=快速核批量)。"""

    base: DensificationParams = Field(default_factory=DensificationParams, description="基准参数")
    sweep: dict[str, list[float]] = Field(
        ...,
        min_length=1,
        description="参数名→取值列表,例 {initial_relative_density: [0.60,0.65,0.70], temperature_c: [880,900,920]}(temperature_c/hold_pressure_mpa 覆盖曲线峰值)",
    )


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
    """POST /sim/{method} 请求体:part 引用 + 内联参数(可只给其一)。

    合并优先级: params(本对象) > config/parts/{part}.yaml > config/service.yaml defaults。
    合并结果写入 job 目录 resolved-params.json,保证可追溯。
    """

    model_config = ConfigDict(extra="allow")

    part: str | None = Field(default=None, description="零件配置名(config/parts/<name>.yaml)", examples=["tc4-demo"])
    params: dict[str, Any] | None = Field(
        default=None,
        description="内联参数(对应各方法参数模型的顶层字段,如 {initial_relative_density: 0.68})",
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
    method: str = Field(..., description="方法名", examples=["densification"])
    status: JobStatusEnum = Field(default=JobStatusEnum.PENDING, description="受理后固定为 pending")
    fidelity: Fidelity | None = Field(
        default=None, description="方法标称保真度:real=真实内核 / smoke=占位本构冒烟"
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
    part: str | None = Field(default=None, description="提交时引用的零件配置名(未引用为 None)")
    status: JobStatusEnum = Field(..., description="状态机:pending → running → succeeded / failed / cancelled")
    fidelity: Fidelity | None = Field(default=None, description="结果保真度:real=真实内核 / smoke=占位本构冒烟")
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


class PartInfo(BaseModel):
    """GET /parts 响应条目。"""

    name: str = Field(..., description="零件配置名(config/parts/<name>.yaml 的文件名)")
    config: dict[str, Any] = Field(..., description="该零件完整配置(几何/材料/曲线/网格,提交时按三级合并继承)")


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
    """POST /uploads(multipart STEP)响应 — 返回服务端路径供 geometry 引用。"""

    path: str = Field(..., description="服务端保存路径(填入 geometry.capsule_step / cavity_step 引用)")
    size_bytes: int = Field(..., description="文件大小(字节)")


# ---------------------------------------------------------------------------
# 内核执行契约(T3/T4 实现方参照)
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
