"""kernels 纯 CPU 测试 — arrhenius / shrinkage / calibrate / materials。

覆盖:默认工艺达标 0.97 与单调性、闭式解对照、物理方向性(温度/压力/时长)、
kinetics 覆盖、工艺窗口网格与 window_ok、收缩公式精确性、gmsh 补偿缩放、
标定回敛(k_ref 相对误差 <5%)、材料插值与未知名报错。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import gmsh
import numpy as np
import pytest

from ansys_hip.kernels.arrhenius import (
    DEFAULT_KINETICS,
    integrate_density,
    run_densification,
    run_process_window,
    run_sensitivity,
)
from ansys_hip.kernels.calibrate import run_calibrate
from ansys_hip.kernels.materials import (
    available_material_names,
    interpolate,
    load_material,
    run_material_query,
)
from ansys_hip.kernels.shrinkage import run_compensate, run_shrinkage_estimate
from ansys_hip.registry import KernelError
from ansys_hip.schemas import (
    CalibrateParams,
    CompensateParams,
    Cycle,
    CyclePoint,
    DensificationParams,
    ExperimentalPoint,
    GeometryRef,
    MaterialQueryParams,
    ProcessWindowParams,
    RunContext,
    SensitivityParams,
    ShrinkageEstimateParams,
)

# ---------------------------------------------------------------------------
# fixtures 与构造辅助(全部写在本文件内,不依赖 conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> RunContext:
    """临时作业目录的运行上下文。"""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    return RunContext(
        job_dir=job_dir, ansys_bin="unused", license_file="unused",
        ansys_np=1, job_timeout_s=300,
    )


def make_cycle(
    hold_temp_c: float = 900.0,
    hold_pressure_mpa: float = 120.0,
    hold_time_s: float = 10800.0,
    heatup_s: float = 3600.0,
) -> Cycle:
    """典型 HIP 曲线:1 h 升温升压 + 保温保压(默认 900℃/120MPa/3h)。"""
    return Cycle(points=[
        CyclePoint(time_s=0.0, temperature_c=20.0, pressure_mpa=0.0),
        CyclePoint(time_s=heatup_s, temperature_c=hold_temp_c, pressure_mpa=hold_pressure_mpa),
        CyclePoint(time_s=heatup_s + hold_time_s, temperature_c=hold_temp_c,
                   pressure_mpa=hold_pressure_mpa),
    ])


def make_densification_params(cycle: Cycle | None = None, **overrides) -> DensificationParams:
    """默认致密化参数(可指定曲线/关键字覆盖)。"""
    values: dict = {"cycle": cycle if cycle is not None else make_cycle()}
    values.update(overrides)
    return DensificationParams(**values)


def final_density_of(ctx: RunContext, **cycle_kwargs) -> float:
    """按给定曲线参数跑一次 densification,返回终态密度。"""
    result = run_densification(make_densification_params(make_cycle(**cycle_kwargs)), ctx)
    return result["final_density"]


@contextmanager
def gmsh_session() -> Iterator[object]:
    """测试内 gmsh 独占会话(与内核相同的清理约定)。"""
    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        yield gmsh
    finally:
        gmsh.finalize()


def write_box_step(path: Path, size_mm: tuple[float, float, float] = (10.0, 20.0, 30.0)) -> None:
    """用 gmsh OCC 生成原点处长方体 STEP(作补偿测试的输入几何)。"""
    with gmsh_session() as gmsh_module:
        occ = gmsh_module.model.occ
        occ.addBox(0.0, 0.0, 0.0, *size_mm)
        occ.synchronize()
        gmsh_module.write(str(path))


# ---------------------------------------------------------------------------
# densification
# ---------------------------------------------------------------------------


class TestDensification:

    def test_default_900c_120mpa_3h_reaches_097_with_monotonic_density(self, ctx: RunContext):
        # Act
        result = run_densification(make_densification_params(), ctx)
        # Assert
        assert result["final_density"] >= 0.97
        assert result["reached_097"] is True
        assert result["fidelity"] == "real"
        assert len(result["times_s"]) == len(result["densities"])
        assert len(result["times_s"]) <= 500
        assert result["times_s"][0] == 0.0
        assert result["densities"][0] == pytest.approx(0.65)
        # 求解器容差 rtol=1e-8 允许 ~1e-9 量级的局部抖动,物理上 D 单调不减
        assert bool(np.all(np.diff(result["densities"]) >= -1e-8)), "D(t) 应单调不减"

    def test_matches_closed_form_solution_at_constant_hold(self, ctx: RunContext):
        # Arrange:全程恒温恒压(900℃/120MPa 即参考态),闭式解可用
        cycle = Cycle(points=[
            CyclePoint(time_s=0.0, temperature_c=900.0, pressure_mpa=120.0),
            CyclePoint(time_s=10800.0, temperature_c=900.0, pressure_mpa=120.0),
        ])
        k_t = DEFAULT_KINETICS["k_ref"] * 10800.0
        expected = 0.995 - (0.995 - 0.65) * float(np.exp(-k_t))
        # Act
        result = run_densification(make_densification_params(cycle), ctx)
        # Assert
        assert result["final_density"] == pytest.approx(expected, rel=1e-6)

    def test_higher_temperature_densifies_faster(self, ctx: RunContext):
        assert final_density_of(ctx, hold_temp_c=940.0) > final_density_of(ctx, hold_temp_c=860.0)

    def test_higher_pressure_densifies_faster(self, ctx: RunContext):
        assert final_density_of(ctx, hold_pressure_mpa=140.0) > final_density_of(ctx, hold_pressure_mpa=100.0)

    def test_longer_hold_gives_higher_final_density(self, ctx: RunContext):
        assert final_density_of(ctx, hold_time_s=14400.0) > final_density_of(ctx, hold_time_s=7200.0)

    def test_kinetics_overrides_change_result_and_are_echoed(self, ctx: RunContext):
        # Arrange
        baseline = final_density_of(ctx)
        faster = make_densification_params(kinetics={"k_ref": 1e-3})
        # Act
        result = run_densification(faster, ctx)
        # Assert
        assert result["final_density"] > baseline
        assert result["kinetics"]["k_ref"] == pytest.approx(1e-3)
        assert result["kinetics"]["q_j_per_mol"] == pytest.approx(DEFAULT_KINETICS["q_j_per_mol"])

    def test_unknown_kinetics_key_raises_invalid_params(self, ctx: RunContext):
        params = make_densification_params(kinetics={"bogus_key": 1.0})
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_densification(params, ctx)

    def test_missing_cycle_raises_invalid_params(self, ctx: RunContext):
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_densification(DensificationParams(cycle=None), ctx)

    def test_csv_artifact_written_to_artifacts_dir(self, ctx: RunContext):
        # Act
        result = run_densification(make_densification_params(), ctx)
        # Assert
        assert result["artifacts"] == ["densification-curve.csv"]
        csv_path = ctx.job_dir / "artifacts" / "densification-curve.csv"
        assert csv_path.is_file()
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == "time_s,relative_density"
        assert len(lines) == len(result["times_s"]) + 1


# ---------------------------------------------------------------------------
# process-window
# ---------------------------------------------------------------------------


class TestProcessWindow:

    def test_grid_shapes_and_window_ok_consistency(self, ctx: RunContext):
        # Arrange
        params = ProcessWindowParams(
            temperatures_c=[860.0, 940.0],
            pressures_mpa=[80.0, 140.0],
            hold_times_s=[3600.0, 14400.0],
        )
        # Act
        result = run_process_window(params, ctx)
        # Assert
        assert result["fidelity"] == "real"
        assert np.shape(result["final_densities"]) == (2, 2, 2)
        assert np.shape(result["window_ok"]) == (2, 2, 2)
        for i in range(2):
            for j in range(2):
                for k in range(2):
                    density = result["final_densities"][i][j][k]
                    assert result["window_ok"][i][j][k] == (density >= 0.97)

    def test_window_corners_and_monotonicity(self, ctx: RunContext):
        # Arrange
        params = ProcessWindowParams(
            temperatures_c=[860.0, 940.0],
            pressures_mpa=[80.0, 140.0],
            hold_times_s=[3600.0, 14400.0],
        )
        # Act
        result = run_process_window(params, ctx)
        densities = np.array(result["final_densities"])
        ok = np.array(result["window_ok"])
        # Assert:温和角不达标、苛刻角达标;沿温度/压力/时长三个轴单调不减
        assert not ok[0, 0, 0]
        assert ok[1, 1, 1]
        assert densities[1, 1, 1] == densities.max()
        for axis in range(3):
            assert bool(np.all(np.diff(densities, axis=axis) >= -1e-9)), \
                f"终态密度沿轴 {axis} 应单调不减"

    def test_fastest_ok_matches_grid(self, ctx: RunContext):
        # Arrange
        params = ProcessWindowParams(
            temperatures_c=[880.0, 920.0],
            pressures_mpa=[100.0, 130.0],
            hold_times_s=[7200.0, 10800.0, 14400.0],
        )
        # Act
        result = run_process_window(params, ctx)
        # Assert:每个温度的 fastest 条目与网格自洽
        for t_index, entry in enumerate(result["fastest_ok_by_temperature"]):
            ok_holds = [
                result["grid"]["hold_times_s"][h_index]
                for p_index in range(2)
                for h_index in range(3)
                if result["window_ok"][t_index][p_index][h_index]
            ]
            if not ok_holds:
                assert entry is None
            else:
                assert entry is not None
                assert entry["hold_time_s"] == min(ok_holds)
                assert entry["final_density"] >= 0.97
                assert entry["temperature_c"] == result["grid"]["temperatures_c"][t_index]

    def test_empty_sweep_list_raises_invalid_params(self, ctx: RunContext):
        params = ProcessWindowParams(temperatures_c=[], pressures_mpa=[120.0], hold_times_s=[10800.0])
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_process_window(params, ctx)


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------


class TestSensitivity:

    def test_unknown_parameter_raises_invalid_params(self, ctx: RunContext):
        params = SensitivityParams(
            base=DensificationParams(cycle=make_cycle()),
            sweep={"not_a_param": [1.0, 2.0]},
        )
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_sensitivity(params, ctx)

    def test_oat_temperature_and_hold_time_sweeps_are_physical(self, ctx: RunContext):
        # Arrange:base 不给 cycle → 内核用默认合成曲线 900/120/3h
        params = SensitivityParams(
            base=DensificationParams(),
            sweep={
                "temperature_c": [860.0, 900.0, 940.0],
                "hold_time_s": [7200.0, 10800.0, 14400.0],
            },
        )
        # Act
        result = run_sensitivity(params, ctx)
        # Assert
        assert 0.97 <= result["base_final_density"] < 0.995
        entries = {entry["name"]: entry for entry in result["one_at_a_time"]}
        assert set(entries) == {"temperature_c", "hold_time_s"}
        for name in ("temperature_c", "hold_time_s"):
            entry = entries[name]
            assert entry["values"] == pytest.approx([860.0, 900.0, 940.0] if name == "temperature_c"
                                                    else [7200.0, 10800.0, 14400.0])
            assert entry["final_densities"] == sorted(entry["final_densities"]), \
                f"{name} 增大应使终态密度单调增加"
            assert entry["sensitivity"] > 0.0

    def test_kinetics_sweep_changes_final_density(self, ctx: RunContext):
        params = SensitivityParams(
            base=DensificationParams(cycle=make_cycle()),
            sweep={"k_ref": [1.0e-4, 5.0e-4]},
        )
        result = run_sensitivity(params, ctx)
        entry = result["one_at_a_time"][0]
        assert entry["final_densities"][1] > entry["final_densities"][0]
        assert entry["sensitivity"] == pytest.approx(
            (max(entry["final_densities"]) - min(entry["final_densities"]))
            / abs(result["base_final_density"]),
        )

    def test_pressure_override_beats_lower_pressure(self, ctx: RunContext):
        params = SensitivityParams(
            base=DensificationParams(cycle=make_cycle()),
            sweep={"hold_pressure_mpa": [100.0, 140.0]},
        )
        result = run_sensitivity(params, ctx)
        finals = result["one_at_a_time"][0]["final_densities"]
        assert finals[1] > finals[0]


# ---------------------------------------------------------------------------
# shrinkage-estimate / compensate
# ---------------------------------------------------------------------------


class TestShrinkageEstimate:

    def test_exact_volume_conservation_numbers(self, ctx: RunContext):
        # Arrange
        ratio = (0.65 / 0.97) ** (1.0 / 3.0)
        params = ShrinkageEstimateParams(
            initial_relative_density=0.65,
            final_relative_density=0.97,
            characteristic_lengths_mm={"height": 150.0, "outer_diameter": 100.0},
        )
        # Act
        result = run_shrinkage_estimate(params, ctx)
        # Assert
        assert result["fidelity"] == "real"
        assert result["linear_shrink_ratio"] == pytest.approx(0.875105, rel=1e-4)
        assert result["linear_strain"] == pytest.approx(ratio - 1.0)
        items = {item["name"]: item for item in result["items"]}
        assert items["height"]["final_mm"] == pytest.approx(150.0 * ratio)
        assert items["height"]["shrink_mm"] == pytest.approx(150.0 * (1.0 - ratio))
        assert items["outer_diameter"]["shrink_ratio"] == pytest.approx(1.0 - ratio)
        assert "formula" in result


class TestCompensate:

    def test_scales_box_step_and_bounding_box_ratio(self, ctx: RunContext, tmp_path: Path):
        # Arrange
        cavity = tmp_path / "cavity.step"
        write_box_step(cavity, size_mm=(10.0, 20.0, 30.0))
        scale = (0.97 / 0.65) ** (1.0 / 3.0)
        params = CompensateParams(
            geometry=GeometryRef(cavity_step=str(cavity)),
            initial_relative_density=0.65,
            final_relative_density=0.97,
        )
        # Act
        result = run_compensate(params, ctx)
        # Assert
        assert result["fidelity"] == "real"
        assert result["scale_factors"]["x"] == pytest.approx(scale, rel=1e-6)
        assert scale == pytest.approx(1.1426, abs=1e-3)
        output = Path(result["output_step_path"])
        assert output == ctx.job_dir / "artifacts" / "compensated.step"
        assert output.is_file()
        assert result["artifacts"] == ["compensated.step"]
        before_size = result["bounding_box"]["before"]["size_mm"]
        after_size = result["bounding_box"]["after"]["size_mm"]
        for before, after in zip(before_size, after_size):
            assert after / before == pytest.approx(scale, rel=1e-6)
        # 重新读回输出 STEP 验证几何确实被缩放
        with gmsh_session() as gmsh_module:
            gmsh_module.open(str(output))
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh_module.model.getBoundingBox(-1, -1)
        assert (xmax - xmin) == pytest.approx(10.0 * scale, rel=1e-6)
        assert (ymax - ymin) == pytest.approx(20.0 * scale, rel=1e-6)
        assert (zmax - zmin) == pytest.approx(30.0 * scale, rel=1e-6)

    def test_per_axis_note_included(self, ctx: RunContext, tmp_path: Path):
        cavity = tmp_path / "cavity.step"
        write_box_step(cavity)
        params = CompensateParams(
            geometry=GeometryRef(cavity_step=str(cavity)),
            scale_axis="per_axis",
        )
        result = run_compensate(params, ctx)
        assert result["scale_axis"] == "per_axis"
        assert "per_axis" in result["scale_axis_note"]

    def test_missing_step_raises_geometry_not_found(self, ctx: RunContext):
        params = CompensateParams(
            geometry=GeometryRef(cavity_step="/nonexistent/cavity.step"),
        )
        with pytest.raises(KernelError, match="GEOMETRY_NOT_FOUND"):
            run_compensate(params, ctx)

    def test_missing_cavity_ref_raises_invalid_params(self, ctx: RunContext):
        params = CompensateParams(geometry=GeometryRef())
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_compensate(params, ctx)


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


def make_calibration_params(fit_params: list[str]) -> CalibrateParams:
    """用已知真值参数合成实验曲线,构造标定输入。"""
    true_kinetics = {**DEFAULT_KINETICS, "k_ref": 3.5e-4, "q_j_per_mol": 160e3}
    cycle = make_cycle()
    times = np.array([1800.0, 3600.0, 5400.0, 7200.0, 9000.0, 10800.0, 14400.0])
    densities = integrate_density(cycle, true_kinetics, 0.65, 0.995, times)
    experimental = [
        ExperimentalPoint(time_s=float(t), relative_density=float(d))
        for t, d in zip(times, densities)
    ]
    return CalibrateParams(
        experimental=experimental,
        initial_relative_density=0.65,
        limiting_relative_density=0.995,
        cycle=cycle,
        fit_params=fit_params,
    )


class TestCalibrate:

    def test_recovers_kinetics_from_synthetic_data(self, ctx: RunContext):
        # Act
        result = run_calibrate(make_calibration_params(["k_ref", "q_j_per_mol"]), ctx)
        # Assert:k_ref 相对误差 <5%,残差近零,收敛成功
        assert result["fitted_params"]["k_ref"] == pytest.approx(3.5e-4, rel=0.05)
        assert result["fitted_params"]["q_j_per_mol"] == pytest.approx(160e3, rel=0.05)
        assert result["fixed_params"]["pressure_exponent"] == pytest.approx(1.5)
        assert result["residual_rms"] < 1e-6
        assert result["convergence"]["success"] is True
        assert result["fidelity"] == "real"
        fit_curve = result["fit_curve"]
        assert len(fit_curve["times_s"]) == len(fit_curve["densities"]) == 200
        assert fit_curve["experimental"]["times_s"][0] == pytest.approx(1800.0)

    def test_fit_only_k_ref(self, ctx: RunContext):
        true_kinetics = {**DEFAULT_KINETICS, "k_ref": 4.86e-4}
        cycle = make_cycle()
        times = np.array([1800.0, 5400.0, 9000.0, 14400.0])
        densities = integrate_density(cycle, true_kinetics, 0.65, 0.995, times)
        params = CalibrateParams(
            experimental=[ExperimentalPoint(time_s=float(t), relative_density=float(d))
                          for t, d in zip(times, densities)],
            cycle=cycle,
            fit_params=["k_ref"],
        )
        result = run_calibrate(params, ctx)
        assert result["fitted_params"]["k_ref"] == pytest.approx(4.86e-4, rel=0.05)
        assert "q_j_per_mol" in result["fixed_params"]

    def test_non_monotonic_times_raise_invalid_params(self, ctx: RunContext):
        params = CalibrateParams(
            experimental=[
                ExperimentalPoint(time_s=7200.0, relative_density=0.90),
                ExperimentalPoint(time_s=3600.0, relative_density=0.80),
                ExperimentalPoint(time_s=10800.0, relative_density=0.95),
            ],
            cycle=make_cycle(),
        )
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_calibrate(params, ctx)

    def test_time_beyond_cycle_raises_invalid_params(self, ctx: RunContext):
        params = CalibrateParams(
            experimental=[
                ExperimentalPoint(time_s=3600.0, relative_density=0.80),
                ExperimentalPoint(time_s=7200.0, relative_density=0.90),
                ExperimentalPoint(time_s=999999.0, relative_density=0.95),
            ],
            cycle=make_cycle(),
        )
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_calibrate(params, ctx)

    def test_missing_cycle_raises_invalid_params(self, ctx: RunContext):
        params = CalibrateParams(
            experimental=[
                ExperimentalPoint(time_s=3600.0, relative_density=0.80),
                ExperimentalPoint(time_s=7200.0, relative_density=0.90),
                ExperimentalPoint(time_s=10800.0, relative_density=0.95),
            ],
            cycle=None,
        )
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_calibrate(params, ctx)


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------


class TestMaterials:

    POINTS = [{"temperature_c": 20, "value": 113.0}, {"temperature_c": 900, "value": 70.0}]

    def test_interpolate_endpoints_midpoint_and_clamping(self):
        assert interpolate(self.POINTS, 20.0) == pytest.approx(113.0)
        assert interpolate(self.POINTS, 900.0) == pytest.approx(70.0)
        assert interpolate(self.POINTS, 460.0) == pytest.approx(91.5)
        assert interpolate(self.POINTS, -40.0) == pytest.approx(113.0)   # 越界取端点
        assert interpolate(self.POINTS, 1200.0) == pytest.approx(70.0)

    def test_interpolate_handles_unsorted_points(self):
        unsorted = [{"temperature_c": 900, "value": 70.0}, {"temperature_c": 20, "value": 113.0}]
        assert interpolate(unsorted, 460.0) == pytest.approx(91.5)

    def test_unknown_material_raises_valueerror(self):
        with pytest.raises(ValueError, match="未知材料"):
            load_material("inconel718")

    def test_material_query_full_properties(self, ctx: RunContext):
        params = MaterialQueryParams(material="tc4", temperatures_c=[20.0, 600.0])
        result = run_material_query(params, ctx)
        assert result["fidelity"] == "real"
        assert result["density_kg_m3"] == 4430
        assert [row["temperature_c"] for row in result["rows"]] == [20.0, 600.0]
        assert result["rows"][0]["properties"]["young_modulus_gpa"] == pytest.approx(113.0)
        assert result["rows"][1]["properties"]["young_modulus_gpa"] == pytest.approx(85.0)
        assert result["rows"][1]["properties"]["yield_stress_mpa"] == pytest.approx(350.0)
        assert set(result["rows"][0]["properties"]) == {
            "young_modulus_gpa", "yield_stress_mpa", "ultimate_stress_mpa",
            "thermal_conductivity_w_m_k", "specific_heat_j_kgk", "poisson_ratio",
        }
        assert result["source_notes"]

    def test_poisson_ratio_single_point_list_is_constant(self):
        tc4 = load_material("tc4")
        points = tc4["properties"]["poisson_ratio"]
        assert interpolate(points, 20.0) == pytest.approx(0.34)
        assert interpolate(points, 900.0) == pytest.approx(0.34)   # 单点 + 端点钳位 → 恒值

    def test_load_material_returns_deep_copy(self):
        material = load_material("20steel")
        material["properties"]["young_modulus_gpa"][0]["value"] = 999.0
        assert load_material("20steel")["properties"]["young_modulus_gpa"][0]["value"] == 205.0

    def test_available_material_names_sorted(self):
        assert available_material_names() == ["20steel", "tc4"]

    def test_material_query_limited_properties(self, ctx: RunContext):
        params = MaterialQueryParams(
            material="20steel", temperatures_c=[900.0],
            properties=["yield_stress_mpa"],
        )
        result = run_material_query(params, ctx)
        # 关键点:20 钢 900℃ 屈服 ~20-30 MPa
        yield_900 = result["rows"][0]["properties"]["yield_stress_mpa"]
        assert 20.0 <= yield_900 <= 30.0
        assert set(result["rows"][0]["properties"]) == {"yield_stress_mpa"}

    def test_material_query_missing_property_is_null_and_noted(self, ctx: RunContext):
        params = MaterialQueryParams(
            material="tc4", temperatures_c=[20.0],
            properties=["young_modulus_gpa", "creep_rate_mpa"],
        )
        result = run_material_query(params, ctx)
        properties = result["rows"][0]["properties"]
        assert properties["young_modulus_gpa"] == pytest.approx(113.0)
        assert properties["creep_rate_mpa"] is None
        assert any("null" in note for note in result["source_notes"])

    def test_material_query_unknown_material_raises_invalid_params(self, ctx: RunContext):
        params = MaterialQueryParams(material="titanium_magic", temperatures_c=[20.0])
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_material_query(params, ctx)
