"""gmsh 网格管线 — STEP 几何 → 共形两材料四面体网格 → .cdb/.msh/.stl。

移植 HIPForm cad_workflow 语义(独立实现,不 import 上游代码):
    - 每个 STEP 导入后必须恰含 1 个实体(highestDimOnly);
    - 包套/粉末(cavity)体积交 ≤ 2e-7(相对)、间隙距离 ≤ 1e-7 mm;
    - occ.fragment → 两域共享界面的共形网格;physical groups: powder=1 / capsule=2;
    - cavity 参数未提供 → 降级单域(仅包套)网格,结果带 degraded=True;
    - capsule STEP 缺失 → KernelError GEOMETRY_NOT_FOUND。

.cdb 为 MAPDL 命令流(ET/N/MAT/E,可 /INPUT 读入):meshio 的 "ansys" 格式
实为 Fluent .msh 分节、MAPDL 无法读取,故自写命令流(详见汇报)。
MAT 编号与 physical group 对齐:1=powder、2=capsule,求解模板按此定义材料属性。
gmsh 全局 API 非线程安全:所有调用经模块锁串行。
"""

from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import gmsh

from .registry import KernelError

logger = logging.getLogger(__name__)

# HIPForm cad_workflow 的几何配对容差(原样移植)
OVERLAP_REL_TOL = 2e-7          # 体积交 / 粉末体积 的相对上限
GAP_ABS_TOL_MM = 1e-7           # 两域间距绝对上限(必须贴合)

# physical group 与 .cdb MAT 编号(powder 在前,与求解模板的材料定义对齐)
GROUP_POWDER_TAG = 1
GROUP_CAPSULE_TAG = 2

# 网格:一阶四面体(gmsh 元素类型 4 = 4 节点四面体;MAPDL 侧 SOLID45)
TET_ELEMENT_TYPE = 4
MESH_STEM = "capsule_powder"
SUPPORTED_FORMATS = ("cdb", "msh", "stl")
DEFAULT_FORMATS = ("cdb",)

_GMSH_LOCK = threading.Lock()


@contextmanager
def _gmsh_session() -> Iterator[None]:
    """持有模块锁并完成 gmsh init/finalize(gmsh 全局 API 必须串行)。"""
    with _GMSH_LOCK:
        gmsh.initialize()
        gmsh.option.setNumber("General.Terminal", 0)
        try:
            yield
        finally:
            gmsh.finalize()


@dataclass(frozen=True)
class MeshData:
    """网格提取结果:节点坐标表 + 分组四面体(节点 tag 四元组)。"""

    nodes: dict[int, tuple[float, float, float]]
    tets_by_group: dict[str, tuple[tuple[int, int, int, int], ...]]


def step_bbox(step_path: Path | str) -> dict[str, list[float]]:
    """读单个 STEP 的整体包围盒(fem 由几何自动派生轴对称剖面用)。

    返回 {"min": [x,y,z], "max": [x,y,z]}(mm);文件缺失 → GEOMETRY_NOT_FOUND。
    """
    path = Path(step_path)
    if not path.is_file():
        raise KernelError("GEOMETRY_NOT_FOUND", f"几何文件不存在: {path}")
    with _gmsh_session():
        try:
            _import_single_solid(path, "几何")
            gmsh.model.occ.synchronize()
            raw = gmsh.model.getBoundingBox(-1, -1)
        except KernelError:
            raise
        except Exception as exc:  # gmsh API 失败
            raise KernelError("INTERNAL", f"读取 STEP 包围盒失败({path}): {exc}") from exc
    return {"min": [float(v) for v in raw[:3]], "max": [float(v) for v in raw[3:]]}


