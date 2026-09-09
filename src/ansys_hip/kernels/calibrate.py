"""标定内核 — calibrate:实验 D-t 数据 → Arrhenius 动力学参数。

残差 = 模型 D(t_i) − 实验 D_i,模型复用 arrhenius.integrate_density 沿同一
工艺曲线积分。scipy.optimize.least_squares 拟合 fit_params 子集:
    k_ref 走 log10 空间(保证正值);q_j_per_mol / pressure_exponent 线性。
未拟合的参数固定为默认动力学值(CalibrateParams 不提供覆盖入口)。
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from ..registry import KernelError
from ..schemas import CalibrateParams, RunContext
from .arrhenius import DEFAULT_KINETICS, integrate_density, resolve_kinetics

# 拟合参数边界(k_ref 为原始物理空间,内部转 log10)
K_REF_BOUNDS: tuple[float, float] = (1e-8, 1e-1)
Q_BOUNDS: tuple[float, float] = (50e3, 400e3)
PRESSURE_EXPONENT_BOUNDS: tuple[float, float] = (0.5, 4.0)

MIN_EXPERIMENTAL_POINTS: int = 3
FIT_CURVE_MAX_POINTS: int = 200


def _validated_experiment_times(params: CalibrateParams, cycle_end_s: float) -> np.ndarray:
    """校验实验时间序列:≥3 点、严格递增且落在 [0, 曲线总时长] 内。"""
    times = np.array([pt.time_s for pt in params.experimental], dtype=float)
    if times.size < MIN_EXPERIMENTAL_POINTS:
        raise KernelError(
            "INVALID_PARAMS", f"实验数据至少 {MIN_EXPERIMENTAL_POINTS} 点,得到 {times.size} 点")
    if not bool(np.all(np.diff(times) > 0.0)):
        raise KernelError("INVALID_PARAMS", "实验数据的 time_s 须严格递增(按时间排序)")
    if times[-1] > cycle_end_s + 1e-9 or times[0] < 0.0:
        raise KernelError(
            "INVALID_PARAMS",
            f"实验时间须落在 [0, {cycle_end_s:g}] s(工艺曲线时长)内",
        )
    return times


def _deduplicated_fit_names(fit_params: list[str]) -> list[str]:
    """去重保序的拟合参数名。"""
    return list(dict.fromkeys(fit_params))


def _initial_vector(fit_names: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """拟合变量的初值与上下界(k_ref 用 log10 空间)。"""
    config = {
        "k_ref": (np.log10(DEFAULT_KINETICS["k_ref"]),
                  np.log10(K_REF_BOUNDS[0]), np.log10(K_REF_BOUNDS[1])),
        "q_j_per_mol": (DEFAULT_KINETICS["q_j_per_mol"], Q_BOUNDS[0], Q_BOUNDS[1]),
        "pressure_exponent": (DEFAULT_KINETICS["pressure_exponent"],
                              PRESSURE_EXPONENT_BOUNDS[0], PRESSURE_EXPONENT_BOUNDS[1]),
    }
    unknown = [name for name in fit_names if name not in config]
    if unknown:
        raise KernelError("INVALID_PARAMS", f"不可拟合的参数: {unknown};可选: {sorted(config)}")
    triples = [config[name] for name in fit_names]
    x0 = np.array([t[0] for t in triples])
    lower = np.array([t[1] for t in triples])
    upper = np.array([t[2] for t in triples])
    return x0, lower, upper


def _vector_to_kinetics(x: np.ndarray, fit_names: list[str]) -> dict[str, float]:
    """拟合向量 → 完整动力学参数集(新 dict;k_ref 从 log10 还原)。"""
    merged = {**DEFAULT_KINETICS}
    for name, value in zip(fit_names, x):
        merged[name] = float(10.0 ** value) if name == "k_ref" else float(value)
    return merged


def run_calibrate(params: CalibrateParams, ctx: RunContext) -> dict:
    """最小二乘标定 Arrhenius 动力学参数,输出拟合值/残差/拟合曲线/收敛信息。"""
    if params.cycle is None:
        raise KernelError(
            "INVALID_PARAMS",
            "calibrate 需要 cycle(实验对应的工艺曲线;应由配置合并层注入)",
        )
    fit_names = _deduplicated_fit_names(params.fit_params)
    fixed_kinetics = resolve_kinetics(None)
    cycle_times = [pt.time_s for pt in params.cycle.points]
    times = _validated_experiment_times(params, max(cycle_times))
    densities_exp = np.array(
        [pt.relative_density for pt in params.experimental], dtype=float)
    x0, lower, upper = _initial_vector(fit_names)

    def residuals(x: np.ndarray) -> np.ndarray:
        kinetics = _vector_to_kinetics(x, fit_names)
        model = integrate_density(
            params.cycle, kinetics,   # type: ignore[arg-type]  # cycle 已判非 None
            params.initial_relative_density, params.limiting_relative_density,
            times,
        )
        return model - densities_exp

    try:
        result = least_squares(
            residuals, x0, bounds=(lower, upper),
            xtol=1e-12, ftol=1e-12, gtol=1e-12,
        )
    except KernelError:
        raise
    except Exception as exc:
        raise KernelError("CONVERGENCE_FAILED", f"最小二乘求解异常: {exc}") from exc
    if not result.success:
        raise KernelError("CONVERGENCE_FAILED", f"最小二乘未收敛: {result.message}")

    fitted_kinetics = _vector_to_kinetics(result.x, fit_names)
    fit_curve_times = np.linspace(0.0, float(times[-1]), FIT_CURVE_MAX_POINTS)
    fit_curve_densities = integrate_density(
        params.cycle, fitted_kinetics,
        params.initial_relative_density, params.limiting_relative_density,
        fit_curve_times,
    )
    residual_rms = float(np.sqrt(np.mean(result.fun ** 2)))

    return {
        "fitted_params": {name: fitted_kinetics[name] for name in fit_names},
        "fixed_params": {
            name: value for name, value in fixed_kinetics.items() if name not in fit_names
        },
        "bounds": {
            "k_ref": list(K_REF_BOUNDS),
            "q_j_per_mol": list(Q_BOUNDS),
            "pressure_exponent": list(PRESSURE_EXPONENT_BOUNDS),
        },
        "residual_rms": residual_rms,
        "fit_curve": {
            "times_s": [float(t) for t in fit_curve_times],
            "densities": [float(d) for d in fit_curve_densities],
            "experimental": {
                "times_s": [float(t) for t in times],
                "relative_densities": [float(d) for d in densities_exp],
            },
        },
        "convergence": {
            "success": bool(result.success),
            "status": int(result.status),
            "message": str(result.message),
            "nfev": int(result.nfev),
            "cost": float(result.cost),
            "optimality": float(result.optimality),
        },
        "model": "dD/dt = k_ref·exp[-Q/R·(1/T - 1/T_ref)]·(p/p_ref)^n·(D_lim - D)",
        "fidelity": "real",
    }
