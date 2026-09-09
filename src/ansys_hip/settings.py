"""配置加载与三级参数合并 — service.yaml / 零件配置 / 环境变量 → 不可变 Settings.

覆盖优先级(与 README 一致):
    请求内联 params > config/parts/<零件>.yaml > config/service.yaml defaults

merge_params 为纯函数(无 I/O、无副作用,可独立单测):合并结果是一个
"方法参数模型顶层字段" 的 dict,由 api 层交给 model_validate 校验,
失败映射为 400 INVALID_PARAMS。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .registry import MethodSpec

DEFAULT_CONFIG_PATH = Path("config/service.yaml")
PARTS_DIRNAME = "parts"

# 环境变量 → (配置节, 配置键);同名环境变量覆盖 yaml 中的值
ENV_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("HIP_SERVICE_CONFIG", "", ""),          # 特殊:主配置文件路径,在加载前处理
    ("ANSYS_BIN", "ansys", "bin"),
    ("ANSYSLMD_LICENSE_FILE", "ansys", "license_file"),
    ("HIP_SERVICE_JOBS_DIR", "storage", "jobs_dir"),
)


class SettingsError(RuntimeError):
    """配置缺失/语法/结构错误 — 启动即失败,消息面向运维可读。"""


# ---------------------------------------------------------------------------
# 配置模型(全部 frozen + extra=forbid:配置键拼写错误在启动时暴露)
# ---------------------------------------------------------------------------

class ServerConfig(BaseModel):
    """HTTP 监听参数(uvicorn 启动时使用)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=8010, ge=1, le=65535)


class AnsysConfig(BaseModel):
    """MAPDL 批处理环境。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bin: str = ""
    license_file: str = ""
    np: int = Field(default=4, ge=1)
    job_timeout_s: int = Field(default=14400, ge=1)


class QueueConfig(BaseModel):
    """作业队列并发配置(单许可 → 默认 1)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent: int = Field(default=1, ge=1)


class StorageConfig(BaseModel):
    """作业/上传文件的落盘位置与保留策略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs_dir: str = "var/jobs"
    uploads_dir: str = "var/uploads"
    retention_days: int = Field(default=3, ge=1)


class DefaultsConfig(BaseModel):
    """全局默认(零件配置与请求内联参数可逐节覆盖);内容为原始 dict,
    结构由 merge_params 按方法参数模型分节消费。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle: dict[str, Any] = Field(default_factory=dict)
    materials: dict[str, Any] = Field(default_factory=dict)
    mesh: dict[str, Any] = Field(default_factory=dict)
    densification: dict[str, Any] = Field(default_factory=dict)


class MethodsConfig(BaseModel):
    """方法级开关(临时下线某方法 → 503 METHOD_DISABLED)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disabled: tuple[str, ...] = ()


class Settings(BaseModel):
    """服务全量配置(不可变);config_path/parts_dir 由加载过程注入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: ServerConfig = ServerConfig()
    ansys: AnsysConfig = AnsysConfig()
    queue: QueueConfig = QueueConfig()
    storage: StorageConfig = StorageConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    methods: MethodsConfig = MethodsConfig()
    config_path: Path = DEFAULT_CONFIG_PATH
    parts_dir: Path = DEFAULT_CONFIG_PATH.parent / PARTS_DIRNAME

    @property
    def jobs_root(self) -> Path:
        """作业根目录(绝对路径)。"""
        return Path(self.storage.jobs_dir).resolve()

    @property
    def uploads_root(self) -> Path:
        """上传文件根目录(绝对路径)。"""
        return Path(self.storage.uploads_dir).resolve()


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_settings(config_path: str | Path | None = None) -> Settings:
    """读主配置 yaml → 环境变量覆盖 → 不可变 Settings。

    任何缺失/语法/结构问题抛 SettingsError(消息含文件路径与原因)。
    """
    path = _resolve_config_path(config_path)
    raw = _read_yaml_dict(path)
    raw = _apply_env_overrides(raw)
    payload = {
        **raw,
        "config_path": path.resolve(),
        "parts_dir": path.resolve().parent / PARTS_DIRNAME,
    }
    try:
        return Settings.model_validate(payload)
    except ValidationError as exc:
        raise SettingsError(f"配置文件 {path} 结构不合法:\n{_format_validation(exc)}") from exc


def load_parts(config_dir: str | Path) -> dict[str, dict[str, Any]]:
    """读零件配置目录下全部 *.yaml → {零件名: 配置 dict}(按名排序稳定)。"""
    directory = Path(config_dir)
    if not directory.is_dir():
        raise SettingsError(f"零件配置目录不存在: {directory}")
    parts: dict[str, dict[str, Any]] = {}
    for file_path in sorted(directory.glob("*.yaml")):
        parts[file_path.stem] = _read_yaml_dict(file_path)
    return parts


