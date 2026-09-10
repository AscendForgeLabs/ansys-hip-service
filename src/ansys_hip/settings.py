"""配置加载 — service.yaml / 环境变量 → 不可变 Settings.

类型化方法库与零件配置级(defaults / config/parts)已移除:服务收敛为
passthrough 单通道,参数只来自请求内联 params(经方法参数模型校验,
结果落盘 resolved-params.json 保证可追溯),配置层仅保留服务运行时项。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_CONFIG_PATH = Path("config/service.yaml")

# 环境变量 → (配置节, 配置键);同名环境变量覆盖 yaml 中的值
ENV_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("HIP_SERVICE_CONFIG", "", ""),          # 特殊:主配置文件路径,在加载前处理
    ("ANSYS_BIN", "ansys", "bin"),
    ("ANSYSLMD_LICENSE_FILE", "ansys", "license_file"),
    ("HIP_SERVICE_JOBS_DIR", "storage", "jobs_dir"),
    ("HIP_SERVICE_PASSTHROUGH_ENABLED", "passthrough", "enabled"),
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


class MethodsConfig(BaseModel):
    """方法级开关(临时下线某方法 → 503 METHOD_DISABLED)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disabled: tuple[str, ...] = ()


class PassthroughConfig(BaseModel):
    """直通通道开关(POST /sim/passthrough,任意 APDL 输入直接交 MAPDL 执行)。

    安全前提:开启即暴露任意 APDL 执行面(APDL 可读写文件、起系统命令),
    仅限受控内网 + 明确信任上游时开启;默认关闭,提交路由回 403
    PASSTHROUGH_DISABLED(区别于 404 端点不存在与 503 方法下线)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False


class Settings(BaseModel):
    """服务全量配置(不可变);config_path 由加载过程注入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: ServerConfig = ServerConfig()
    ansys: AnsysConfig = AnsysConfig()
    queue: QueueConfig = QueueConfig()
    storage: StorageConfig = StorageConfig()
    methods: MethodsConfig = MethodsConfig()
    passthrough: PassthroughConfig = PassthroughConfig()
    config_path: Path = DEFAULT_CONFIG_PATH

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
    payload = {**raw, "config_path": path.resolve()}
    try:
        return Settings.model_validate(payload)
    except ValidationError as exc:
        raise SettingsError(f"配置文件 {path} 结构不合法:\n{_format_validation(exc)}") from exc


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
