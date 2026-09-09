"""方法注册表(冻结版)— 12 个计算方法的元数据与执行器解析.

API 层(/sim/*)只依赖本注册表;内核实现方(T3/T4)按签名约定提供
`run_<method>(params, ctx) -> dict`,注册表按需懒加载,缺失时返回
METHOD_NOT_IMPLEMENTED。新增方法 = 在 REGISTRY 加一行 + kernels 提供 run_* 函数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .schemas import (
    AxisymHipParams,
    AxisymMechanicalParams,
    AxisymThermalParams,
    CalibrateParams,
    CompensateParams,
    DensificationParams,
    Full3dHipParams,
    MaterialQueryParams,
    MeshMethodParams,
    ProcessWindowParams,
    SensitivityParams,
    ShrinkageEstimateParams,
)


class KernelError(Exception):
    """内核主动失败(作业 → failed,error 原样返回给调用方)。

    code 取值见 schemas.py 顶部错误码约定;message 面向人读,
    MAPDL 类错误应附 job.out 中的关键行。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


Group = Literal["quick", "fem2d", "fem3d", "inverse"]

GROUP_LABELS: dict[Group, str] = {
    "quick": "快速计算(解析/数值秒级)",
    "fem2d": "2D 轴对称 FEM(MAPDL)",
    "fem3d": "3D 全模型 FEM(MAPDL)",
    "inverse": "反演/优化",
}


