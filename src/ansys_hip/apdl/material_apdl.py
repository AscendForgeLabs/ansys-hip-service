"""APDL 材料命令块 — materials.yaml 点列 → MPTEMP/MPDATA 命令流。

单位换算(mm/MPa/s/℃ 一致单位制):
    DENS  kg/m³   × 1e-12 → tonne/mm³
    C     J/(kg·K) × 1e6  → mJ/(tonne·K)
    KXX   W/(m·K) × 1     → mW/(mm·K)(数值相等)
    EX    GPa    × 1e3    → MPa
    PRXY  无量纲直取

温度相关表(MPTEMP/MPDATA)用于热瞬态;力学冒烟取保温温度单值
(elastic_at_temperature 插值 → 模板 MP,EX/MP,PRXY)。
Gurson/蠕变本构为阶段 2 内容(TB 命令位置见 structural TODO)。
"""

from __future__ import annotations

from typing import Sequence

from ..registry import KernelError

# (材料库属性键, APDL 标签, 数值比例) — kind → thermal | structural
PROPERTY_SPECS: dict[str, tuple[tuple[str, str, float], ...]] = {
    "thermal": (
        ("thermal_conductivity_w_m_k", "KXX", 1.0),
        ("specific_heat_j_kgk", "C", 1e6),
    ),
    "structural": (
        ("young_modulus_gpa", "EX", 1e3),
        ("poisson_ratio", "PRXY", 1.0),
    ),
}

# MPTEMP/MPDATA 每条命令最多 6 个数据点
POINTS_PER_COMMAND = 6


def material_commands(material: dict, mat_id: int, kind: str) -> list[str]:
    """整材料命令块(kind: "thermal" → DENS/KXX/C;"structural" → DENS/EX/PRXY)。

    仅输出材料库中存在的属性;密度为标量,单点 MPDATA。
    """
    specs = PROPERTY_SPECS.get(kind)
    if specs is None:
        raise KernelError("INTERNAL", f"未知材料命令块类型: {kind}")
    name = material.get("display_name", "material")
    lines = [f"! ---- {name} -> MAT {mat_id}({kind})"]
    density = material.get("density_kg_m3")
    if isinstance(density, (int, float)):
        lines.append(f"MPDATA,DENS,{mat_id},1,{float(density) * 1e-12:.9g}")
    properties = material.get("properties", {})
    for key, label, scale in specs:
        points = properties.get(key)
        if not points:
            continue
        temps = [float(point["temperature_c"]) for point in points]
        values = [float(point["value"]) * scale for point in points]
        lines.extend(_mptemp_lines(temps))
        lines.extend(_mpdata_lines(label, mat_id, values))
    return lines


def elastic_at_temperature(material: dict, temperature_c: float) -> dict:
    """保温温度处线性插值的弹性常数 → {"ex_mpa", "prxy"}(冒烟模板单值 MP 用)。"""
    properties = material.get("properties", {})
    ex_gpa = _interp(properties.get("young_modulus_gpa"), temperature_c)
    prxy = _interp(properties.get("poisson_ratio"), temperature_c)
    return {"ex_mpa": ex_gpa * 1e3, "prxy": prxy}


def _mptemp_lines(temps: Sequence[float]) -> list[str]:
    """MPTEMP 命令(每条 ≤6 点,STLOC 续行)。"""
    lines: list[str] = []
    for start in range(0, len(temps), POINTS_PER_COMMAND):
        chunk = temps[start:start + POINTS_PER_COMMAND]
        location = start + 1
        lines.append(",".join(["MPTEMP", str(location)] + [f"{t:.9g}" for t in chunk]))
    return lines


def _mpdata_lines(label: str, mat_id: int, values: Sequence[float]) -> list[str]:
    """MPDATA 命令(每条 ≤6 点,STLOC 续行)。"""
    lines: list[str] = []
    for start in range(0, len(values), POINTS_PER_COMMAND):
        chunk = values[start:start + POINTS_PER_COMMAND]
        location = start + 1
        lines.append(",".join(
            ["MPDATA", label, str(mat_id), str(location)] + [f"{v:.9g}" for v in chunk]
        ))
    return lines


def _interp(points: Sequence[dict] | None, temperature_c: float) -> float:
    """温度点列线性插值,端点外取端值(与 kernels.materials.interpolate 语义一致)。"""
    if not points:
        raise KernelError("INTERNAL", "材料属性点列为空,无法插值")
    ordered = sorted((float(p["temperature_c"]), float(p["value"])) for p in points)
    if temperature_c <= ordered[0][0]:
        return ordered[0][1]
    if temperature_c >= ordered[-1][0]:
        return ordered[-1][1]
    for (t_lo, v_lo), (t_hi, v_hi) in zip(ordered, ordered[1:]):
        if t_lo <= temperature_c <= t_hi:
            fraction = (temperature_c - t_lo) / (t_hi - t_lo) if t_hi > t_lo else 0.0
            return v_lo + fraction * (v_hi - v_lo)
    return ordered[-1][1]
