"""存储清扫测试 — 配置层 / 按天 / 孤儿 / 配额 / 磁盘水位 / 生命周期.

配置层:StorageConfig 清理三字段(周期/水位/配额,0 值 = 关闭对应路)与
ServiceLogConfig(服务运行日志,uvicorn 接管用)的校验;清扫行为用例为
纯函数测试(手造目录 + state.json + os.utime 回拨时间),不依赖真实队列。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ansys_hip.settings import (
    ServiceLogConfig,
    Settings,
    StorageConfig,
    load_settings,
)
from ansys_hip.sweeper import sweep_expired_jobs, sweep_expired_uploads

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"

OLD_TS = (datetime(2020, 1, 1, tzinfo=timezone.utc)).timestamp()
FRESH_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")
STALE_ISO = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
    timespec="seconds"
)


def make_job_dir(
    jobs_root: Path,
    job_id: str,
    *,
    status: str = "succeeded",
    finished_at: str | None = None,
    created_at: str | None = None,
    state_json: str | None = None,
    mtime_ts: float | None = None,
) -> Path:
    """手造作业目录:state.json(或给定原始内容)+ 可选回拨目录 mtime。"""
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    if state_json is not None:
        (job_dir / "state.json").write_text(state_json, encoding="utf-8")
    else:
        payload: dict[str, object] = {"id": job_id, "status": status}
        if created_at is not None:
            payload["created_at"] = created_at
        if finished_at is not None:
            payload["finished_at"] = finished_at
        (job_dir / "state.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    if mtime_ts is not None:
        os.utime(job_dir, (mtime_ts, mtime_ts))
    return job_dir


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


# ---------------------------------------------------------------------------
# 按天清扫(纯函数):终态超期删 / 活跃保护 / 孤儿兜底
# ---------------------------------------------------------------------------

def test_sweep_expired_jobs_removes_only_stale_terminal(tmp_path):
    # Arrange(finished_at 超 3 天保留期 vs 刚完成)
    jobs_root = tmp_path / "jobs"
    stale = make_job_dir(jobs_root, "staleJob01", finished_at=STALE_ISO)
    fresh = make_job_dir(jobs_root, "freshJob01", finished_at=FRESH_ISO)

    # Act
    removed = sweep_expired_jobs(jobs_root, retention_days=3, active_ids=frozenset())

    # Assert
    assert removed == 1
    assert not stale.exists()
    assert fresh.is_dir()


def test_sweep_never_touches_pending_or_running_dirs(tmp_path):
    """pending/running 目录无条件保护(state.json 为准,与 active_ids 无关)。"""
    # Arrange(目录极旧 + 不在 active_ids,也不许删:运行现场)
    jobs_root = tmp_path / "jobs"
    pending = make_job_dir(
        jobs_root, "pendJob01", status="pending",
        created_at=STALE_ISO, mtime_ts=OLD_TS,
    )
    running = make_job_dir(
        jobs_root, "runJob001", status="running",
        created_at=STALE_ISO, mtime_ts=OLD_TS,
    )

    # Act
    removed = sweep_expired_jobs(jobs_root, retention_days=3, active_ids=frozenset())

    # Assert
    assert removed == 0
    assert pending.is_dir()
    assert running.is_dir()


def test_terminal_dir_of_active_job_protected_by_active_ids(tmp_path):
    """state.json 已终态但内存仍在册(state 落盘滞后)→ active_ids 第二道保险。"""
    # Arrange
    jobs_root = tmp_path / "jobs"
    active = make_job_dir(jobs_root, "actJob001", finished_at=STALE_ISO)
    stale = make_job_dir(jobs_root, "goneJob01", finished_at=STALE_ISO)

    # Act
    removed = sweep_expired_jobs(
        jobs_root, retention_days=3, active_ids=frozenset({"actJob001"})
    )

    # Assert
    assert removed == 1
    assert active.is_dir()
    assert not stale.exists()


def test_orphan_dirs_judged_by_mtime(tmp_path):
    """无/坏 state.json 的孤儿目录按目录 mtime 判龄(修复旧版永远跳过盲区)。"""
    # Arrange
    jobs_root = tmp_path / "jobs"
    orphan_old = make_job_dir(jobs_root, "orphOld01", mtime_ts=OLD_TS)
    orphan_new = make_job_dir(jobs_root, "orphNew01")  # mtime=now:可能处于
    #   submit 的 mkdir→state.json 落盘窗口,不许删
    broken = make_job_dir(
        jobs_root, "brokJson01", state_json="{ 撕裂的 JSON", mtime_ts=OLD_TS
    )

    # Act
    removed = sweep_expired_jobs(jobs_root, retention_days=3, active_ids=frozenset())

    # Assert
    assert removed == 2
    assert not orphan_old.exists()
    assert not broken.exists()
    assert orphan_new.is_dir()


def test_terminal_missing_finished_at_falls_back(tmp_path):
    """终态缺 finished_at:回退 created_at,再回退目录 mtime(不再永远跳过)。"""
    # Arrange
    jobs_root = tmp_path / "jobs"
    by_created = make_job_dir(jobs_root, "byCreat01", created_at=STALE_ISO)
    by_mtime = make_job_dir(jobs_root, "byMtime01", mtime_ts=OLD_TS)
    fresh_mtime = make_job_dir(jobs_root, "freshMt01")  # 无任何时间戳但目录新

    # Act
    removed = sweep_expired_jobs(jobs_root, retention_days=3, active_ids=frozenset())

    # Assert
    assert removed == 2
    assert not by_created.exists()
    assert not by_mtime.exists()
    assert fresh_mtime.is_dir()


def test_sweep_expired_jobs_missing_root_returns_zero(tmp_path):
    assert sweep_expired_jobs(tmp_path / "missing", 3, frozenset()) == 0
    assert sweep_expired_uploads(tmp_path / "missing", retention_days=3) == 0
