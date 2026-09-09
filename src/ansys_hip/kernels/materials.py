"""材料库内核 — material-query:读 data/materials.yaml 并按温度插值。

YAML schema(冻结,T4 的 APDL 材料库按同一结构读取):
    materials.<name>.display_name / density_kg_m3 / source_notes
    materials.<name>.properties.<prop>: [{temperature_c, value}] 点列

插值策略:温度点间线性插值;超出数据温度范围取端点值(不外推)。
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

from ..registry import KernelError
from ..schemas import MaterialQueryParams, RunContext

MATERIALS_YAML_PATH: Path = Path(__file__).resolve().parents[1] / "data" / "materials.yaml"

INTERPOLATION_NOTE: str = "插值策略: 温度点间线性插值;超出数据温度范围取端点值(不外推)。"


@lru_cache(maxsize=1)
def _load_materials_document() -> dict:
    """读入 materials.yaml(进程内缓存);文件缺失/格式错误 → ValueError。"""
    try:
        with open(MATERIALS_YAML_PATH, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"材料库文件加载失败: {MATERIALS_YAML_PATH}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("materials"), dict):
        raise ValueError(f"材料库格式错误(缺少 materials 键): {MATERIALS_YAML_PATH}")
    return document


def load_material(name: str) -> dict:
    """按名取材料定义;未知名 → ValueError。

    返回深拷贝:调用方(FEM 材料块生成等)可安全叠加 overrides 而不污染进程内缓存。
    """
    materials = _load_materials_document()["materials"]
    if name not in materials:
        raise ValueError(f"未知材料: {name!r};可用: {sorted(materials)}")
    return copy.deepcopy(materials[name])


def available_material_names() -> list[str]:
    """材料库全部材料名(排序);供参数校验/自描述,避免调用方解析异常消息。"""
    return sorted(_load_materials_document()["materials"])


def interpolate(points: list[dict], temperature_c: float) -> float:
    """对 [{temperature_c, value}] 点列线性插值;点列乱序自动排序,越界取端点值。"""
    ordered = sorted(points, key=lambda p: float(p["temperature_c"]))
    temps = [float(p["temperature_c"]) for p in ordered]
    values = [float(p["value"]) for p in ordered]
    return float(np.interp(temperature_c, temps, values))


def run_material_query(params: MaterialQueryParams, ctx: RunContext) -> dict:
    """材料性能查询:对 temperatures_c 逐点插值出请求属性(缺数据属性输出 null)。"""
    try:
        material = load_material(params.material)
    except ValueError as exc:
        raise KernelError("INVALID_PARAMS", str(exc)) from exc

    available: dict = material.get("properties", {})
    requested = list(params.properties) if params.properties is not None else list(available)
    missing = [name for name in requested if name not in available]

    rows = [
        {
            "temperature_c": float(temperature),
            "properties": {
                name: interpolate(available[name], float(temperature))
                if name in available else None
                for name in requested
            },
        }
        for temperature in params.temperatures_c
    ]

    notes = [str(material.get("source_notes", "")).strip()]
    if missing:
        notes.append(f"库中无数据(输出 null)的属性: {', '.join(missing)}")
    notes.append(INTERPOLATION_NOTE)

    return {
        "material": params.material,
        "display_name": material.get("display_name", params.material),
        "density_kg_m3": material.get("density_kg_m3"),
        "rows": rows,
        "source_notes": [note for note in notes if note],
        "fidelity": "real",
    }
