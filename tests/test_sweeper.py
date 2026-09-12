"""存储清扫测试 — 配置层 / 按天 / 孤儿 / 配额 / 磁盘水位 / 生命周期.

配置层:StorageConfig 清理三字段(周期/水位/配额,0 值 = 关闭对应路)与
ServiceLogConfig(服务运行日志,uvicorn 接管用)的校验;清扫行为用例随后补齐。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ansys_hip.settings import (
    ServiceLogConfig,
    Settings,
    StorageConfig,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"


# ---------------------------------------------------------------------------
# 配置层:清理触发三字段 + 服务运行日志
# ---------------------------------------------------------------------------

class TestSweeperConfig:
    """StorageConfig 清理三字段:默认值 / 0 哨兵 / 边界校验。"""

    def test_defaults_keep_paths_off(self):
        # Arrange / Act / Assert(默认 = 兼容旧部署:周期 1h 开,水位与配额关)
        storage = StorageConfig()
        assert storage.sweep_interval_s == 3600
        assert storage.min_free_gb == 0.0
        assert storage.max_total_gb == 0.0

    def test_zero_disables_each_path(self):
        storage = StorageConfig(sweep_interval_s=0, min_free_gb=0, max_total_gb=0)
        assert storage.sweep_interval_s == 0

    def test_negative_values_rejected(self):
        with pytest.raises(ValidationError):
            StorageConfig(sweep_interval_s=-1)
        with pytest.raises(ValidationError):
            StorageConfig(min_free_gb=-0.1)
        with pytest.raises(ValidationError):
            StorageConfig(max_total_gb=-1)

    def test_unknown_key_rejected(self):
        # extra=forbid:拼写错误启动即暴露
        with pytest.raises(ValidationError):
            StorageConfig(sweep_interval_sec=60)


class TestServiceLogConfig:
    def test_defaults(self):
        service_log = ServiceLogConfig()
        assert service_log.file == "var/logs/service.log"
        assert service_log.retention_days == 14

    def test_retention_days_minimum_one(self):
        with pytest.raises(ValidationError):
            ServiceLogConfig(retention_days=0)

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            ServiceLogConfig(flie="x.log")

    def test_settings_derives_absolute_service_log_path(self, tmp_path):
        target = tmp_path / "logs" / "service.log"
        settings = Settings(service_log=ServiceLogConfig(file=str(target)))
        assert settings.service_log_path == target.resolve()


def test_deployed_config_enables_all_three_sweep_paths():
    """部署机配置三路清理全开(数值与 config/service.yaml 钉住,改配置须同步本用例)。"""
    settings = load_settings(REAL_CONFIG_PATH)
    assert settings.storage.sweep_interval_s == 3600
    assert settings.storage.min_free_gb == 20
    assert settings.storage.max_total_gb == 100
    assert settings.service_log.file == "/ansys/hip-var/logs/service.log"


def test_settings_factory_isolates_service_log(settings_factory, tmp_path):
    """conftest 隔离服务运行日志到 tmp:测试运行不写仓库 var/logs/service.log。"""
    settings = settings_factory()
    assert settings.service_log.file.startswith(str(tmp_path))
