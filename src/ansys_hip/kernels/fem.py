"""FEM 内核 — mesh / axisym-thermal(真实)/ axisym-hip / axisym-mechanical / full3d-hip。

编排:参数解析 → 剖面域(geometry 包围盒或显式 profile)→ 材料命令块
(apdl/material_apdl)→ 模板渲染(apdl/render)→ runner.run_mapdl 批处理
→ results 解析 series/summary → 结果 dict(必含 fidelity)。

约定:
    - MAT 1=powder、2=capsule(与 mesh.py 的 .cdb 分组一致);
    - 几何自动剖面假设 STEP 轴沿 Z:outer=max|x|、height=z 跨度、cavity 给 r_split;
    - params.cycle 为 None 时用默认工艺(900℃/120MPa/3h,同配置链缺省);
    - MaterialSelection.overrides 阶段 1 忽略(FEM 直接用材料库点列);
    - axisym 系列无 cavity 时退化为单粉末域(等价环近似,日志记录)。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Sequence

from ..apdl.material_apdl import elastic_at_temperature, material_commands
from ..apdl.render import write_input
from ..mesh import exterior_faces_from_cdb, mesh_capsule_powder, step_bbox
from ..registry import KernelError
from ..results import (
    SERIES_FILENAME,
    SUMMARY_FILENAME,
    parse_series_csv,
    parse_summary_csv,
)
from ..runner import run_mapdl
from ..schemas import (
    AxisymHipParams,
    AxisymMechanicalParams,
    AxisymThermalParams,
    Cycle,
    CyclePoint,
    Full3dHipParams,
    MaterialSelection,
    MeshMethodParams,
    Numerics,
    RunContext,
)
from . import artifact_dir, publish_artifacts
from .materials import load_material

logger = logging.getLogger(__name__)

# 默认工艺曲线(900℃/120MPa/3h;与配置链缺省一致)
DEFAULT_CYCLE_POINTS: tuple[tuple[float, float, float], ...] = (
    (0.0, 20.0, 0.0),
    (3600.0, 900.0, 120.0),
    (14400.0, 900.0, 120.0),
    (18000.0, 20.0, 0.0),
)
DEFAULT_STEPS_PER_CYCLE = 120
MAX_SUBSTEPS_PER_SEGMENT = 200

POWDER_MAT_ID = 1
CAPSULE_MAT_ID = 2
DEFAULT_POWDER = "tc4"
DEFAULT_CAPSULE = "20steel"
PROBE_NAMES_DEFAULT = ("core", "surface")
SECTION_SAMPLE_COUNT = 11
CDB_STEM = "capsule_powder"


# ---------------------------------------------------------------------------
# mesh(真实:STEP → gmsh → .cdb/.msh/.stl)
# ---------------------------------------------------------------------------

def run_mesh(params: MeshMethodParams, ctx: RunContext) -> dict:
    """mesh 方法:STEP → 粉末域 → 共形网格 → 多格式工件(artifacts 为文件名)。"""
    geometry = params.geometry
    if geometry is None or not geometry.capsule_step:
        raise KernelError("INVALID_PARAMS", "mesh 方法必须提供 geometry.capsule_step")
    result = mesh_capsule_powder(
        capsule_step=geometry.capsule_step,
        cavity_step=geometry.cavity_step if params.derive_powder_domain else None,
        mesh_size_mm=params.mesh.mesh_size_mm,
        out_formats=params.output_formats,
        workdir=artifact_dir(ctx),
    )
    return {
        "node_count": result["node_count"],
        "element_count": result["element_count"],
        "groups": result["groups"],
        "artifacts": [Path(path).name for path in result["artifacts"]],
        "degraded": result["degraded"],
        "fidelity": "real",
    }


# ---------------------------------------------------------------------------
# axisym-thermal(真实:PLANE77 纯热瞬态)
# ---------------------------------------------------------------------------

def run_axisym_thermal(params: AxisymThermalParams, ctx: RunContext) -> dict:
    """升温段热瞬态:探针 T-t 曲线 + 芯表最大滞后(max_lag_c)。"""
    domain = _axisym_domain(params)
    zones = _axisym_zones(domain)
    cycle = _resolve_cycle(params.cycle)
    time_step_s = _time_step(params.numerics, cycle)
    powder, capsule = _load_two_materials(params.materials)
    material_blocks = [
        "\n".join(material_commands(material, mat_id, "thermal"))
        for mat_id, material in _present_materials(zones, powder, capsule).items()
    ]
    probes = [
        {
            "index": index + 1,
            "r": domain["inner"] + r_norm * (domain["outer"] - domain["inner"]),
            "z": z_norm * domain["height"],
        }
        for index, (r_norm, z_norm) in enumerate(params.probe_points)
    ]
    _execute(
        "template_axisym_thermal.inp", "axisym_thermal.inp", ctx,
        zones=zones,
        z_from=0.0,
        z_to=domain["height"],
        initial_temp_c=float(cycle.points[0].temperature_c),
        material_blocks=material_blocks,
        mesh_size_mm=params.mesh.mesh_size_mm,
        outer_r=domain["outer"],
        probes=probes,
        segments=_segments(cycle, time_step_s),
    )
    rows = parse_series_csv(ctx.job_dir)
    if not rows:
        raise KernelError("INTERNAL", "MAPDL 正常结束但 series.csv 无数据(探针提取失败)")
    names = _probe_names(len(probes))
    curves = {name: {"times_s": [], "temperatures_c": []} for name in names}
    for row in rows:
        probe_index = int(row["probe"])
        if not 1 <= probe_index <= len(names):
            continue
        curve = curves[names[probe_index - 1]]
        curve["times_s"].append(row["time_s"])
        curve["temperatures_c"].append(row["value"])
    return {
        "probes": [{"name": name, **curves[name]} for name in names],
        "max_lag_c": round(_max_probe_lag(rows), 6),
        "artifacts": ["axisym_thermal.inp", *publish_artifacts(
            ctx, (SERIES_FILENAME, SUMMARY_FILENAME)
        )],
        "fidelity": "real",
    }


# ---------------------------------------------------------------------------
# axisym-hip(冒烟:线弹性全过程)
# ---------------------------------------------------------------------------

def run_axisym_hip(params: AxisymHipParams, ctx: RunContext) -> dict:
    """2D 轴对称 HIP 全过程冒烟:外压曲线分段 + 位移/等效应极值与 T-位移历程。"""
    domain = _axisym_domain(params)
    zones = _axisym_zones(domain)
    cycle = _resolve_cycle(params.cycle)
    time_step_s = _time_step(params.numerics, cycle)
    hold_temperature_c = max(point.temperature_c for point in cycle.points)
    powder, capsule = _load_two_materials(params.materials)
    material_lines = _elastic_lines(zones, powder, capsule, hold_temperature_c)
    _execute(
        "template_axisym_hip.inp", "axisym_hip.inp", ctx,
        zones=zones,
        z_from=0.0,
        z_to=domain["height"],
        material_lines=material_lines,
        mesh_size_mm=params.mesh.mesh_size_mm,
        outer_r=domain["outer"],
        nlgeom=bool(params.nlgeom),
        segments=_segments(cycle, time_step_s),
    )
    rows = parse_series_csv(ctx.job_dir)
    time_history = {
        "t": [row["time_s"] for row in rows if row["probe"] == "0"],
        "disp": [row["value"] for row in rows if row["probe"] == "0"],
    }
    summary = parse_summary_csv(ctx.job_dir)
    return {
        "displacement_max_mm": _summary_float(summary, "displacement_max_mm"),
        "von_mises_max_mpa": _summary_float(summary, "von_mises_max_mpa"),
        "time_history": time_history,
        "artifacts": ["axisym_hip.inp", *publish_artifacts(
            ctx, (SERIES_FILENAME, SUMMARY_FILENAME)
        )],
        "fidelity": "smoke",
    }


# ---------------------------------------------------------------------------
# axisym-mechanical(冒烟:保温段线弹性单工况)
# ---------------------------------------------------------------------------

def run_axisym_mechanical(params: AxisymMechanicalParams, ctx: RunContext) -> dict:
    """保温段单工况:位移/等效应极值 + 中截面径向应力剖面。"""
    domain = _axisym_domain(params)
    zones = _axisym_zones(domain)
    powder, capsule = _load_two_materials(params.materials)
    material_lines = _elastic_lines(zones, powder, capsule, params.hold_temperature_c)
    section_samples = [
        {"radius": radius}
        for radius in _linspace(domain["inner"], domain["outer"], SECTION_SAMPLE_COUNT)
    ]
    _execute(
        "template_axisym_mechanical.inp", "axisym_mechanical.inp", ctx,
        zones=zones,
        z_from=0.0,
        z_to=domain["height"],
        z_mid=domain["height"] / 2.0,
        material_lines=material_lines,
        mesh_size_mm=params.mesh.mesh_size_mm,
        outer_r=domain["outer"],
        hold_pressure_mpa=params.hold_pressure_mpa,
        section_samples=section_samples,
    )
    section_stress = [
        {"radius_mm": row["time_s"], "sxx_mpa": row["value"]}
        for row in parse_series_csv(ctx.job_dir)
        if row["probe"] == "1"
    ]
    summary = parse_summary_csv(ctx.job_dir)
    return {
        "displacement_max_mm": _summary_float(summary, "displacement_max_mm"),
        "von_mises_max_mpa": _summary_float(summary, "von_mises_max_mpa"),
        "section_stress": section_stress,
        "artifacts": ["axisym_mechanical.inp", *publish_artifacts(
            ctx, (SERIES_FILENAME, SUMMARY_FILENAME)
        )],
        "fidelity": "smoke",
    }


# ---------------------------------------------------------------------------
# full3d-hip(冒烟:真实网格 + 线弹性 + 外表面均压)
# ---------------------------------------------------------------------------

def run_full3d_hip(params: Full3dHipParams, ctx: RunContext) -> dict:
    """3D 全模型:先 mesh 出 .cdb,再 SFE 外表面均压求解(SOLID45 线弹性)。"""
    geometry = params.geometry
    if geometry is None or not geometry.capsule_step:
        raise KernelError("INVALID_PARAMS", "full3d-hip 方法必须提供 geometry.capsule_step")
    mesh_result = mesh_capsule_powder(
        capsule_step=geometry.capsule_step,
        cavity_step=geometry.cavity_step,
        mesh_size_mm=params.mesh.mesh_size_mm,
        out_formats=("cdb",),
        workdir=artifact_dir(ctx),
    )
    cdb_path = artifact_dir(ctx) / f"{CDB_STEM}.cdb"
    pressure_faces = exterior_faces_from_cdb(cdb_path)
    if not pressure_faces:
        raise KernelError("INTERNAL", "未能从 .cdb 提取外边界面(网格退化?)")
    cycle = _resolve_cycle(params.cycle)
    hold_temperature_c = max(point.temperature_c for point in cycle.points)
    hold_pressure_mpa = max(point.pressure_mpa for point in cycle.points)
    powder, capsule = _load_two_materials(params.materials)
    material_lines = [
        line
        for mat_id, material in ((POWDER_MAT_ID, powder), (CAPSULE_MAT_ID, capsule))
        for line in _elastic_lines_for_one(material, mat_id, hold_temperature_c)
    ]
    (xmin, ymin, zmin), (xmax, ymax, _) = mesh_result["bbox"]["min"], mesh_result["bbox"]["max"]
    _execute(
        "template_3d.inp", "full3d_hip.inp", ctx,
        cdb_stem=CDB_STEM,
        material_lines=material_lines,
        nlgeom=bool(params.nlgeom),
        anchor_1=(xmin, ymin, zmin),
        anchor_2=(xmax, ymin, zmin),
        anchor_3=(xmin, ymax, zmin),
        pressure_faces=pressure_faces,
        hold_pressure_mpa=hold_pressure_mpa,
    )
    summary = parse_summary_csv(ctx.job_dir)
    return {
        "displacement_max_mm": _summary_float(summary, "displacement_max_mm"),
        "von_mises_max_mpa": _summary_float(summary, "von_mises_max_mpa"),
        "deformed_stl": None,  # 阶段 2(UPGEOM+变形网格导出);见汇报缺陷记录
        "artifacts": [f"{CDB_STEM}.cdb", "full3d_hip.inp", *publish_artifacts(
            ctx, (SUMMARY_FILENAME,)
        )],
        "fidelity": "smoke",
    }


# ---------------------------------------------------------------------------
# 共用助手
# ---------------------------------------------------------------------------

def _execute(template_name: str, inp_name: str, ctx: RunContext, **context: Any) -> Path:
    """渲染模板 → 写 inp(入 artifacts/)→ 交 runner 批处理(超时/许可/取消统一治理)。"""
    inp_path = write_input(template_name, artifact_dir(ctx) / inp_name, **context)
    run_mapdl(inp_path, ctx.job_dir, ctx, ctx.job_dir.name)
    return inp_path


def _resolve_cycle(cycle: Cycle | None) -> Cycle:
    """cycle 为 None 时回落默认工艺(900℃/120MPa/3h)。"""
    if cycle is not None:
        return cycle
    return Cycle(points=[
        CyclePoint(time_s=t, temperature_c=temp, pressure_mpa=pressure)
        for t, temp, pressure in DEFAULT_CYCLE_POINTS
    ])


def _segments(cycle: Cycle, time_step_s: float) -> list[dict[str, float | int]]:
    """相邻控制点 → 求解分段(终值 + 子步数;段内 KBC,0 坡道)。"""
    points = cycle.points
    if len(points) < 2:
        raise KernelError("INVALID_PARAMS", "工艺曲线至少 2 个控制点")
    segments: list[dict[str, float | int]] = []
    previous_t = float(points[0].time_s)
    for point in points[1:]:
        t_end = float(point.time_s)
        duration = t_end - previous_t
        segments.append({
            "t_end": t_end,
            "temp": float(point.temperature_c),
            "pressure": float(point.pressure_mpa),
            "nsub": _nsubsteps(duration, time_step_s),
        })
        previous_t = t_end
    return segments


def _nsubsteps(duration_s: float, time_step_s: float) -> int:
    """子步数 = ceil(时长/步长),限 [1, MAX](保护许可时长)。"""
    if duration_s <= 0 or time_step_s <= 0:
        return 1
    return max(1, min(math.ceil(duration_s / time_step_s), MAX_SUBSTEPS_PER_SEGMENT))


def _time_step(numerics: Numerics, cycle: Cycle) -> float:
    """时间步长:显式优先;否则曲线总长 / DEFAULT_STEPS_PER_CYCLE。"""
    if numerics.time_step_s is not None and numerics.time_step_s > 0:
        return float(numerics.time_step_s)
    total = float(cycle.points[-1].time_s)
    if total <= 0:
        raise KernelError("INVALID_PARAMS", "工艺曲线总时长必须为正")
    return total / DEFAULT_STEPS_PER_CYCLE


def _axisym_domain(params: AxisymHipParams | AxisymThermalParams | AxisymMechanicalParams) -> dict[str, float | None]:
    """剖面域 {inner, outer, height, r_split}:profile 优先,否则几何包围盒。

    几何自动剖面假设轴沿 Z(等效矩形环):outer=max|x|、height=z 跨度;
    cavity 包围盒提供粉末半径 r_split;越界/缺失 → r_split=None(单粉末域)。
    """
    profile = params.profile
    if profile is not None and profile.outer_radius_mm and profile.height_mm:
        return {
            "inner": float(profile.inner_radius_mm or 0.0),
            "outer": float(profile.outer_radius_mm),
            "height": float(profile.height_mm),
            "r_split": None,
        }
    geometry = params.geometry
    if geometry is not None and geometry.capsule_step:
        capsule = step_bbox(geometry.capsule_step)
        outer = max(abs(capsule["min"][0]), abs(capsule["max"][0]))
        height = capsule["max"][2] - capsule["min"][2]
        r_split = None
        if geometry.cavity_step:
            cavity = step_bbox(geometry.cavity_step)
            r_split = max(abs(cavity["min"][0]), abs(cavity["max"][0]))
    else:
        raise KernelError("INVALID_PARAMS", "axisym 方法需要 geometry.capsule_step 或 profile(outer+height)")
    if outer <= 0 or height <= 0:
        raise KernelError("INVALID_PARAMS", f"几何包围盒退化: outer={outer}, height={height}")
    if r_split is not None and not 0 < r_split < outer:
        logger.warning("粉末半径 %s 越界(outer=%s),退化单粉末域", r_split, outer)
        r_split = None
    return {"inner": 0.0, "outer": outer, "height": height, "r_split": r_split}


def _axisym_zones(domain: dict[str, float | None]) -> list[dict[str, float | int]]:
    """径向分区列表(r_from/r_to/mat/质心选取带);MAT 1=powder 2=capsule。"""
    inner = float(domain["inner"])
    outer = float(domain["outer"])
    split = domain["r_split"]
    bands = [(inner, float(split) if split is not None else outer, POWDER_MAT_ID)]
    if split is not None:
        bands.append((float(split), outer, CAPSULE_MAT_ID))
    zones: list[dict[str, float | int]] = []
    for r_from, r_to, mat_id in bands:
        center = 0.5 * (r_from + r_to)
        half_band = 0.2 * (r_to - r_from)
        zones.append({
            "r_from": r_from, "r_to": r_to, "mat": mat_id,
            "sel_lo": center - half_band, "sel_hi": center + half_band,
        })
    return zones


def _load_two_materials(materials: MaterialSelection | None) -> tuple[dict, dict]:
    """(powder, capsule) 材料字典;未知名称 → INVALID_PARAMS。"""
    powder_name = materials.powder if materials is not None else DEFAULT_POWDER
    capsule_name = materials.capsule if materials is not None else DEFAULT_CAPSULE
    try:
        return load_material(powder_name), load_material(capsule_name)
    except ValueError as exc:
        raise KernelError("INVALID_PARAMS", f"未知材料名: {exc}") from exc


def _present_materials(
    zones: Sequence[dict[str, float | int]], powder: dict, capsule: dict
) -> dict[int, dict]:
    """按分区实际出现的 MAT 取材料字典(单域时 capsule 不加载属性)。"""
    present = {int(zone["mat"]) for zone in zones}
    available = {POWDER_MAT_ID: powder, CAPSULE_MAT_ID: capsule}
    return {mat_id: available[mat_id] for mat_id in sorted(present)}


def _elastic_lines(
    zones: Sequence[dict[str, float | int]], powder: dict, capsule: dict, temperature_c: float
) -> list[str]:
    """各分区线弹性单值 MP 命令(保温温度处插值)。"""
    materials = _present_materials(zones, powder, capsule)
    return [
        line
        for mat_id, material in materials.items()
        for line in _elastic_lines_for_one(material, mat_id, temperature_c)
    ]


def _elastic_lines_for_one(material: dict, mat_id: int, temperature_c: float) -> list[str]:
    """单材料线弹性 MP 命令块(EX GPa→MPa;PRXY 直取)。"""
    elastic = elastic_at_temperature(material, temperature_c)
    return [
        f"! ---- {material.get('display_name', 'material')} @ {temperature_c:.9g} C -> MAT {mat_id}",
        f"MP,EX,{mat_id},{elastic['ex_mpa']:.9g}",
        f"MP,PRXY,{mat_id},{elastic['prxy']:.9g}",
    ]


def _probe_names(count: int) -> list[str]:
    """探针命名:2 个 → core/surface;其余 probe_N。"""
    if count == len(PROBE_NAMES_DEFAULT):
        return list(PROBE_NAMES_DEFAULT)
    return [f"probe_{index}" for index in range(1, count + 1)]


def _max_probe_lag(rows: Sequence[dict]) -> float:
    """同一时刻各探针温度的极差,取全程最大(芯表滞后)。"""
    by_time: dict[float, list[float]] = {}
    for row in rows:
        by_time.setdefault(row["time_s"], []).append(row["value"])
    if not by_time:
        return 0.0
    return max(max(values) - min(values) for values in by_time.values())


def _summary_float(summary: dict, key: str) -> float:
    """summary.csv 键取 float;缺失/非数值 → INTERNAL(求解器结果写出异常)。"""
    value = summary.get(key)
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    raise KernelError("INTERNAL", f"summary.csv 缺少或不可解析的键: {key}(实际 {value!r})")


def _linspace(start: float, stop: float, count: int) -> list[float]:
    """等距采样(含端点)。"""
    if count <= 1:
        return [start]
    step = (stop - start) / (count - 1)
    return [start + index * step for index in range(count)]
