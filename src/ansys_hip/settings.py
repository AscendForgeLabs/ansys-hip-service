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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_CONFIG_PATH = Path("config/service.yaml")

# 环境变量 → (配置节, 配置键);同名环境变量覆盖 yaml 中的值
ENV_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("HIP_SERVICE_CONFIG", "", ""),          # 特殊:主配置文件路径,在加载前处理
    ("ANSYS_BIN", "ansys", "bin"),
    ("ANSYSLMD_LICENSE_FILE", "ansys", "license_file"),
    ("HIP_SERVICE_JOBS_DIR", "storage", "jobs_dir"),
    ("HIP_SERVICE_PASSTHROUGH_ENABLED", "passthrough", "enabled"),
    ("HIP_SERVICE_CORS_ORIGINS", "server", "cors_origins"),
    ("HIP_SERVICE_API_KEYS", "auth", "api_keys"),
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
    # 跨域放行源(如前端页面经 hip-playback 组件跨域拉取工件);默认空 = 不挂 CORS,
    # 行为与历史版本一致。环境变量 HIP_SERVICE_CORS_ORIGINS 支持逗号分隔字符串。
    cors_origins: tuple[str, ...] = ()

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_env_string(cls, value: object) -> object:
        """兼容环境变量传入的逗号分隔字符串("a, b" → ("a","b"));列表/元组原样。"""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value


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
    """作业/上传文件的落盘位置与保留策略。

    清理触发三路(0 值 = 关闭对应路):启动必扫一次;周期任务按 sweep_interval_s
    重复;磁盘剩余低于 min_free_gb 触发紧急清理。配额 max_total_gb 为第二道
    保险,超限按最老优先删终态作业(49G 爆盘事故的双保险设计)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs_dir: str = "var/jobs"
    uploads_dir: str = "var/uploads"
    retention_days: int = Field(default=3, ge=1)
    # 周期清扫间隔(秒):启动必扫一次,此后按间隔重复;0 = 关闭周期路(仅启动)
    sweep_interval_s: int = Field(default=3600, ge=0)
    # jobs/uploads 所在文件系统剩余空间水位(GB),低于即触发紧急清理;0 = 关闭水位路
    min_free_gb: float = Field(default=0.0, ge=0)
    # jobs+uploads 合计总量上限(GB),超限按最老优先删终态作业;0 = 关闭配额路
    max_total_gb: float = Field(default=0.0, ge=0)


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


class AuthConfig(BaseModel):
    """API Key 鉴权(fail-closed:api_keys 为空 = 除 /health 与 OPTIONS 外全部 401)。

    多 key 并存即配置级轮换:追加新 key → 客户端切换 → 移除旧 key,零代码轮换。
    真实 key 不入 git(config/service.yaml 已入库,生产值走环境变量
    HIP_SERVICE_API_KEYS,逗号分隔)。元素为 SecretStr:配置对象整体 repr/落日志
    自动脱敏,防明文 key 意外进 service.log(经 /service/log 可读)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_keys: tuple[SecretStr, ...] = ()

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_env_string(cls, value: object) -> object:
        """兼容环境变量传入的逗号分隔字符串("a, b" → ("a","b"));列表/元组
        须全为字符串——YAML 1.1 会把裸 0123/true 解析为数值/布尔,显式拒绝
        (pydantic 默认会静默 str() 强转,造成"配置看着对、比对永远不中")。"""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, (list, tuple)):
            if not all(isinstance(item, str) for item in value):
                raise ValueError(
                    "auth.api_keys 元素必须是字符串(YAML 中请引号包裹,防 0123/true 被解析为数值/布尔)"
                )
            return tuple(value)
        return value


class AccessLogConfig(BaseModel):
    """请求访问日志配置(独立完整服务日志:全部请求一字不漏,含 /health
    轮询、静态资源与 404;按天午夜轮转,面板 GET /service/log 可查)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = "var/logs/access.log"
    retention_days: int = Field(default=14, ge=1)


class ServiceLogConfig(BaseModel):
    """服务运行日志配置(uvicorn + 应用 logger 代码内接管,按天午夜轮转)。

    取代启动命令的 shell 重定向(旧 uvicorn.log 无轮转、路径不受配置控制);
    路径随本配置而非部署命令,自定义日志位置即改此 file。启动命令不得再传
    --log-config(会与本接管互抢)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = "var/logs/service.log"
    retention_days: int = Field(default=14, ge=1)


class Settings(BaseModel):
    """服务全量配置(不可变);config_path 由加载过程注入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: ServerConfig = ServerConfig()
    ansys: AnsysConfig = AnsysConfig()
    queue: QueueConfig = QueueConfig()
    storage: StorageConfig = StorageConfig()
    methods: MethodsConfig = MethodsConfig()
    passthrough: PassthroughConfig = PassthroughConfig()
    auth: AuthConfig = Field(default_factory=AuthConfig)
    access_log: AccessLogConfig = Field(default_factory=AccessLogConfig)
    service_log: ServiceLogConfig = Field(default_factory=ServiceLogConfig)
    config_path: Path = DEFAULT_CONFIG_PATH

    @model_validator(mode="after")
    def _distinct_log_files(self) -> "Settings":
        """两个日志文件不得同路径:同一路径两个按天轮转 handler 会互相抢轮转。"""
        if self.service_log.file == self.access_log.file:
            raise ValueError(
                "service_log.file 与 access_log.file 不能指向同一文件"
                "(service_log=服务运行日志,access_log=请求访问日志,各有独立轮转)"
            )
        return self

    @property
    def jobs_root(self) -> Path:
        """作业根目录(绝对路径)。"""
        return Path(self.storage.jobs_dir).resolve()

    @property
    def uploads_root(self) -> Path:
        """上传文件根目录(绝对路径)。"""
        return Path(self.storage.uploads_dir).resolve()

    @property
    def access_log_path(self) -> Path:
        """请求访问日志文件路径(绝对路径)。"""
        return Path(self.access_log.file).resolve()

    @property
    def service_log_path(self) -> Path:
        """服务运行日志文件路径(绝对路径)。"""
        return Path(self.service_log.file).resolve()

    @property
    def sweep_log_path(self) -> Path:
        """存储清理日志文件路径(service_log 同目录 sweep.log,随日志位置配置)。"""
        return self.service_log_path.parent / "sweep.log"


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
        if value is None or value == "":
            # 空串视为未设置:防 env_file/wrapper 脚本残留的 "VAR=" 空赋值
            # 静默清空 yaml 已配置的值(如 HIP_SERVICE_API_KEYS= 清掉全部 key)
            continue
        result = {**result, section: {**result.get(section, {}), key: value}}
    return result


def _format_validation(exc: ValidationError) -> str:
    """pydantic 校验错误 → 每行 '字段路径: 原因' 的可读摘要。"""
    lines = [f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}" for error in exc.errors()]
    return "\n".join(lines)
