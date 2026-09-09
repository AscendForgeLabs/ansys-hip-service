"""接口契约(冻结版)— 所有请求/响应的 pydantic 模型.

字段命名与 HIPForm config YAML 对齐(cycle/materials/mesh/numerics),便于 HIPForm 侧映射。
每个字段的 description/examples 直接成为 Swagger 文档 — 请写清楚。

单位约定:mm / MPa / s / ℃(与 docs/materials-process.md 一致)。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


# ---------------------------------------------------------------------------
# 各方法参数模型(方法注册表引用;字段即 Swagger 输入)
# ---------------------------------------------------------------------------

class DensificationParams(BaseModel):
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


class ProcessWindowParams(BaseModel):
    """工艺窗口扫参(方法 process-window):温度×压力×保温时长 → 终态密度。"""

    base: DensificationParams = Field(default_factory=DensificationParams, description="基准参数")
    temperatures_c: list[float] = Field(default=[880, 900, 920, 940], description="温度扫参点(℃)")
    pressures_mpa: list[float] = Field(default=[100, 110, 120, 130, 140], description="压力扫参点(MPa)")
    hold_times_s: list[float] = Field(default=[7200, 10800, 14400], description="保温时长扫参点(秒)")


class ShrinkageEstimateParams(BaseModel):
    """均匀收缩估算(方法 shrinkage-estimate)。"""

    initial_relative_density: float = Field(default=0.65, gt=0, lt=1)
    final_relative_density: float = Field(default=0.97, gt=0, le=1, description="终态相对密度(或目标)")
    characteristic_lengths_mm: dict[str, float] = Field(
        ...,
        min_length=1,
        description="命名特征尺寸(mm)→ 各自的收缩量,例 {height: 150, outer_diameter: 100}",
        examples=[{"height": 150, "outer_diameter": 100}],
    )


class MaterialQueryParams(BaseModel):
    """材料性能查询(方法 material-query)。"""

    material: str = Field(default="tc4", description="材料名:tc4 | 20steel", examples=["tc4"])
    temperatures_c: list[float] = Field(default=[20, 400, 600, 800, 900], description="查询温度点(℃)")
    properties: list[str] | None = Field(
        default=None,
        description="限定属性(如 young_modulus_gpa, yield_stress_mpa);None 返回全部",
    )


class MeshMethodParams(BaseModel):
    """网格转换(方法 mesh):STEP → 粉末域 → Gmsh → .cdb。"""

    geometry: GeometryRef = Field(..., description="几何引用(capsule_step 必填)")
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    derive_powder_domain: bool = Field(default=True, description="是否推导粉末域(cad_workflow 逻辑)")
    output_formats: list[Literal["cdb", "msh", "stl"]] = Field(default=["cdb"], description="输出格式")


class AxisymProfile(BaseModel):
    """轴对称 2D 剖面(mm);缺省从几何包围盒自动生成等效矩形环。"""

    inner_radius_mm: float | None = Field(default=None, gt=0, examples=[20])
    outer_radius_mm: float | None = Field(default=None, gt=0, examples=[50])
    height_mm: float | None = Field(default=None, gt=0, examples=[150])


class AxisymHipParams(BaseModel):
    """2D 轴对称 HIP 全过程(方法 axisym-hip;阶段 1 冒烟=线性占位本构)。"""

    geometry: GeometryRef | None = Field(default=None, description="几何(用于自动剖面);None 需给 profile")
    profile: AxisymProfile | None = Field(default=None, description="显式 2D 剖面;优先于 geometry 自动推导")
    cycle: Cycle | None = None
    materials: MaterialSelection | None = None
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    numerics: Numerics = Field(default_factory=Numerics)
    nlgeom: bool = Field(default=False, description="大变形开关(冒烟阶段建议 False)")


class AxisymThermalParams(BaseModel):
    """升温段温度场(方法 axisym-thermal;真实内核:纯热瞬态)。"""

    geometry: GeometryRef | None = None
    profile: AxisymProfile | None = None
    cycle: Cycle | None = Field(default=None, description="取其温度 ramp;压力忽略")
    materials: MaterialSelection | None = None
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    numerics: Numerics = Field(default_factory=Numerics)
    probe_points: list[tuple[float, float]] = Field(
        default=[(0.0, 0.5), (0.0, 0.0)],
        description="探针点 (r, z) 归一化坐标 → 输出 T-t 曲线(芯部/表面)",
    )


class AxisymMechanicalParams(BaseModel):
    """保温段应力/密度分布(方法 axisym-mechanical;阶段 1 冒烟=线弹性)。"""

    geometry: GeometryRef | None = None
    profile: AxisymProfile | None = None
    hold_temperature_c: float = Field(default=900, description="保温温度(℃)")
    hold_pressure_mpa: float = Field(default=120, description="保温压力(MPa)")
    materials: MaterialSelection | None = None
    mesh: MeshSettings = Field(default_factory=MeshSettings)


class Full3dHipParams(BaseModel):
    """3D 全模型 HIP(方法 full3d-hip;阶段 1 冒烟=真实网格+线性占位本构)。"""

    geometry: GeometryRef = Field(..., description="capsule_step 必填")
    cycle: Cycle | None = None
    materials: MaterialSelection | None = None
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    nlgeom: bool = Field(default=False)


class ExperimentalPoint(BaseModel):
    """实验密度数据点。"""

    time_s: float = Field(..., ge=0)
    relative_density: float = Field(..., gt=0, le=1)


class CalibrateParams(BaseModel):
    """本构参数标定(方法 calibrate;阶段 1=Arrhenius 最小二乘)。"""

    experimental: list[ExperimentalPoint] = Field(..., min_length=3, description="实验 D-t 数据(≥3 点)")
    initial_relative_density: float = Field(default=0.65, gt=0, lt=1)
    limiting_relative_density: float = Field(default=0.995, gt=0, le=1)
    cycle: Cycle | None = None
    fit_params: list[Literal["k_ref", "q_j_per_mol", "pressure_exponent"]] = Field(
        default=["k_ref", "q_j_per_mol"],
        description="拟合哪些动力学参数(其余固定)",
    )


class CompensateParams(BaseModel):
    """型腔预变形补偿(方法 compensate;阶段 1=均匀收缩缩放)。"""

    geometry: GeometryRef = Field(..., description="cavity_step=目标型腔(必填)")
    initial_relative_density: float = Field(default=0.65, gt=0, lt=1)
    final_relative_density: float = Field(default=0.97, gt=0, le=1, description="或由 densification 结果取终态密度")
    scale_axis: Literal["uniform", "per_axis"] = Field(default="uniform", description="均匀/各向异性缩放")


class SensitivityParams(BaseModel):
    """参数敏感性(方法 sensitivity;阶段 1=快速核批量)。"""

    base: DensificationParams = Field(default_factory=DensificationParams, description="基准参数")
    sweep: dict[str, list[float]] = Field(
        ...,
        min_length=1,
        description="参数名→取值列表,例 {initial_relative_density: [0.60,0.65,0.70], temperature_c: [880,900,920]}(temperature_c/hold_pressure_mpa 覆盖曲线峰值)",
    )


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


class SimAccepted(BaseModel):
    """作业受理响应(202)。"""

    id: str = Field(..., description="作业 ID", examples=["9d2f..."])
    method: str
    status: JobStatusEnum = JobStatusEnum.PENDING
    fidelity: Fidelity | None = None
    status_url: str = Field(..., examples=["/jobs/9d2f..."])


class JobState(BaseModel):
    """GET /jobs/{id} 响应。"""

    id: str
    method: str
    part: str | None = None
    status: JobStatusEnum
    fidelity: Fidelity | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: ErrorBody | None = None
    log_url: str | None = None
    result_url: str | None = None
    artifacts_url: str | None = None


class PartInfo(BaseModel):
    """GET /parts 响应条目。"""

    name: str
    config: dict[str, Any]


class HealthReport(BaseModel):
    """GET /health 响应。"""

    status: Literal["ok", "degraded"]
    mapdl_found: bool
    license_env_set: bool
    queue_running: int
    queue_pending: int
    version: str


class UploadAccepted(BaseModel):
    """POST /uploads(multipart STEP)响应 — 返回服务端路径供 geometry 引用。"""

    path: str
    size_bytes: int


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