def mesh_capsule_powder(
    capsule_step: Path | str,
    cavity_step: Path | str | None,
    mesh_size_mm: float,
    out_formats: Sequence[str] | None,
    workdir: Path | str,
) -> dict:
    """包套(+粉末型腔)STEP → 共形四面体网格并按要求写出工件。

    成功返回 {node_count, element_count, groups{powder,capsule},
    artifacts[], bbox{min,max}, degraded};几何/参数非法抛 KernelError。
    """
    capsule_path = Path(capsule_step)
    if not capsule_path.is_file():
        raise KernelError("GEOMETRY_NOT_FOUND", f"包套 STEP 不存在: {capsule_path}")
    degraded = cavity_step is None
    cavity_path = None if degraded else Path(cavity_step)
    if cavity_path is not None and not cavity_path.is_file():
        raise KernelError("GEOMETRY_NOT_FOUND", f"粉末型腔 STEP 不存在: {cavity_path}")
    if mesh_size_mm <= 0:
        raise KernelError("INVALID_PARAMS", f"网格尺寸必须为正数(mm),收到 {mesh_size_mm}")
    formats = _normalize_formats(out_formats)
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)
    if degraded:
        logger.warning("未提供粉末型腔 STEP,降级为仅包套单域网格: %s", capsule_path)

    with _gmsh_session():
        try:
            cap_tag = _import_single_solid(capsule_path, "包套")
            if degraded:
                capsule_vols, powder_vols = [cap_tag], []
            else:
                cav_tag = _import_single_solid(cavity_path, "粉末型腔")  # type: ignore[arg-type]
                _validate_pairing(cap_tag, cav_tag, cavity_path)  # type: ignore[arg-type]
                capsule_vols, powder_vols = _fragment_domains(cap_tag, cav_tag)
            gmsh.model.occ.synchronize()
            _add_physical_groups(capsule_vols, powder_vols)
            _configure_meshing(mesh_size_mm)
            gmsh.model.mesh.generate(3)
            msh_path = _write_msh(formats, workdir_path)
            mesh_data = _extract_mesh(capsule_vols, powder_vols)
            bbox = _model_bbox()
        except KernelError:
            raise
        except Exception as exc:  # gmsh 布尔/网格化等 API 失败
            raise KernelError("INTERNAL", f"gmsh 网格管线失败({capsule_path}): {exc}") from exc

    artifacts = [str(p) for p in (msh_path,) if p is not None]
    artifacts += _write_cdb(formats, workdir_path, mesh_data)
    artifacts += _write_stl(formats, workdir_path, mesh_data)
    return {
        "node_count": len(mesh_data.nodes),
        "element_count": sum(len(t) for t in mesh_data.tets_by_group.values()),
        "groups": {
            "powder": len(mesh_data.tets_by_group["powder"]),
            "capsule": len(mesh_data.tets_by_group["capsule"]),
        },
        "artifacts": artifacts,
        "bbox": bbox,
        "degraded": degraded,
    }


# ---------------------------------------------------------------------------
# 几何导入与配对校验(HIPForm cad_workflow 语义)
# ---------------------------------------------------------------------------

def _import_single_solid(step_path: Path, label: str) -> int:
    """导入 STEP 并要求恰含 1 个实体;返回其实体 tag。"""
    raw = gmsh.model.occ.importShapes(fileName=str(step_path), highestDimOnly=True)
    tags = tuple(ref if isinstance(ref, int) else int(ref[1]) for ref in raw)
    if len(tags) != 1:
        raise KernelError(
            "INVALID_PARAMS", f"{label} STEP 应恰含 1 个实体,实际 {len(tags)} 个: {step_path}"
        )
    return tags[0]


def _validate_pairing(cap_tag: int, cav_tag: int, cavity_path: Path) -> None:
    """包套-粉末配对校验:体积交与间隙都须在容差内(空腔壳体-内芯拓扑)。"""
    cap_copy = gmsh.model.occ.copy([(3, cap_tag)])
    cav_copy = gmsh.model.occ.copy([(3, cav_tag)])
    overlap, _ = gmsh.model.occ.intersect(cap_copy, cav_copy, removeObject=True, removeTool=True)
    overlap_vol = sum(gmsh.model.occ.getMass(3, t) for d, t in overlap if d == 3)
    cavity_vol = gmsh.model.occ.getMass(3, cav_tag)
    if cavity_vol > 0 and overlap_vol / cavity_vol > OVERLAP_REL_TOL:
        ratio = overlap_vol / cavity_vol
        raise KernelError(
            "INVALID_PARAMS",
            f"包套与粉末体积重叠(相对 {ratio:.2e} > 容差 {OVERLAP_REL_TOL:g}),"
            f"应为空腔壳体+内芯两实体: {cavity_path}",
        )
    # gmsh occ.getDistance 返回 (dist, x1,y1,z1, x2,y2,z2),仅取距离
    distance = gmsh.model.occ.getDistance(3, cap_tag, 3, cav_tag)[0]
    if distance > GAP_ABS_TOL_MM:
        raise KernelError(
            "INVALID_PARAMS",
            f"包套与粉末间隙 {distance:.3e} mm 超容差(粉末未贴合包套内壁): {cavity_path}",
        )


