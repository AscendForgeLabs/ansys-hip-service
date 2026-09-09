"""T4 网格/模板/结果解析测试 — 不运行 MAPDL(真求解由 lead 串行验证)。

覆盖:
    - gmsh 嵌套盒 STEP 夹具(包套=外壳挖内腔;型腔=内芯)→ mesh_capsule_powder
    - .cdb 命令流内容与 exterior_faces_from_cdb 面提取
    - 4 个 APDL 模板渲染关键词(StrictUndefined 下上下文键齐全)
    - results.py summary/series 解析与错误行提取
    - material_apdl 单位换算(MPTEMP/MPDATA 块、保温温度插值)
    - fem 纯助手(分区/分段/探针命名/滞后)
    - @slow: ~/tempt 真 STEP(仅 gmsh 网格,无 MAPDL)
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from ansys_hip.apdl.material_apdl import (
    elastic_at_temperature,
    material_commands,
)
from ansys_hip.apdl.render import render, write_input
from ansys_hip.kernels.fem import (
    _axisym_domain,
    _axisym_zones,
    _elastic_lines,
    _max_probe_lag,
    _nsubsteps,
    _probe_names,
    _segments,
    run_mesh,
)
from ansys_hip.kernels.materials import load_material
from ansys_hip.mesh import exterior_faces_from_cdb, mesh_capsule_powder, step_bbox
from ansys_hip.registry import KernelError
from ansys_hip.results import extract_error_lines, parse_series_csv, parse_summary_csv
from ansys_hip.schemas import (
    AxisymHipParams,
    AxisymProfile,
    Cycle,
    CyclePoint,
    GeometryRef,
    MeshMethodParams,
    RunContext,
)

# --- 嵌套盒夹具几何(包套外半宽 30,内腔半宽 20,内腔 z 10..50 严格内含) ---
CAPSULE_R = 30.0
CAVITY_R = 20.0
HEIGHT = 60.0
CAVITY_Z0 = 10.0
CAVITY_Z1 = 50.0

REAL_CAPSULE_STEP = Path("/home/yushen/tempt/capsule.step")
REAL_CAVITY_STEP = Path("/home/yushen/tempt/cavity.step")


# ---------------------------------------------------------------------------
# 夹具:gmsh OCC 嵌套盒 → STEP
# ---------------------------------------------------------------------------

def _export_step(path: Path, builder) -> None:
    """独立 gmsh 会话内构建单实体并导出 STEP(会话即开即关,不与 mesh 锁互嵌)。"""
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add(path.stem)
        builder(gmsh)
        gmsh.model.occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()


def _build_capsule(gmsh) -> None:
    """包套 = 外盒 CUT 内腔盒 → 单空心壳实体。"""
    outer = gmsh.model.occ.addBox(-CAPSULE_R, -CAPSULE_R, 0.0, 2 * CAPSULE_R, 2 * CAPSULE_R, HEIGHT)
    void = gmsh.model.occ.addBox(
        -CAVITY_R, -CAVITY_R, CAVITY_Z0, 2 * CAVITY_R, 2 * CAVITY_R, CAVITY_Z1 - CAVITY_Z0
    )
    gmsh.model.occ.cut([(3, outer)], [(3, void)])


def _build_cavity(gmsh) -> None:
    """型腔(粉末芯)= 与包套内腔同尺寸的内芯盒。"""
    gmsh.model.occ.addBox(
        -CAVITY_R, -CAVITY_R, CAVITY_Z0, 2 * CAVITY_R, 2 * CAVITY_R, CAVITY_Z1 - CAVITY_Z0
    )


@pytest.fixture(scope="session")
def nested_steps(tmp_path_factory) -> tuple[Path, Path]:
    """会话级 STEP 夹具:(capsule.step, cavity.step)。"""
    out_dir = tmp_path_factory.mktemp("nested_step")
    capsule = out_dir / "capsule.step"
    cavity = out_dir / "cavity.step"
    _export_step(capsule, _build_capsule)
    _export_step(cavity, _build_cavity)
    return capsule, cavity


def _make_ctx(job_dir: Path) -> RunContext:
    """FEM 测试上下文(无真 MAPDL,仅 mesh 路径不触碰 ansys_bin)。"""
    return RunContext(
        job_dir=job_dir,
        ansys_bin="/nonexistent/ansys221",
        license_file="",
        ansys_np=2,
        job_timeout_s=60,
    )


def _cdb_element_lines(cdb_path: Path) -> list[str]:
    return [line for line in cdb_path.read_text().splitlines() if line.startswith("E,")]


def _cdb_node_lines(cdb_path: Path) -> list[str]:
    return [line for line in cdb_path.read_text().splitlines() if line.startswith("N,")]


# ---------------------------------------------------------------------------
# step_bbox / mesh_capsule_powder / exterior_faces_from_cdb
# ---------------------------------------------------------------------------

class TestStepBbox:
    def test_nested_capsule_bbox(self, nested_steps):
        capsule, _ = nested_steps
        bbox = step_bbox(capsule)
        # STEP 往返有 ~1e-7 量级的几何容差,放宽绝对误差
        assert bbox["min"] == pytest.approx([-CAPSULE_R, -CAPSULE_R, 0.0], abs=1e-5)
        assert bbox["max"] == pytest.approx([CAPSULE_R, CAPSULE_R, HEIGHT], abs=1e-5)

    def test_missing_step_raises_geometry_not_found(self, tmp_path):
        with pytest.raises(KernelError, match="GEOMETRY_NOT_FOUND"):
            step_bbox(tmp_path / "absent.step")


class TestMeshCapsulePowder:
    def test_full_two_domain_mesh(self, nested_steps, tmp_path):
        capsule, cavity = nested_steps
        result = mesh_capsule_powder(
            capsule_step=str(capsule),
            cavity_step=str(cavity),
            mesh_size_mm=6.0,
            out_formats=("cdb", "msh", "stl"),
            workdir=tmp_path,
        )
        assert result["node_count"] > 0
        assert result["element_count"] > 0
        assert result["groups"]["powder"] > 0
        assert result["groups"]["capsule"] > 0
        assert result["degraded"] is False
        assert sorted(Path(p).name for p in result["artifacts"]) == [
            "capsule_powder.cdb", "capsule_powder.msh", "capsule_powder.stl",
        ]
        for path in result["artifacts"]:
            assert Path(path).is_file()
        cdb = tmp_path / "capsule_powder.cdb"
        text = cdb.read_text()
        assert "ET,1,SOLID45" in text
        assert "MAT,1" in text and "MAT,2" in text
        assert len(_cdb_element_lines(cdb)) == result["element_count"]
        assert len(_cdb_node_lines(cdb)) == result["node_count"]

    def test_degraded_capsule_only(self, nested_steps, tmp_path):
        capsule, _ = nested_steps
        result = mesh_capsule_powder(
            capsule_step=str(capsule),
            cavity_step=None,
            mesh_size_mm=8.0,
            out_formats=("cdb",),
            workdir=tmp_path,
        )
        assert result["degraded"] is True
        assert result["groups"]["powder"] == 0
        assert result["groups"]["capsule"] > 0
        assert [Path(p).name for p in result["artifacts"]] == ["capsule_powder.cdb"]

    def test_missing_capsule_raises(self, tmp_path):
        with pytest.raises(KernelError, match="GEOMETRY_NOT_FOUND"):
            mesh_capsule_powder("/absent.step", None, 6.0, ("cdb",), tmp_path)

    def test_missing_cavity_raises(self, nested_steps, tmp_path):
        capsule, _ = nested_steps
        with pytest.raises(KernelError, match="GEOMETRY_NOT_FOUND"):
            mesh_capsule_powder(str(capsule), "/absent.step", 6.0, ("cdb",), tmp_path)

    @pytest.mark.parametrize("bad_size", [0.0, -3.0])
    def test_invalid_mesh_size(self, nested_steps, tmp_path, bad_size):
        capsule, cavity = nested_steps
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            mesh_capsule_powder(str(capsule), str(cavity), bad_size, ("cdb",), tmp_path)

    def test_unsupported_format(self, nested_steps, tmp_path):
        capsule, cavity = nested_steps
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            mesh_capsule_powder(str(capsule), str(cavity), 6.0, ("nastran",), tmp_path)


class TestExteriorFaces:
    def test_faces_within_element_range(self, nested_steps, tmp_path):
        capsule, cavity = nested_steps
        result = mesh_capsule_powder(
            str(capsule), str(cavity), 8.0, ("cdb",), tmp_path
        )
        cdb = tmp_path / "capsule_powder.cdb"
        faces = exterior_faces_from_cdb(cdb)
        assert len(faces) > 0
        assert all(1 <= elem <= result["element_count"] for elem, _ in faces)
        assert all(1 <= face <= 4 for _, face in faces)
        # 单元四面共 4 面,外表面必是全部面的严格子集
        total_faces = 4 * result["element_count"]
        assert len(faces) < total_faces

    def test_missing_cdb_raises_internal(self, tmp_path):
        with pytest.raises(KernelError, match="INTERNAL"):
            exterior_faces_from_cdb(tmp_path / "absent.cdb")


class TestRunMeshKernel:
    def test_run_mesh_returns_basenames_and_fidelity(self, nested_steps, tmp_path):
        capsule, cavity = nested_steps
        params = MeshMethodParams(
            geometry=GeometryRef(capsule_step=str(capsule), cavity_step=str(cavity)),
            output_formats=["cdb", "stl"],
        )
        result = run_mesh(params, _make_ctx(tmp_path))
        assert result["fidelity"] == "real"
        assert result["node_count"] > 0
        assert all("/" not in name for name in result["artifacts"])
        assert (tmp_path / "capsule_powder.cdb").is_file()
        assert (tmp_path / "capsule_powder.stl").is_file()

    def test_run_mesh_requires_capsule(self, tmp_path):
        params = MeshMethodParams(geometry=GeometryRef(capsule_step=None))
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            run_mesh(params, _make_ctx(tmp_path))


@pytest.mark.slow
@pytest.mark.skipif(
    not (REAL_CAPSULE_STEP.is_file() and REAL_CAVITY_STEP.is_file()),
    reason="~/tempt 真 STEP 夹具不存在",
)
class TestRealSteps:
    def test_mesh_real_capsule_cavity(self, tmp_path):
        result = mesh_capsule_powder(
            str(REAL_CAPSULE_STEP), str(REAL_CAVITY_STEP), 6.0, ("cdb", "stl"), tmp_path
        )
        assert result["node_count"] > 0
        assert result["element_count"] > 0
        assert result["groups"]["powder"] > 0
        assert result["groups"]["capsule"] > 0
        assert result["degraded"] is False


# ---------------------------------------------------------------------------
# 模板渲染关键词(上下文键与 fem.py 传入严格一致)
# ---------------------------------------------------------------------------

_TWO_ZONES = [
    {"r_from": 0.0, "r_to": 20.0, "mat": 1, "sel_lo": 4.0, "sel_hi": 16.0},
    {"r_from": 20.0, "r_to": 30.0, "mat": 2, "sel_lo": 22.0, "sel_hi": 28.0},
]


class TestTemplateRender:
    def test_axisym_thermal_keywords(self):
        material_blocks = ["\n".join(material_commands(load_material("tc4"), 1, "thermal"))]
        text = render(
            "template_axisym_thermal.inp",
            zones=_TWO_ZONES, z_from=0.0, z_to=60.0, initial_temp_c=20.0,
            material_blocks=material_blocks, mesh_size_mm=6.0, outer_r=30.0,
            probes=[{"index": 1, "r": 0.0, "z": 30.0}, {"index": 2, "r": 0.0, "z": 0.0}],
            segments=[
                {"t_end": 3600.0, "temp": 900.0, "nsub": 36},
                {"t_end": 14400.0, "temp": 900.0, "nsub": 108},
            ],
        )
        assert "ET,1,PLANE77" in text
        assert "KEYOPT,1,3,1" in text
        assert "ANTYPE,TRANS" in text
        assert "TUNIF,20" in text
        assert "SFL,ALL,TEMP,900" in text
        assert "*CFOPEN,series,csv,,,APPEND" in text
        assert "MPTEMP,1,20" in text
        assert "MPDATA,KXX,1,1," in text
        assert "MPDATA,C,1,1," in text
        assert "MPDATA,DENS,1,1,4.43e-09" in text  # 4430 kg/m3 → 4.43e-9 tonne/mm3
        assert "npr1 = NODE(0,30)" in text
        assert "*VWRITE,tcur,1,vpr1" in text
        assert "AGLUE,ALL" in text

    def test_axisym_hip_keywords(self):
        text = render(
            "template_axisym_hip.inp",
            zones=_TWO_ZONES, z_from=0.0, z_to=60.0,
            material_lines=["MP,EX,1,110000", "MP,PRXY,1,0.34"],
            mesh_size_mm=6.0, outer_r=30.0, nlgeom=True,
            segments=[
                {"t_end": 3600.0, "pressure": 120.0, "nsub": 36},
                {"t_end": 14400.0, "pressure": 120.0, "nsub": 108},
            ],
        )
        assert "ET,1,PLANE183" in text
        assert "NLGEOM,1" in text
        assert "SFL,ALL,PRES,120" in text
        assert "NSUBST,36" in text
        assert "ESORT,ETAB,vmse" in text
        assert "MP,EX,1,110000" in text
        assert "*CFOPEN,summary,csv" in text

    def test_axisym_mechanical_keywords(self):
        text = render(
            "template_axisym_mechanical.inp",
            zones=_TWO_ZONES, z_from=0.0, z_to=60.0, z_mid=30.0,
            material_lines=["MP,EX,1,110000"],
            mesh_size_mm=6.0, outer_r=30.0, hold_pressure_mpa=120.0,
            section_samples=[{"radius": 0.0}, {"radius": 15.0}, {"radius": 30.0}],
        )
        assert "ET,1,PLANE183" in text
        assert "SFL,ALL,PRES,120" in text
        assert "nsec1 = NODE(0,30)" in text
        assert "*GET,ssec1,NODE,nsec1,S,X" in text
        assert "*VWRITE,rcur1,1,ssec1" in text

    def test_3d_keywords(self):
        text = render(
            "template_3d.inp",
            cdb_stem="capsule_powder",
            material_lines=["MP,EX,1,110000", "MP,EX,2,120000"],
            nlgeom=False,
            anchor_1=(-30.0, -30.0, 0.0),
            anchor_2=(30.0, -30.0, 0.0),
            anchor_3=(-30.0, 30.0, 0.0),
            pressure_faces=[(1, 4), (9, 1)],
            hold_pressure_mpa=120.0,
        )
        assert "/INPUT,capsule_powder,cdb" in text
        assert "SFE,1,4,PRES,,120" in text
        assert "SFE,9,1,PRES,,120" in text
        assert "D,n1,UX,0" in text
        assert "D,n2,UY,0 $ D,n2,UZ,0" in text
        assert "NLGEOM,0" in text
        assert "ETABLE,vmse,S,EQV" in text

    def test_missing_context_key_raises(self):
        from jinja2 import UndefinedError

        with pytest.raises(UndefinedError):
            render("template_3d.inp", cdb_stem="x")  # 其余键缺失

    def test_write_input_creates_file(self, tmp_path):
        out = tmp_path / "rendered.inp"
        write_input(
            "template_3d.inp", out,
            cdb_stem="capsule_powder", material_lines=[], nlgeom=False,
            anchor_1=(0, 0, 0), anchor_2=(1, 0, 0), anchor_3=(0, 1, 0),
            pressure_faces=[], hold_pressure_mpa=0.0,
        )
        assert out.is_file()
        assert "/INPUT,capsule_powder,cdb" in out.read_text()


# ---------------------------------------------------------------------------
# results 解析
# ---------------------------------------------------------------------------

class TestResultsParsing:
    def test_summary_parses_floats_and_strings(self, tmp_path):
        (tmp_path / "summary.csv").write_text(
            "key,value\ndisplacement_max_mm,0.123\nnote,hello\nbadline\n", encoding="utf-8"
        )
        summary = parse_summary_csv(tmp_path)
        assert summary["displacement_max_mm"] == pytest.approx(0.123)
        assert summary["note"] == "hello"
        assert "badline" not in summary

    def test_summary_missing_returns_empty(self, tmp_path):
        assert parse_summary_csv(tmp_path) == {}

    def test_series_sorted_and_probe_kept_as_string(self, tmp_path):
        (tmp_path / "series.csv").write_text(
            " 3.6E+03, 1, 5.1E+02\n 1.8E+03, 2, 3.0E+02\n 3.6E+03, 0, 0.15\nnot,a,row\n",
            encoding="utf-8",
        )
        rows = parse_series_csv(tmp_path)
        assert [row["probe"] for row in rows] == ["2", "0", "1"]  # 1800s 行先于 3600s
        assert rows[0]["time_s"] == pytest.approx(1800.0)
        assert rows[0]["value"] == pytest.approx(300.0)
        assert rows[1]["probe"] == "0"
        assert rows[1]["value"] == pytest.approx(0.15)

    def test_series_missing_returns_empty(self, tmp_path):
        assert parse_series_csv(tmp_path) == []

    def test_extract_error_lines(self):
        out = "\n".join(
            ["   *** ERROR ***  CP = 1.2", "normal line", "*** FATAL *** terminated", " error lowercase"]
        )
        lines = extract_error_lines(out)
        assert len(lines) == 3
        assert lines[0].startswith("*** ERROR ***")

    def test_extract_error_lines_capped(self):
        out = "\n".join(f"*** ERROR *** {i}" for i in range(60))
        assert len(extract_error_lines(out)) == 40


# ---------------------------------------------------------------------------
# material_apdl 单位换算与插值
# ---------------------------------------------------------------------------

_SYNTHETIC = {
    "display_name": "合成材料",
    "density_kg_m3": 4430,
    "properties": {
        "young_modulus_gpa": [
            {"temperature_c": 20.0, "value": 100.0},
            {"temperature_c": 1000.0, "value": 50.0},
        ],
        "poisson_ratio": [{"temperature_c": 20.0, "value": 0.3}],
        "thermal_conductivity_w_m_k": [
            {"temperature_c": 20.0, "value": 7.0},
            {"temperature_c": 900.0, "value": 12.0},
        ],
        "specific_heat_j_kgk": [
            {"temperature_c": 20.0, "value": 550.0},
            {"temperature_c": 900.0, "value": 700.0},
        ],
    },
}


class TestMaterialApdl:
    def test_thermal_commands_units(self):
        lines = material_commands(_SYNTHETIC, 1, "thermal")
        assert lines[0].startswith("! ----")
        assert "MPDATA,DENS,1,1,4.43e-09" in lines
        assert "MPTEMP,1,20,900" in lines
        assert "MPDATA,KXX,1,1,7,12" in lines          # KXX ×1
        assert "MPDATA,C,1,1,550000000,700000000" in lines  # C ×1e6

    def test_structural_commands_units(self):
        lines = material_commands(_SYNTHETIC, 2, "structural")
        assert "MPDATA,EX,2,1,100000,50000" in lines   # GPa ×1e3 → MPa
        assert "MPDATA,PRXY,2,1,0.3" in lines

    def test_unknown_kind_raises(self):
        with pytest.raises(KernelError, match="INTERNAL"):
            material_commands(_SYNTHETIC, 1, "gurson")

    def test_elastic_at_temperature_interpolates(self):
        at_low = elastic_at_temperature(_SYNTHETIC, 20.0)
        assert at_low["ex_mpa"] == pytest.approx(100000.0)
        assert at_low["prxy"] == pytest.approx(0.3)
        mid = elastic_at_temperature(_SYNTHETIC, 560.0)
        expected = (100.0 + (560.0 - 20.0) / 980.0 * (50.0 - 100.0)) * 1e3
        assert mid["ex_mpa"] == pytest.approx(expected)
        assert elastic_at_temperature(_SYNTHETIC, 2000.0)["ex_mpa"] == pytest.approx(50000.0)

    def test_real_materials_render(self):
        lines = material_commands(load_material("20steel"), 2, "thermal")
        assert any(line.startswith("MPDATA,KXX,2,1,") for line in lines)
        assert any(line.startswith("MPDATA,DENS,2,1,") for line in lines)


# ---------------------------------------------------------------------------
# fem 纯助手(无 MAPDL)
# ---------------------------------------------------------------------------

class TestFemHelpers:
    def test_domain_from_profile(self):
        params = AxisymHipParams(
            profile=AxisymProfile(inner_radius_mm=20.0, outer_radius_mm=30.0, height_mm=60.0)
        )
        domain = _axisym_domain(params)
        assert domain == {"inner": 20.0, "outer": 30.0, "height": 60.0, "r_split": None}

    def test_domain_from_geometry_bbox(self, nested_steps):
        capsule, cavity = nested_steps
        params = AxisymHipParams(
            geometry=GeometryRef(capsule_step=str(capsule), cavity_step=str(cavity))
        )
        domain = _axisym_domain(params)
        assert domain["outer"] == pytest.approx(CAPSULE_R)
        assert domain["height"] == pytest.approx(HEIGHT)
        assert domain["r_split"] == pytest.approx(CAVITY_R)

    def test_domain_requires_geometry_or_profile(self):
        with pytest.raises(KernelError, match="INVALID_PARAMS"):
            _axisym_domain(AxisymHipParams())

    def test_zones_single_and_double(self):
        single = _axisym_zones({"inner": 0.0, "outer": 30.0, "height": 60.0, "r_split": None})
        assert [zone["mat"] for zone in single] == [1]
        double = _axisym_zones({"inner": 0.0, "outer": 30.0, "height": 60.0, "r_split": 20.0})
        assert [zone["mat"] for zone in double] == [1, 2]
        assert double[0]["r_to"] == pytest.approx(20.0)
        assert double[1]["r_from"] == pytest.approx(20.0)
        # 质心选取带须落在各自径向带内
        for zone in double:
            assert zone["r_from"] <= zone["sel_lo"] < zone["sel_hi"] <= zone["r_to"]

    def test_segments_and_substep_cap(self):
        cycle = Cycle(points=[
            CyclePoint(time_s=0.0, temperature_c=20.0, pressure_mpa=0.0),
            CyclePoint(time_s=3600.0, temperature_c=900.0, pressure_mpa=120.0),
            CyclePoint(time_s=14400.0, temperature_c=900.0, pressure_mpa=120.0),
        ])
        segments = _segments(cycle, 100.0)
        assert [s["t_end"] for s in segments] == [3600.0, 14400.0]
        assert [s["nsub"] for s in segments] == [36, 108]
        capped = _segments(cycle, 1.0)
        assert all(s["nsub"] <= 200 for s in capped)
        assert _nsubsteps(0.0, 100.0) == 1

    def test_probe_names(self):
        assert _probe_names(2) == ["core", "surface"]
        assert _probe_names(3) == ["probe_1", "probe_2", "probe_3"]

    def test_max_probe_lag(self):
        rows = [
            {"time_s": 1.0, "probe": "1", "value": 100.0},
            {"time_s": 1.0, "probe": "2", "value": 90.0},
            {"time_s": 2.0, "probe": "1", "value": 300.0},
            {"time_s": 2.0, "probe": "2", "value": 100.0},
        ]
        assert _max_probe_lag(rows) == pytest.approx(200.0)
        assert _max_probe_lag([]) == 0.0

    def test_elastic_lines_use_present_mats_only(self):
        powder = load_material("tc4")
        capsule = load_material("20steel")
        single = _elastic_lines(
            _axisym_zones({"inner": 0.0, "outer": 30.0, "height": 60.0, "r_split": None}),
            powder, capsule, 900.0,
        )
        assert any(line.startswith("MP,EX,1,") for line in single)
        assert not any(line.startswith("MP,EX,2,") for line in single)
        double = _elastic_lines(
            _axisym_zones({"inner": 0.0, "outer": 30.0, "height": 60.0, "r_split": 20.0}),
            powder, capsule, 900.0,
        )
        assert any(line.startswith("MP,EX,1,") for line in double)
        assert any(line.startswith("MP,PRXY,2,") for line in double)

    def test_default_substeps_are_sane(self):
        # 3h 缺省曲线 / 120 步 → 每段子步远低于许可保护上限
        durations = (3600.0, 10800.0, 3600.0)
        dt = 18000.0 / 120.0
        assert all(1 <= _nsubsteps(d, dt) <= 200 for d in durations)
        assert math.ceil(3600.0 / dt) == 24
