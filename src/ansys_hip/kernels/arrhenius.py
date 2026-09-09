"""Arrhenius 致密化内核 — densification / process-window / sensitivity。

物理模型(HIPForm python 线同族,文献校核):
    dD/dt = k_ref · exp[-Q/R · (1/T - 1/T_ref)] · (p/p_ref)^n · (D_lim - D)

其中 T 为开尔文温度,R = 8.314 J/(mol·K)。默认动力学参数(TC4,自洽标定:
900℃/120MPa 保温 3h 使相对密度从 0.65 → ≥0.97):
    k_ref = 2.43e-4 1/s(参考态 T_ref = 900℃, p_ref = 120 MPa)
    Q = 150 kJ/mol, n = 1.5;D_lim 由参数 limiting_relative_density 给出(默认 0.995)。

本模块同时是 calibrate 内核的模型底座(integrate_density / resolve_kinetics 被其复用)。
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from pydantic import ValidationError
from scipy.integrate import solve_ivp

from . import artifact_dir
from ..registry import KernelError
from ..schemas import (
    Cycle,
    CyclePoint,
    DensificationParams,
    ProcessWindowParams,
    RunContext,
    SensitivityParams,
)

# ---------------------------------------------------------------------------
# 物理与数值常量
# ---------------------------------------------------------------------------

GAS_CONSTANT_J_PER_MOL_K: float = 8.314
CELSIUS_TO_KELVIN: float = 273.15
TARGET_DENSITY: float = 0.97          # 工艺窗口达标阈值(规格要求 ≥0.97)

ODE_RTOL: float = 1e-8                # solve_ivp 相对容差(契约冻结)
ODE_ATOL: float = 1e-10

# 默认 Arrhenius 动力学参数(参考态:900℃ / 120 MPa)
DEFAULT_KINETICS: dict[str, float] = {
    "k_ref": 2.43e-4,
    "q_j_per_mol": 150e3,
    "pressure_exponent": 1.5,
    "t_ref_c": 900.0,
    "p_ref_mpa": 120.0,
}
# 必须为正的动力学键(t_ref_c 允许负温但有下限)
_POSITIVE_KINETIC_KEYS: tuple[str, ...] = (
    "k_ref", "q_j_per_mol", "pressure_exponent", "p_ref_mpa",
)

# 默认合成工艺曲线:1 h 升温升压 + 3 h 保温保压(与 config/service.yaml 默认规格一致)
DEFAULT_ROOM_TEMP_C: float = 20.0
DEFAULT_HOLD_TEMP_C: float = 900.0
DEFAULT_HOLD_PRESSURE_MPA: float = 120.0
DEFAULT_HOLD_TIME_S: float = 10800.0
SYNTHETIC_HEATUP_S: float = 3600.0

MAX_OUTPUT_SAMPLES: int = 500         # D-t 曲线输出采样上限
MAX_CYCLE_TEMPERATURE_C: float = 2500.0   # 与 schemas.CyclePoint 约束一致
MAX_CYCLE_PRESSURE_MPA: float = 500.0

DENSIFICATION_CSV: str = "densification-curve.csv"

MODEL_FORMULA: str = (
    "dD/dt = k_ref·exp[-Q/R·(1/T - 1/T_ref)]·(p/p_ref)^n·(D_lim - D)"
)

# sensitivity 支持的 OAT 参数键(其余 → INVALID_PARAMS)
SENSITIVITY_PARAM_KEYS: frozenset[str] = frozenset({
    "initial_relative_density",
    "temperature_c",
    "hold_pressure_mpa",
    "hold_time_s",
    "k_ref",
    "q_j_per_mol",
    "pressure_exponent",
})


def _require(condition: bool, message: str) -> None:
    """条件不满足即抛 KernelError(INVALID_PARAMS)。"""
    if not condition:
        raise KernelError("INVALID_PARAMS", message)


# ---------------------------------------------------------------------------
# 动力学参数解析
# ---------------------------------------------------------------------------

def resolve_kinetics(overrides: dict[str, float] | None) -> dict[str, float]:
    """合并默认动力学参数与用户覆盖,返回新 dict(不改入参)。

    未知键或非正值 → KernelError(INVALID_PARAMS)。
    """
    supplied = overrides or {}
    unknown = sorted(set(supplied) - set(DEFAULT_KINETICS))
    _require(not unknown, f"未知动力学参数键: {unknown};支持: {sorted(DEFAULT_KINETICS)}")
    merged = {**DEFAULT_KINETICS, **{k: float(v) for k, v in supplied.items()}}
    for key in _POSITIVE_KINETIC_KEYS:
        _require(merged[key] > 0.0, f"动力学参数 {key} 须为正数,得到 {merged[key]}")
    _require(merged["t_ref_c"] > -CELSIUS_TO_KELVIN, f"t_ref_c 须高于绝对零度,得到 {merged['t_ref_c']}")
    return merged


# ---------------------------------------------------------------------------
# 工艺曲线工具
# ---------------------------------------------------------------------------

def _cycle_arrays(cycle: Cycle) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """曲线 → (times, temperatures, pressures) 数组,并校验时间严格递增。"""
    times = np.array([p.time_s for p in cycle.points], dtype=float)
    temps = np.array([p.temperature_c for p in cycle.points], dtype=float)
    pressures = np.array([p.pressure_mpa for p in cycle.points], dtype=float)
    _require(
        times.size >= 2 and bool(np.all(np.diff(times) > 0.0)),
        "cycle.points 需 ≥2 个控制点且 time_s 严格递增",
    )
    return times, temps, pressures


def synthetic_cycle(
    temperature_c: float,
    pressure_mpa: float,
    hold_time_s: float,
    heatup_time_s: float = SYNTHETIC_HEATUP_S,
) -> Cycle:
    """合成工艺曲线:0 s(室温/零压)→ 升温结束(保温 T/p)→ 保温结束(T/p)。

    只含升温+保温段(忽略降温),process-window / sensitivity 用。
    """
    try:
        return Cycle(points=[
            CyclePoint(time_s=0.0, temperature_c=DEFAULT_ROOM_TEMP_C, pressure_mpa=0.0),
            CyclePoint(time_s=heatup_time_s, temperature_c=temperature_c, pressure_mpa=pressure_mpa),
            CyclePoint(time_s=heatup_time_s + hold_time_s, temperature_c=temperature_c, pressure_mpa=pressure_mpa),
        ])
    except ValidationError as exc:
        raise KernelError("INVALID_PARAMS", f"合成工艺曲线参数非法: {exc}") from exc


def default_cycle() -> Cycle:
    """默认合成曲线:900℃ / 120 MPa / 保温 3 h(1 h 升温)。"""
    return synthetic_cycle(DEFAULT_HOLD_TEMP_C, DEFAULT_HOLD_PRESSURE_MPA, DEFAULT_HOLD_TIME_S)


def _plateau_value(cycle: Cycle, field: str) -> float:
    """取曲线上某字段(temperature_c / pressure_mpa)的平台峰值。"""
    return max(getattr(p, field) for p in cycle.points)


def _override_plateau(cycle: Cycle, field: str, value: float) -> Cycle:
    """把平台上(该字段等于峰值)的控制点替换为 value,其余点不变(返回新 Cycle)。"""
    peak = _plateau_value(cycle, field)
    points = [
        p.model_copy(update={field: value}) if getattr(p, field) == peak else p
        for p in cycle.points
    ]
    return Cycle(points=points)


# ---------------------------------------------------------------------------
# ODE 积分核心
# ---------------------------------------------------------------------------

def densification_rate(
    temperature_c: float,
    pressure_mpa: float,
    density: float,
    kinetics: dict[str, float],
    limiting_density: float,
) -> float:
    """Arrhenius 致密化速率单点求值(温度 ℃,压力 MPa,密度为相对密度)。"""
    temp_k = temperature_c + CELSIUS_TO_KELVIN
    temp_ref_k = kinetics["t_ref_c"] + CELSIUS_TO_KELVIN
    arrhenius = float(np.exp(
        -(kinetics["q_j_per_mol"] / GAS_CONSTANT_J_PER_MOL_K) * (1.0 / temp_k - 1.0 / temp_ref_k)
    ))
    pressure_term = (pressure_mpa / kinetics["p_ref_mpa"]) ** kinetics["pressure_exponent"]
    return kinetics["k_ref"] * arrhenius * pressure_term * (limiting_density - density)


def integrate_density(
    cycle: Cycle,
    kinetics: dict[str, float],
    initial_density: float,
    limiting_density: float,
    eval_times: np.ndarray,
) -> np.ndarray:
    """沿 cycle(分段线性 T(t)/p(t))积分 ODE,返回 eval_times 处的相对密度。

    eval_times 须升序且落在 [0, 曲线总时长] 内;求解失败抛
    KernelError(CONVERGENCE_FAILED)。
    """
    times, temps, pressures = _cycle_arrays(cycle)
    eval_times = np.asarray(eval_times, dtype=float)
    t_end = float(times[-1])
    _require(
        eval_times.size > 0 and eval_times[0] >= 0.0 and eval_times[-1] <= t_end + 1e-9,
        f"求值时刻须升序且落在 [0, {t_end:g}] s 内",
    )

    def rhs(t: float, y: np.ndarray) -> np.ndarray:
        rate = densification_rate(
            float(np.interp(t, times, temps)),
            float(np.interp(t, times, pressures)),
            float(y[0]),
            kinetics,
            limiting_density,
        )
        return np.array([rate])

    solution = solve_ivp(
        rhs,
        (0.0, t_end),
        [float(initial_density)],
        t_eval=eval_times,
        rtol=ODE_RTOL,
        atol=ODE_ATOL,
        method="RK45",
    )
    if not solution.success:
        raise KernelError("CONVERGENCE_FAILED", f"致密化 ODE 积分失败: {solution.message}")
    return solution.y[0]


def _sample_times(t_end: float) -> np.ndarray:
    """[0, t_end] 均匀采样 ≤ MAX_OUTPUT_SAMPLES 点(始终含首末)。"""
    count = min(MAX_OUTPUT_SAMPLES, max(2, int(t_end) + 1))
    return np.linspace(0.0, t_end, count)


def _final_density(
    cycle: Cycle,
    kinetics: dict[str, float],
    initial_density: float,
    limiting_density: float,
) -> float:
    """便捷封装:只取曲线终点的相对密度。"""
    times, _, _ = _cycle_arrays(cycle)
    return float(integrate_density(
        cycle, kinetics, initial_density, limiting_density, np.array([float(times[-1])]),
    )[0])


def _write_curve_csv(path: Path, times: np.ndarray, densities: np.ndarray) -> None:
    """把 D-t 曲线写为 CSV 工件(time_s, relative_density 两列)。"""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_s", "relative_density"])
        writer.writerows((f"{t:.1f}", f"{d:.9f}") for t, d in zip(times, densities))


# ---------------------------------------------------------------------------
# 方法 densification
# ---------------------------------------------------------------------------

def run_densification(params: DensificationParams, ctx: RunContext) -> dict:
    """致密化曲线:沿工艺曲线积分 Arrhenius 律,回答『能否到 0.97』。"""
    if params.cycle is None:
        raise KernelError(
            "INVALID_PARAMS",
            "densification 需要 cycle(应由配置合并层注入默认工艺曲线 900℃/120MPa/3h)",
        )
    kinetics = resolve_kinetics(params.kinetics)
    times, temps, pressures = _cycle_arrays(params.cycle)
    sample_times = _sample_times(float(times[-1]))
    densities = integrate_density(
        params.cycle, kinetics,
        params.initial_relative_density, params.limiting_relative_density,
        sample_times,
    )
    final_density = float(densities[-1])

    _write_curve_csv(artifact_dir(ctx) / DENSIFICATION_CSV, sample_times, densities)

    return {
        "initial_relative_density": params.initial_relative_density,
        "limiting_relative_density": params.limiting_relative_density,
        "final_density": final_density,
        "reached_097": final_density >= TARGET_DENSITY,
        "target_density": TARGET_DENSITY,
        "times_s": [float(t) for t in sample_times],
        "densities": [float(d) for d in densities],
        "cycle": {
            "duration_s": float(times[-1]),
            "n_points": int(times.size),
            "max_temperature_c": float(temps.max()),
            "max_pressure_mpa": float(pressures.max()),
        },
        "kinetics": kinetics,
        "kinetics_source": "默认=TC4 标定(阶段 1 忽略 material 选择,可用 kinetics 覆盖)",
        "model": MODEL_FORMULA,
        "artifacts": [DENSIFICATION_CSV],
        "fidelity": "real",
    }


# ---------------------------------------------------------------------------
# 方法 process-window
# ---------------------------------------------------------------------------

def _validated_sweep_list(
    values: list[float],
    name: str,
    lower: float,
    upper: float,
    *,
    inclusive_lower: bool = True,
) -> list[float]:
    """校验扫参列表非空且取值在界内,返回 float 列表(新对象)。"""
    _require(bool(values), f"{name} 扫参列表不能为空")
    checked: list[float] = []
    for value in values:
        value = float(value)
        in_range = value >= lower if inclusive_lower else value > lower
        _require(in_range and value <= upper,
                 f"{name} 取值 {value} 超出允许范围 ({lower}, {upper}]")
        checked.append(value)
    return checked


def _fastest_ok_per_temperature(
    temperatures: list[float],
    pressures: list[float],
    hold_times: list[float],
    final_densities: list[list[list[float]]],
    window_ok: list[list[list[bool]]],
) -> list[dict | None]:
    """对每个温度找最快达标组合(保温时长最短,并列取压力最低);无达标返回 None。"""
    summary: list[dict | None] = []
    for t_index, temp_c in enumerate(temperatures):
        candidates = [
            (hold, press, final_densities[t_index][p_index][h_index])
            for p_index, press in enumerate(pressures)
            for h_index, hold in enumerate(hold_times)
            if window_ok[t_index][p_index][h_index]
        ]
        if not candidates:
            summary.append(None)
            continue
        best_hold, best_press, best_density = min(candidates, key=lambda c: (c[0], c[1]))
        summary.append({
            "temperature_c": temp_c,
            "pressure_mpa": best_press,
            "hold_time_s": best_hold,
            "final_density": best_density,
        })
    return summary


def run_process_window(params: ProcessWindowParams, ctx: RunContext) -> dict:
    """工艺窗口扫参:T×p×t_hold 合成曲线(1 h 升温+保温)→ 终态密度与达标窗口。"""
    temperatures = _validated_sweep_list(
        params.temperatures_c, "temperatures_c", 0.0, MAX_CYCLE_TEMPERATURE_C)
    pressures = _validated_sweep_list(
        params.pressures_mpa, "pressures_mpa", 0.0, MAX_CYCLE_PRESSURE_MPA)
    hold_times = _validated_sweep_list(
        params.hold_times_s, "hold_times_s", 0.0, float("inf"), inclusive_lower=False)

    base = params.base
    kinetics = resolve_kinetics(base.kinetics)
    final_densities: list[list[list[float]]] = []
    window_ok: list[list[list[bool]]] = []
    for temp_c in temperatures:
        density_rows: list[list[float]] = []
        ok_rows: list[list[bool]] = []
        for press in pressures:
            density_row: list[float] = []
            ok_row: list[bool] = []
            for hold in hold_times:
                curve = synthetic_cycle(temp_c, press, hold)
                density = _final_density(
                    curve, kinetics,
                    base.initial_relative_density, base.limiting_relative_density,
                )
                density_row.append(density)
                ok_row.append(density >= TARGET_DENSITY)
            density_rows.append(density_row)
            ok_rows.append(ok_row)
        final_densities.append(density_rows)
        window_ok.append(ok_rows)

    return {
        "grid": {
            "temperatures_c": temperatures,
            "pressures_mpa": pressures,
            "hold_times_s": hold_times,
        },
        "final_densities": final_densities,
        "window_ok": window_ok,
        "target_density": TARGET_DENSITY,
        "fastest_ok_by_temperature": _fastest_ok_per_temperature(
            temperatures, pressures, hold_times, final_densities, window_ok,
        ),
        "base": {
            "initial_relative_density": base.initial_relative_density,
            "limiting_relative_density": base.limiting_relative_density,
        },
        "kinetics": kinetics,
        "model": MODEL_FORMULA,
        "fidelity": "real",
    }


# ---------------------------------------------------------------------------
# 方法 sensitivity
# ---------------------------------------------------------------------------

def _sensitivity_case_final(
    base: DensificationParams,
    base_cycle: Cycle,
    kinetics: dict[str, float],
    name: str,
    value: float,
) -> float:
    """OAT 单案例:按参数键构造变体(新对象),返回该案例的终态密度。"""
    if name == "initial_relative_density":
        _require(0.0 < value < 1.0, f"initial_relative_density 须在 (0, 1),得到 {value}")
        return _final_density(
            base_cycle, kinetics, value, base.limiting_relative_density)
    if name == "temperature_c":
        _require(0.0 <= value <= MAX_CYCLE_TEMPERATURE_C,
                 f"temperature_c 须在 [0, {MAX_CYCLE_TEMPERATURE_C:g}] ℃,得到 {value}")
        cycle = _override_plateau(base_cycle, "temperature_c", value)
        return _final_density(cycle, kinetics,
                              base.initial_relative_density, base.limiting_relative_density)
    if name == "hold_pressure_mpa":
        _require(0.0 <= value <= MAX_CYCLE_PRESSURE_MPA,
                 f"hold_pressure_mpa 须在 [0, {MAX_CYCLE_PRESSURE_MPA:g}] MPa,得到 {value}")
        cycle = _override_plateau(base_cycle, "pressure_mpa", value)
        return _final_density(cycle, kinetics,
                              base.initial_relative_density, base.limiting_relative_density)
    if name == "hold_time_s":
        _require(value > 0.0, f"hold_time_s 须为正,得到 {value}")
        cycle = synthetic_cycle(
            _plateau_value(base_cycle, "temperature_c"),
            _plateau_value(base_cycle, "pressure_mpa"),
            value,
        )
        return _final_density(cycle, kinetics,
                              base.initial_relative_density, base.limiting_relative_density)
    # 动力学参数:k_ref / q_j_per_mol / pressure_exponent(resolve_kinetics 已校验正值)
    merged = resolve_kinetics({**base.kinetics, name: value})
    return _final_density(base_cycle, merged,
                          base.initial_relative_density, base.limiting_relative_density)


def run_sensitivity(params: SensitivityParams, ctx: RunContext) -> dict:
    """OAT 参数敏感性:逐参数扫值跑致密化核,输出归一化范围敏感度。"""
    base = params.base
    base_cycle = base.cycle if base.cycle is not None else default_cycle()
    kinetics = resolve_kinetics(base.kinetics)
    base_final = _final_density(
        base_cycle, kinetics, base.initial_relative_density, base.limiting_relative_density)

    one_at_a_time: list[dict] = []
    for name, raw_values in params.sweep.items():
        _require(name in SENSITIVITY_PARAM_KEYS,
                 f"不支持的敏感性参数: {name!r};支持: {sorted(SENSITIVITY_PARAM_KEYS)}")
        _require(bool(raw_values), f"参数 {name} 的取值列表不能为空")
        values = [float(v) for v in raw_values]
        finals = [
            _sensitivity_case_final(base, base_cycle, kinetics, name, value)
            for value in values
        ]
        spread = max(finals) - min(finals)
        sensitivity = spread / abs(base_final) if base_final != 0.0 else 0.0
        one_at_a_time.append({
            "name": name,
            "values": values,
            "final_densities": finals,
            "sensitivity": sensitivity,
        })

    return {
        "base_final_density": base_final,
        "one_at_a_time": one_at_a_time,
        "target_density": TARGET_DENSITY,
        "kinetics": kinetics,
        "model": MODEL_FORMULA,
        "fidelity": "real",
    }