def _fragment_domains(cap_tag: int, cav_tag: int) -> tuple[list[int], list[int]]:
    """occ.fragment → 共形体划分;返回(包套体列表, 粉末体列表)。"""
    _, mapping = gmsh.model.occ.fragment([(3, cap_tag)], [(3, cav_tag)])
    cap_vols = {t for d, t in mapping[0] if d == 3}
    cav_vols = {t for d, t in mapping[1] if d == 3}
    shared = cap_vols & cav_vols
    if shared:
        raise KernelError(
            "INVALID_PARAMS",
            f"布尔 fragment 后包套/粉末仍共享体 {sorted(shared)}(几何体积重叠,非空腔-内芯拓扑)",
        )
    return sorted(cap_vols), sorted(cav_vols)


def _add_physical_groups(capsule_vols: Sequence[int], powder_vols: Sequence[int]) -> None:
    """写 physical groups(命名导出用):powder=1、capsule=2。"""
    if powder_vols:
        gmsh.model.addPhysicalGroup(3, list(powder_vols), tag=GROUP_POWDER_TAG, name="powder")
    gmsh.model.addPhysicalGroup(3, list(capsule_vols), tag=GROUP_CAPSULE_TAG, name="capsule")


def _configure_meshing(mesh_size_mm: float) -> None:
    """一阶四面体、均匀网格尺寸、Algorithm3D=1(HIPForm 同款设置)。"""
    for option, value in (
        ("Mesh.MeshSizeMin", mesh_size_mm),
        ("Mesh.MeshSizeMax", mesh_size_mm),
        ("Mesh.MeshSizeFromCurvature", 0),
        ("Mesh.MeshSizeExtendFromBoundary", 0),
        ("Mesh.ElementOrder", 1),
        ("Mesh.Algorithm3D", 1),
    ):
        gmsh.option.setNumber(option, value)


# ---------------------------------------------------------------------------
# 网格提取与工件写出
# ---------------------------------------------------------------------------

def _extract_mesh(capsule_vols: Sequence[int], powder_vols: Sequence[int]) -> MeshData:
    """从 gmsh 取节点坐标与分组四面体(节点 tag 四元组)。"""
    node_tags, coords, _ = gmsh.model.mesh.getNodes()
    nodes = {
        int(tag): (float(coords[3 * i]), float(coords[3 * i + 1]), float(coords[3 * i + 2]))
        for i, tag in enumerate(node_tags)
    }
    return MeshData(nodes=nodes, tets_by_group={
        "powder": _volume_tets(powder_vols),
        "capsule": _volume_tets(capsule_vols),
    })