@dataclass(frozen=True)
class MethodSpec:
    """一个 /sim/{method} 端点的全部元数据(自描述用,序列化给 GET /sim/methods)。"""

    name: str
    group: Group
    status: Literal["available", "experimental", "planned"]
    fidelity: Literal["real", "smoke"]
    summary: str                       # 一句话用途(HIPForm 侧据此选方法)
    returns: str                       # 结果 JSON 形态简述
    typical_runtime: str
    requires_mapdl: bool
    requires_geometry: bool            # True = 必须给 geometry 或 part 配置
    params_model: type[BaseModel] = field(compare=False)
    tags: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        """GET /sim/methods 条目(params 模型展开为 JSON Schema,Swagger 外的自描述)。"""
        return {
            "name": self.name,
            "group": self.group,
            "group_label": GROUP_LABELS[self.group],
            "status": self.status,
            "fidelity": self.fidelity,
            "summary": self.summary,
            "returns": self.returns,
            "typical_runtime": self.typical_runtime,
            "requires_mapdl": self.requires_mapdl,
            "requires_geometry": self.requires_geometry,
            "params_schema": self.params_model.model_json_schema(),
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# 注册表(12 方法;阶段 1 全部上线)
# ---------------------------------------------------------------------------

REGISTRY: dict[str, MethodSpec] = {
    # ---- 快速计算(真实内核,不跑 MAPDL) ----
    "densification": MethodSpec(
        name="densification",
        group="quick",
        status="available",
        fidelity="real",
        summary="致密化曲线 D(t):Arrhenius 动力学沿工艺曲线积分,回答『该工艺下密度能否到 0.97』",
        returns="{times_s[], densities[], final_density, reached_097: bool, fidelity}",
        typical_runtime="<1 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=DensificationParams,
        tags=("arrhenius", "cycle"),
    ),
    "process-window": MethodSpec(
        name="process-window",
        group="quick",
        status="available",
        fidelity="real",
        summary="工艺窗口扫参:温度×压力×保温时长网格 → 各组合终态密度,输出达标(≥0.97)窗口",
        returns="{grid{temperatures_c[],pressures_mpa[],hold_times_s[]}, final_densities[三重嵌套], window_ok: bool[][][]}",
        typical_runtime="5–30 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=ProcessWindowParams,
        tags=("arrhenius", "sweep"),
    ),
    "shrinkage-estimate": MethodSpec(
        name="shrinkage-estimate",
        group="quick",
        status="available",
        fidelity="real",
        summary="均匀收缩估算:由 D0→Df 体积守恒 (D0/Df)^(1/3) 给各特征尺寸的收缩量与收缩率",
        returns="{items{name,length_mm,shrunk_mm,shrink_ratio}[], linear_strain, fidelity}",
        typical_runtime="<1 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=ShrinkageEstimateParams,
        tags=("geometry-free"),
    ),
    "material-query": MethodSpec(
        name="material-query",
        group="quick",
        status="available",
        fidelity="real",
        summary="材料性能查询:TC4 粉末/20 钢包套温度相关参数(模量/屈服/蠕变/Gurson 参数,文献初值)",
        returns="{material, rows[{temperature_c, properties{}}], source_notes[]}",
        typical_runtime="<1 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=MaterialQueryParams,
        tags=("materials"),
    ),
    "mesh": MethodSpec(
        name="mesh",
        group="quick",
        status="available",
        fidelity="real",
        summary="STEP→粉末域推导(cad_workflow 移植)→Gmsh 网格(physical groups 分包套/粉末)→.cdb/msh/stl",
        returns="{node_count, element_count, groups{capsule,powder}, artifacts[]}",
        typical_runtime="10–60 s",
        requires_mapdl=False,
        requires_geometry=True,
        params_model=MeshMethodParams,
        tags=("gmsh", "meshio", "cdb"),
    ),
    # ---- 2D FEM(MAPDL) ----
    "axisym-hip": MethodSpec(
        name="axisym-hip",
        group="fem2d",
        status="experimental",
        fidelity="smoke",
        summary="2D 轴对称 HIP 全过程。阶段 1 冒烟:真实几何剖面+线弹性占位本构,验证求解管线;阶段 2 换 Gurson+蠕变+接触,API 不变",
        returns="{displacement_max_mm, von_mises_max_mpa, time_history{t[],disp[]}, artifacts[]}",
        typical_runtime="2–10 min",
        requires_mapdl=True,
        requires_geometry=False,   # geometry 或 profile 二选一
        params_model=AxisymHipParams,
        tags=("PLANE183", "smoke"),
    ),
    "axisym-thermal": MethodSpec(
        name="axisym-thermal",
        group="fem2d",
        status="available",
        fidelity="real",
        summary="升温段纯热瞬态(PLANE77):包套-粉末芯部温差滞后曲线,校核均匀温度假设",
        returns="{probes{name,times_s[],temperatures_c[]}, max_lag_c, artifacts[]}",
        typical_runtime="1–5 min",
        requires_mapdl=True,
        requires_geometry=False,
        params_model=AxisymThermalParams,
        tags=("PLANE77", "transient"),
    ),
    "axisym-mechanical": MethodSpec(
        name="axisym-mechanical",
        group="fem2d",
        status="experimental",
        fidelity="smoke",
        summary="保温段力学(阶段 1 冒烟=线弹性):外压下包套/粉末应力分布与位移。阶段 2 换 Gurson 等温力学",
        returns="{displacement_max_mm, von_mises_max_mpa, section_stress[], artifacts[]}",
        typical_runtime="1–5 min",
        requires_mapdl=True,
        requires_geometry=False,
        params_model=AxisymMechanicalParams,
        tags=("PLANE183", "smoke"),
    ),
    # ---- 3D FEM(MAPDL) ----
    "full3d-hip": MethodSpec(
        name="full3d-hip",
        group="fem3d",
        status="experimental",
        fidelity="smoke",
        summary="3D 全模型 HIP(真实 STEP 网格,阶段 1 冒烟=线性占位本构;阶段 2 换 SOLID187+接触+NLGEOM)",
        returns="{displacement_max_mm, von_mises_max_mpa, deformed_stl, artifacts[]}",
        typical_runtime="0.5–4 h",
        requires_mapdl=True,
        requires_geometry=True,
        params_model=Full3dHipParams,
        tags=("SOLID187", "smoke"),
    ),
    # ---- 反演/优化 ----
    "calibrate": MethodSpec(
        name="calibrate",
        group="inverse",
        status="available",
        fidelity="real",
        summary="实验 D-t 数据 → Arrhenius 动力学参数(scipy least_squares);阶段 2 升级 Gurson 参数反演,API 不变",
        returns="{fitted_params{}, covariance_or_bounds, residual_rms, fit_curve{times_s[],densities[]}}",
        typical_runtime="5–60 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=CalibrateParams,
        tags=("least_squares"),
    ),
    "compensate": MethodSpec(
        name="compensate",
        group="inverse",
        status="available",
        fidelity="real",
        summary="预变形补偿:目标型腔 STEP 按 (Df/D0)^(1/3) 放大,输出补偿后 STEP(供包套设计);阶段 2 换 FEM 迭代补偿",
        returns="{scale_factors, output_step_path, artifacts[]}",
        typical_runtime="5–30 s",
        requires_mapdl=False,
        requires_geometry=True,
        params_model=CompensateParams,
        tags=("scaling", "step"),
    ),
    "sensitivity": MethodSpec(
        name="sensitivity",
        group="inverse",
        status="available",
        fidelity="real",
        summary="参数敏感性:对扫参字典逐参数批量跑 densification 核,输出各参数对终态密度的敏感度排序",
        returns="{base_final_density, one_at_a_time{name,values[],final_densities[],sensitivity}[]}",
        typical_runtime="5–60 s",
        requires_mapdl=False,
        requires_geometry=False,
        params_model=SensitivityParams,
        tags=("oat"),
    ),
}


# ---------------------------------------------------------------------------
# 执行器解析(懒加载;kernels 子模块由 T3/T4 分头实现)
# ---------------------------------------------------------------------------

# 按序搜索的 kernels 子模块(run_<method> 带下划线名,与方法名连字符对应)
KERNEL_MODULES: tuple[str, ...] = (
    "arrhenius",    # T3: densification / process-window / sensitivity
    "shrinkage",    # T3: shrinkage-estimate / compensate
    "calibrate",    # T3: calibrate
    "materials",    # T3: material-query(读 data/materials.yaml)
    "fem",          # T4: axisym-* / full3d-hip / mesh(APDL+gmsh 管线)
)

Executor = Callable[[BaseModel, Any], dict]


def get_method(method: str) -> MethodSpec | None:
    """按名称取方法元数据;不存在返回 None(由 API 层回 404)。"""
    return REGISTRY.get(method)


def resolve_executor(method: str) -> Executor | None:
    """解析内核执行器 run_<method>(params, ctx) -> dict。

    懒加载 kernels 子模块:任一子模块可选依赖缺失不影响 API 启动;
    找不到返回 None(由调用方回 METHOD_NOT_IMPLEMENTED)。
    """
    spec = REGISTRY.get(method)
    if spec is None:
        return None
    func_name = "run_" + method.replace("-", "_")
    for mod_name in KERNEL_MODULES:
        try:
            module = import_module(f"ansys_hip.kernels.{mod_name}")
        except ImportError:  # 可选依赖缺失或子模块尚未实现
            continue
        func = getattr(module, func_name, None)
        if func is not None:
            return func
    return None


def methods_payload() -> list[dict[str, Any]]:
    """GET /sim/methods 响应体:按组排序的全部方法自描述。"""
    order = {"quick": 0, "fem2d": 1, "fem3d": 2, "inverse": 3}
    specs = sorted(REGISTRY.values(), key=lambda s: (order[s.group], s.name))
    return [s.public_dict() for s in specs]