def _resolve_config_path(config_path: str | Path | None) -> Path:
    """配置路径:显式参数 > 环境变量 HIP_SERVICE_CONFIG > 默认 config/service.yaml。"""
    if config_path is not None:
        return Path(config_path)
    env_path = os.environ.get("HIP_SERVICE_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    """读单个 yaml 并要求顶层是 dict;错误消息含路径与解析位置。"""
    if not path.is_file():
        raise SettingsError(
            f"配置文件不存在: {path}(可用环境变量 HIP_SERVICE_CONFIG 指定其他路径)"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(f"配置文件 {path} 不是合法 YAML:\n{exc}") from exc
    except OSError as exc:
        raise SettingsError(f"配置文件 {path} 读取失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"配置文件 {path} 顶层必须是映射(mapping),实际为 {type(raw).__name__}")
    return raw


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """按 ENV_OVERRIDES 用环境变量覆盖对应键;返回新 dict,不改动入参。"""
    result = raw
    for env_name, section, key in ENV_OVERRIDES:
        if not section:  # HIP_SERVICE_CONFIG 已在路径解析阶段处理
            continue
        value = os.environ.get(env_name)
        if value is None:
            continue
        result = {**result, section: {**result.get(section, {}), key: value}}
    return result


def _format_validation(exc: ValidationError) -> str:
    """pydantic 校验错误 → 每行 '字段路径: 原因' 的可读摘要。"""
    lines = [f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}" for error in exc.errors()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 三级参数合并(纯函数)
# ---------------------------------------------------------------------------

def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并 dict(overlay 优先);list 与标量整体替换,返回全新对象。"""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge_params(
    method_spec: MethodSpec,
    settings: Settings,
    part_config: dict[str, Any] | None = None,
    inline_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三级合并 → 方法参数模型顶层字段 dict。

    分节规则:
      - geometry / numerics ← 仅零件配置(主配置无此节);
      - materials ← defaults.materials 再叠零件 materials(powder/capsule 覆盖,overrides 深合并),
        按模型字段名落到 materials(或单数 material,仅 MaterialSelection 型);
      - cycle ← defaults.cycle;零件 cycle 非空则整体替换(points 不逐点合并);
      - mesh ← defaults.mesh 深合并零件 mesh;
      - defaults.densification 的 initial/limiting_relative_density → 模型同名顶层字段;
      - 最后内联 params 顶层键覆盖:dict 值深合并,其余类型整体替换。

    只产出目标模型存在的字段名(节与模型无关时自动丢弃)。
    """
    fields = method_spec.params_model.model_fields
    part = part_config or {}
    merged = _config_sections(fields, settings, part)
    return _apply_inline(merged, inline_params)


def _config_sections(
    fields: dict[str, Any],
    settings: Settings,
    part: dict[str, Any],
) -> dict[str, Any]:
    """主配置 defaults + 零件配置 → 各分节赋值(仅保留模型存在的字段)。"""
    sections: dict[str, Any] = {}
    sections.update(_geometry_section(fields, part))
    sections.update(_cycle_section(fields, settings, part))
    sections.update(_materials_section(fields, settings, part))
    sections.update(_mesh_section(fields, settings, part))
    sections.update(_numerics_section(fields, part))
    sections.update(_densification_section(fields, settings))
    return sections


def _geometry_section(fields: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """geometry ← 零件配置(主配置不提供几何)。"""
    geometry = part.get("geometry")
    if "geometry" in fields and isinstance(geometry, dict) and geometry:
        return {"geometry": dict(geometry)}
    return {}


def _cycle_section(
    fields: dict[str, Any],
    settings: Settings,
    part: dict[str, Any],
) -> dict[str, Any]:
    """cycle ← 主配置 defaults.cycle;零件 cycle 非空则整体替换。"""
    if "cycle" not in fields:
        return {}
    part_cycle = part.get("cycle")
    if isinstance(part_cycle, dict) and part_cycle:
        return {"cycle": dict(part_cycle)}
    if settings.defaults.cycle:
        return {"cycle": dict(settings.defaults.cycle)}
    return {}


def _materials_section(
    fields: dict[str, Any],
    settings: Settings,
    part: dict[str, Any],
) -> dict[str, Any]:
    """materials ← defaults.materials 深合并零件 materials(overrides 也深合并)。"""
    key = _materials_field_name(fields)
    if key is None:
        return {}
    part_materials = part.get("materials")
    selection = deep_merge(
        settings.defaults.materials,
        part_materials if isinstance(part_materials, dict) else {},
    )
    return {key: selection} if selection else {}


def _materials_field_name(fields: dict[str, Any]) -> str | None:
    """材料选择落到的字段名:materials 优先;单数 material 仅当非纯字符串型。"""
    if "materials" in fields:
        return "materials"
    if "material" in fields and fields["material"].annotation is not str:
        return "material"
    return None


def _mesh_section(
    fields: dict[str, Any],
    settings: Settings,
    part: dict[str, Any],
) -> dict[str, Any]:
    """mesh ← defaults.mesh 深合并零件 mesh。"""
    if "mesh" not in fields:
        return {}
    part_mesh = part.get("mesh")
    merged = deep_merge(settings.defaults.mesh, part_mesh if isinstance(part_mesh, dict) else {})
    return {"mesh": merged} if merged else {}


def _numerics_section(fields: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """numerics ← 仅零件配置(主配置不提供)。"""
    numerics = part.get("numerics")
    if "numerics" in fields and isinstance(numerics, dict) and numerics:
        return {"numerics": dict(numerics)}
    return {}


def _densification_section(fields: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """defaults.densification 的键映射到模型同名顶层字段(initial/limiting_relative_density)。"""
    return {
        key: value
        for key, value in settings.defaults.densification.items()
        if key in fields
    }


def _apply_inline(
    merged: dict[str, Any],
    inline_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """内联参数顶层键最后覆盖:dict 值深合并,其余类型整体替换。"""
    result = dict(merged)
    for key, value in (inline_params or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