def _volume_tets(vol_tags: Sequence[int]) -> tuple[tuple[int, int, int, int], ...]:
    """按实体收集全部一阶四面体;出现其他类型即 INTERNAL(网格阶次设置失效)。"""
    tets: list[tuple[int, int, int, int]] = []
    for vol in vol_tags:
        types, _, node_blocks = gmsh.model.mesh.getElements(3, vol)
        for element_type, block in zip(types, node_blocks):
            if element_type != TET_ELEMENT_TYPE:
                raise KernelError(
                    "INTERNAL",
                    f"预期 4 节点四面体(gmsh 类型 {TET_ELEMENT_TYPE}),收到类型 {element_type}",
                )
            tets.extend(
                (int(block[4 * i]), int(block[4 * i + 1]), int(block[4 * i + 2]), int(block[4 * i + 3]))
                for i in range(len(block) // 4)
            )
    return tuple(tets)


def _model_bbox() -> dict[str, list[float]]:
    """整模型包围盒(mm)。"""
    raw = gmsh.model.getBoundingBox(-1, -1)
    return {"min": [float(v) for v in raw[:3]], "max": [float(v) for v in raw[3:]]}


def _normalize_formats(out_formats: Sequence[str] | None) -> tuple[str, ...]:
    """输出格式白名单校验;未提供时默认 ("cdb",)。"""
    formats = tuple(out_formats) if out_formats else DEFAULT_FORMATS
    unknown = [fmt for fmt in formats if fmt not in SUPPORTED_FORMATS]
    if unknown:
        raise KernelError(
            "INVALID_PARAMS", f"不支持的网格输出格式 {unknown};可选 {list(SUPPORTED_FORMATS)}"
        )
    return formats


def _write_msh(formats: tuple[str, ...], workdir: Path) -> Path | None:
    """.msh 工件(gmsh 原生写出,含 physical groups)。"""
    if "msh" not in formats:
        return None
    path = workdir / f"{MESH_STEM}.msh"
    gmsh.write(str(path))
    return path


def _write_cdb(formats: tuple[str, ...], workdir: Path, data: MeshData) -> list[str]:
    """.cdb 工件 — MAPDL 命令流(ET/N/MAT,E),/INPUT 可读。"""
    if "cdb" not in formats:
        return []
    lines = [
        "/PREP7",
        "! 命令流网格(capsule/powder 分 MAT;1=powder 2=capsule,属性由求解模板定义)",
        "ET,1,SOLID45",
    ]
    node_ids = {tag: index for index, tag in enumerate(sorted(data.nodes), start=1)}
    for tag in sorted(data.nodes):
        x, y, z = data.nodes[tag]
        lines.append(f"N,{node_ids[tag]},{x:.9g},{y:.9g},{z:.9g}")
    for group, mat_id in (("powder", GROUP_POWDER_TAG), ("capsule", GROUP_CAPSULE_TAG)):
        tets = data.tets_by_group.get(group, ())
        if not tets:
            continue
        lines.append(f"MAT,{mat_id}")
        lines.extend(
            _cdb_element_line(tet, node_ids, data.nodes) for tet in tets
        )
    path = workdir / f"{MESH_STEM}.cdb"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(path)]


def _cdb_element_line(
    tet: tuple[int, int, int, int],
    node_ids: dict[int, int],
    nodes: dict[int, tuple[float, float, float]],
) -> str:
    """单条 E 命令(节点编号替换;保证正体积定向,交换末两节点修正负 Jacobian)。"""
    a, b, c, d = (nodes[tag] for tag in tet)
    u = tuple(b[i] - a[i] for i in range(3))
    v = tuple(c[i] - a[i] for i in range(3))
    w = tuple(d[i] - a[i] for i in range(3))
    det = (u[0] * (v[1] * w[2] - v[2] * w[1])
           - u[1] * (v[0] * w[2] - v[2] * w[0])
           + u[2] * (v[0] * w[1] - v[1] * w[0]))
    n1, n2, n3, n4 = tet if det >= 0 else (tet[0], tet[1], tet[3], tet[2])
    return f"E,{node_ids[n1]},{node_ids[n2]},{node_ids[n3]},{node_ids[n4]}"


def _tet_faces(tet: tuple[int, int, int, int]) -> tuple[tuple[int, int, int], ...]:
    """四面体的 4 个三角面(节点 tag 三元组,未排序)。"""
    n1, n2, n3, n4 = tet
    return ((n1, n2, n3), (n1, n2, n4), (n1, n3, n4), (n2, n3, n4))


