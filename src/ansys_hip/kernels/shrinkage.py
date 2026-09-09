"""收缩/补偿内核 — shrinkage-estimate / compensate。

体积守恒均匀收缩:相对密度 D0 → Df 时,线尺寸比
    L_final / L_initial = (D0 / Df)^(1/3)

- 收缩估算(shrinkage-estimate):已知包套初始尺寸 → 终态尺寸与收缩量;
- 预变形补偿(compensate):目标型腔(终态)STEP → 初始(放大)STEP,
  scale = (Df / D0)^(1/3),用 gmsh OCC dilate 实现全维度缩放。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import gmsh

from ..registry import KernelError
from ..schemas import CompensateParams, RunContext, ShrinkageEstimateParams

COMPENSATED_STEP_FILENAME: str = "compensated.step"

SHRINKAGE_FORMULA: str = "L_final = L_initial · (D0/Df)^(1/3)(体积守恒,各向同性均匀收缩)"


def linear_shrink_ratio(initial_density: float, final_density: float) -> float:
    """体积守恒线尺寸比 L_final / L_initial = (D0/Df)^(1/3)。"""
    return (initial_density / final_density) ** (1.0 / 3.0)


def run_shrinkage_estimate(params: ShrinkageEstimateParams, ctx: RunContext) -> dict:
    """均匀收缩估算:各命名特征尺寸(包套初始尺寸)的终态尺寸与收缩量。"""
    ratio = linear_shrink_ratio(params.initial_relative_density, params.final_relative_density)
    items = [
        {
            "name": name,
            "length_mm": float(length),
            "final_mm": float(length) * ratio,
            "shrink_mm": float(length) * (1.0 - ratio),
            "shrink_ratio": 1.0 - ratio,
        }
        for name, length in params.characteristic_lengths_mm.items()
    ]
    return {
        "initial_relative_density": params.initial_relative_density,
        "final_relative_density": params.final_relative_density,
        "items": items,
        "linear_strain": ratio - 1.0,
        "linear_shrink_ratio": ratio,
        "formula": SHRINKAGE_FORMULA,
        "fidelity": "real",
    }


# ---------------------------------------------------------------------------
# gmsh OCC 缩放(compensate)
# ---------------------------------------------------------------------------

@contextmanager
def _gmsh_session() -> Iterator[object]:
    """独占 gmsh 会话:进入前清理残留实例,退出必 finalize(防与其他内核串扰)。"""
    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        yield gmsh
    finally:
        gmsh.finalize()


def _bounding_box(gmsh_module: object) -> dict:
    """整模型包围盒(先 occ.synchronize 后调用)。"""
    xmin, ymin, zmin, xmax, ymax, zmax = gmsh_module.model.getBoundingBox(-1, -1)
    return {
        "min_mm": [xmin, ymin, zmin],
        "max_mm": [xmax, ymax, zmax],
        "size_mm": [xmax - xmin, ymax - ymin, zmax - zmin],
    }


def _scale_step_file(source: Path, target: Path, scale: float) -> tuple[dict, dict]:
    """gmsh OCC 读入 STEP → 全维度实体 dilate(scale) → 写出;返回(前, 后)包围盒。"""
    with _gmsh_session() as gmsh_module:
        try:
            gmsh_module.open(str(source))
        except Exception as exc:  # gmsh 对坏文件抛通用异常
            raise KernelError(
                "GEOMETRY_NOT_FOUND", f"STEP 文件无法读入: {source}({exc})",
            ) from exc
        occ = gmsh_module.model.occ
        occ.synchronize()
        dim_tags = occ.getEntities()  # 全维度 (dim, tag) 列表
        if not dim_tags:
            raise KernelError("GEOMETRY_NOT_FOUND", f"STEP 文件不含任何实体: {source}")
        before = _bounding_box(gmsh_module)
        occ.dilate(dim_tags, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        after = _bounding_box(gmsh_module)
        gmsh_module.write(str(target))
    return before, after


def run_compensate(params: CompensateParams, ctx: RunContext) -> dict:
    """预变形补偿:目标型腔 STEP 按 (Df/D0)^(1/3) 放大,输出补偿后 STEP。

    阶段 1 scale_axis=per_axis 与 uniform 相同(各向同性);缩放中心为全局原点。
    """
    cavity_step = params.geometry.cavity_step
    if cavity_step is None:
        raise KernelError("INVALID_PARAMS", "compensate 需要 geometry.cavity_step(目标型腔 STEP 路径)")
    source = Path(cavity_step)
    if not source.is_file():
        raise KernelError("GEOMETRY_NOT_FOUND", f"型腔 STEP 文件不存在: {cavity_step}")

    scale = (params.final_relative_density / params.initial_relative_density) ** (1.0 / 3.0)
    target = ctx.job_dir / COMPENSATED_STEP_FILENAME
    before, after = _scale_step_file(source, target, scale)

    return {
        "initial_relative_density": params.initial_relative_density,
        "final_relative_density": params.final_relative_density,
        "scale_factors": {"x": scale, "y": scale, "z": scale},
        "scale_axis": params.scale_axis,
        "scale_axis_note": (
            "阶段 1 per_axis 与 uniform 相同(各向同性均匀缩放),缩放中心为全局原点"
        ),
        "bounding_box": {"before": before, "after": after},
        "output_step_path": str(target),
        "artifacts": [COMPENSATED_STEP_FILENAME],
        "fidelity": "real",
    }