def _face_occurrences(
    tets: Sequence[tuple[int, int, int, int]],
) -> dict[tuple[int, int, int], tuple[int, int]]:
    """面出现计数:sorted 节点三元组 → (出现次数, 首见四面体序号)。

    外边界面 = 恰出现一次(内部界面出现两次)。
    """
    occurrences: dict[tuple[int, int, int], tuple[int, int]] = {}
    for tet_index, tet in enumerate(tets):
        for triple in _tet_faces(tet):
            key = tuple(sorted(triple))
            count, first_tet = occurrences.get(key, (0, tet_index))
            occurrences[key] = (count + 1, first_tet)
    return occurrences


def _write_stl(formats: tuple[str, ...], workdir: Path, data: MeshData) -> list[str]:
    """.stl 工件 — 外边界面(恰出现一次的面)三角片,法向朝外。"""
    if "stl" not in formats:
        return []
    all_tets = data.tets_by_group["powder"] + data.tets_by_group["capsule"]
    occurrences = _face_occurrences(all_tets)
    lines = [f"solid {MESH_STEM}"]
    for key, (count, first_tet) in occurrences.items():
        if count != 1:
            continue
        a, b, c = (data.nodes[node] for node in key)
        tet_center = _tet_centroid(all_tets[first_tet], data.nodes)
        lines.extend(_stl_facet_lines(a, b, c, tet_center))
    lines.append(f"endsolid {MESH_STEM}")
    path = workdir / f"{MESH_STEM}.stl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(path)]


def exterior_faces_from_cdb(cdb_path: Path | str) -> list[tuple[int, int]]:
    """解析自产 .cdb,返回外边界面列表 [(单元号, 载荷面号 1-4), ...]。

    供 3D 模板直接 SFE 加均压外压(绕开 ESURF/SURF154):
    单元号 = E 命令出现顺序(1 起);载荷面号 = E 序中缺失节点所在位置
    (四面体面 i 与节点 i 相对,MAPDL 实体单元面编号约定)。
    """
    path = Path(cdb_path)
    if not path.is_file():
        raise KernelError("INTERNAL", f".cdb 网格工件不存在: {path}")
    tets: list[tuple[int, int, int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.strip().split(",")
        if len(fields) == 5 and fields[0].upper() == "E":
            tets.append(tuple(int(v) for v in fields[1:]))  # type: ignore[misc]
    occurrences = _face_occurrences(tets)
    faces: list[tuple[int, int]] = []
    for key, (count, first_tet) in occurrences.items():
        if count != 1:
            continue
        missing_position = next(
            index for index, node in enumerate(tets[first_tet]) if node not in key
        )
        faces.append((first_tet + 1, missing_position + 1))
    return sorted(faces)


def _tet_centroid(tet: tuple[int, int, int, int], nodes: dict[int, tuple[float, float, float]]) -> tuple[float, float, float]:
    """四面体质心(用于 STL 面法向朝外判定)。"""
    xs, ys, zs = zip(*(nodes[tag] for tag in tet))
    return (sum(xs) / 4.0, sum(ys) / 4.0, sum(zs) / 4.0)


def _stl_facet_lines(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    tet_center: tuple[float, float, float],
) -> list[str]:
    """单个 STL 三角片(法向由叉积给出,必要时交换 b/c 保证朝外)。"""
    u = tuple(b[i] - a[i] for i in range(3))
    v = tuple(c[i] - a[i] for i in range(3))
    normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(sum(component * component for component in normal))
    unit = tuple(component / length for component in normal) if length > 0 else (0.0, 0.0, 0.0)
    face_center = tuple((a[i] + b[i] + c[i]) / 3.0 for i in range(3))
    outward = sum(unit[i] * (face_center[i] - tet_center[i]) for i in range(3))
    p1, p2, p3 = (a, c, b) if outward < 0 else (a, b, c)
    return [
        f"  facet normal {unit[0]:.9g} {unit[1]:.9g} {unit[2]:.9g}",
        "    outer loop",
        f"      vertex {p1[0]:.9g} {p1[1]:.9g} {p1[2]:.9g}",
        f"      vertex {p2[0]:.9g} {p2[1]:.9g} {p2[2]:.9g}",
        f"      vertex {p3[0]:.9g} {p3[1]:.9g} {p3[2]:.9g}",
        "    endloop",
        "  endfacet",
    ]
