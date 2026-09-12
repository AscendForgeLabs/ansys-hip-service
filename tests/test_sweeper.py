"""存储清扫测试 — 配置层 / 按天 / 孤儿 / 配额 / 磁盘水位 / 生命周期.

配置层:StorageConfig 清理三字段(周期/水位/配额,0 值 = 关闭对应路)与
ServiceLogConfig(服务运行日志,uvicorn 接管用)的校验;清扫行为用例为
纯函数测试(手造目录 + state.json + os.utime 回拨时间),不依赖真实队列。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ansys_hip.api import create_app
from ansys_hip.settings import (
    ServiceLogConfig,
    Settings,
    StorageConfig,
    load_settings,
)
from ansys_hip import sweeper as sweeper_module
from ansys_hip.sweeper import (
    GIB,
    run_sweep_once,
    sweep_emergency,
    sweep_expired_jobs,
    sweep_expired_uploads,
    sweep_over_quota,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG_PATH = REPO_ROOT / "config" / "service.yaml"

OLD_TS = (datetime(2020, 1, 1, tzinfo=timezone.utc)).timestamp()
FRESH_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")
STALE_ISO = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
    timespec="seconds"
)


def _iso_days_ago(days: int) -> str:
    """N 天前的 ISO 时间串(构造不同"最老"梯度用)。"""
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# 生命周期(StorageSweeper 经 lifespan 挂载:启动即刻轮 + 周期任务)
# ---------------------------------------------------------------------------

def with_storage(settings: Settings, **overrides) -> Settings:
    """替换 storage 节字段的便捷拷贝(不可变模型,返回新实例)。"""
    return settings.model_copy(
        update={"storage": settings.storage.model_copy(update=overrides)}
    )


def test_startup_sweeper_removes_stale_dirs(settings_factory):
    """进 lifespan 即触发一轮清扫:超期作业目录与上传被删(承接旧启动清扫语义)。"""
    # Arrange
    settings = settings_factory()
    stale = make_job_dir(settings.jobs_root, "staleJob01", finished_at=STALE_ISO)
    stale_upload = settings.uploads_root / "old.inp"
    stale_upload.parent.mkdir(parents=True, exist_ok=True)
    stale_upload.write_text("x", encoding="utf-8")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).timestamp()
    os.utime(stale_upload, (old_ts, old_ts))

    # Act / Assert
    with TestClient(create_app(settings)):
        assert not stale.exists()
        assert not stale_upload.exists()


def test_sweeper_task_not_created_when_periodic_disabled(settings_factory):
    # Arrange(sweep_interval_s=0 且水位默认关 → 无后台任务,仅启动即刻轮)
    settings = with_storage(settings_factory(), sweep_interval_s=0)
    app = create_app(settings)

    # Act / Assert
    with TestClient(app):
        assert app.state.sweeper.is_running is False


def test_sweeper_task_stops_on_shutdown(settings_factory):
    # Arrange
    settings = with_storage(settings_factory(), sweep_interval_s=0.05)
    app = create_app(settings)

    # Act / Assert
    with TestClient(app):
        assert app.state.sweeper.is_running is True
    assert app.state.sweeper.is_running is False


def test_periodic_tick_survives_exception(settings_factory, monkeypatch, caplog):
    """单周期故障仅记日志,任务继续(清扫是长跑保障,不能因一次 OSError 挂掉)。"""
    # Arrange
    def boom(settings: Settings, active_ids: frozenset[str]) -> None:
        raise OSError("disk went away")

    settings = with_storage(settings_factory(), sweep_interval_s=0.05)
    app = create_app(settings)
    with TestClient(app):
        monkeypatch.setattr(sweeper_module, "run_sweep_once", boom)
        time.sleep(0.4)

        # Assert(任务仍活着,异常已落日志)
        assert app.state.sweeper.is_running is True
    assert any(
        "清扫周期执行异常" in record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 配额清理(纯函数):总量超限 → 最老终态作业优先 → 上传兜底
# ---------------------------------------------------------------------------

def _fill(path: Path, size: int) -> None:
    """写入指定字节数的占位文件(配额测试用)。"""
    path.write_bytes(b"x" * size)


def test_quota_deletes_oldest_terminal_first(tmp_path):
    # Arrange(总 6000 > 配额 4000;删最老终态(3000)后达标)
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    oldest = make_job_dir(jobs_root, "oldTerm1", finished_at=STALE_ISO)
    newest = make_job_dir(jobs_root, "newTerm1", finished_at=FRESH_ISO)
    _fill(oldest / "big.bin", 3000)
    _fill(newest / "big.bin", 3000)

    # Act
    removed = sweep_over_quota(
        jobs_root, uploads_root, max_total_bytes=4000, active_ids=frozenset()
    )

    # Assert(恰好删到达标即停:只删最老)
    assert removed == 1
    assert not oldest.exists()
    assert newest.is_dir()


def test_quota_noop_when_under_limit(tmp_path):
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    stale = make_job_dir(jobs_root, "oldTerm1", finished_at=STALE_ISO)
    _fill(stale / "big.bin", 100)

    assert sweep_over_quota(jobs_root, uploads_root, 10_000, frozenset()) == 0
    assert stale.is_dir()
    assert sweep_over_quota(jobs_root, uploads_root, 0, frozenset()) == 0  # 0 = 关闭


def test_quota_falls_through_to_uploads(tmp_path):
    # Arrange(唯一作业目录 active 不可删;超配额只能删上传,按最老 mtime 序)
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    active = make_job_dir(
        jobs_root, "actJob001", status="running", created_at=FRESH_ISO
    )
    _fill(active / "big.bin", 3000)
    old_upload = uploads_root / "old.inp"
    new_upload = uploads_root / "new.inp"
    _fill(old_upload, 2000)
    _fill(new_upload, 2000)
    os.utime(old_upload, (OLD_TS, OLD_TS))

    # Act(总 7000 > 配额 3000;删两个上传后 = 3000 达标,active 目录不动)
    removed = sweep_over_quota(
        jobs_root, uploads_root, max_total_bytes=3000, active_ids=frozenset({"actJob001"})
    )

    # Assert
    assert removed == 2
    assert not old_upload.exists()
    assert not new_upload.exists()
    assert active.is_dir()


def test_quota_nothing_deletable_keeps_all(tmp_path):
    # Arrange(active 目录超配额但无任何可删对象)
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    active = make_job_dir(jobs_root, "actJob001", status="running")
    _fill(active / "big.bin", 5000)

    # Act
    removed = sweep_over_quota(
        jobs_root, uploads_root, max_total_bytes=3000, active_ids=frozenset({"actJob001"})
    )

    # Assert
    assert removed == 0
    assert active.is_dir()


def test_run_sweep_once_includes_quota_path(settings_factory):
    """周期轮完整链路:按天不动的新鲜终态作业,因配额超限被删最老。"""
    # Arrange(retention 默认 3 天:两作业均新鲜不超期;配额调到只容一个)
    settings = with_storage(settings_factory(), max_total_gb=4000 / 1024**3)
    jobs_root = settings.jobs_root
    oldest = make_job_dir(
        jobs_root, "oldTerm1",
        finished_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="seconds"
        ),
    )
    newest = make_job_dir(
        jobs_root, "newTerm1", finished_at=FRESH_ISO
    )
    _fill(oldest / "big.bin", 3000)
    _fill(newest / "big.bin", 3000)

    # Act
    run_sweep_once(settings, frozenset())

    # Assert
    assert not oldest.exists()
    assert newest.is_dir()


# ---------------------------------------------------------------------------
# 磁盘水位监控 + 紧急清理(无视保留期,滞回目标 = 阈值 + 5G)
# ---------------------------------------------------------------------------

def test_emergency_sweep_until_hysteresis_target(tmp_path, monkeypatch):
    """紧急清理删到目标即停:每个目录"占 12G",删最老两个到 25G 恰好达标。"""
    # Arrange(剩余空间 = 25G - 12G×(现存目录数-1):删一个多 12G,确定性模型)
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    oldest = make_job_dir(jobs_root, "oldTerm1", finished_at=_iso_days_ago(30))
    middle = make_job_dir(jobs_root, "midTerm1", finished_at=_iso_days_ago(20))
    newest = make_job_dir(jobs_root, "newTerm1", finished_at=FRESH_ISO)
    candidates = (oldest, middle, newest)

    def fake_free(path: Path) -> int:
        existing = sum(1 for job_dir in candidates if job_dir.exists())
        return 25 * GIB - 12 * GIB * (existing - 1)

    monkeypatch.setattr(sweeper_module, "_disk_free_bytes", fake_free)

    # Act(目标 25G:3 目录时 1G → 删最老 13G 仍低 → 删次老 25G 达标即停)
    recovered = sweep_emergency(
        jobs_root, uploads_root,
        target_free_bytes=25 * GIB,
        watched_paths=(tmp_path,),
        active_ids=frozenset(),
    )

    # Assert
    assert recovered is True
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.is_dir()


def test_emergency_logs_error_and_keeps_active_when_exhausted(
    tmp_path, monkeypatch, caplog
):
    # Arrange(恒低 1G:候选耗尽仍不达标 → error 报警;活跃目录绝不删)
    monkeypatch.setattr(
        sweeper_module, "_disk_free_bytes", lambda path: 1 * GIB
    )
    jobs_root = tmp_path / "jobs"
    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    active = make_job_dir(jobs_root, "actJob001", status="running")
    terminal = make_job_dir(jobs_root, "termJob01", finished_at=FRESH_ISO)
    upload = uploads_root / "some.inp"
    upload.write_text("x", encoding="utf-8")

    # Act
    with caplog.at_level("ERROR"):
        recovered = sweep_emergency(
            jobs_root, uploads_root,
            target_free_bytes=25 * GIB,
            watched_paths=(tmp_path,),
            active_ids=frozenset({"actJob001"}),
        )

    # Assert
    assert recovered is False
    assert not terminal.exists()
    assert not upload.exists()
    assert active.is_dir()
    assert any("需人工介入" in record.getMessage() for record in caplog.records)


def test_sweeper_task_created_when_only_watermark_on(settings_factory):
    # Arrange(周期关、水位开 → 任务仍建立:双时钟各管各的)
    settings = with_storage(settings_factory(), sweep_interval_s=0, min_free_gb=10)
    app = create_app(settings)

    # Act / Assert(tmp 所在盘真实剩余充足 → 只验证任务建立,不触发紧急)
    with TestClient(app):
        assert app.state.sweeper.is_running is True


def test_watermark_monitor_triggers_emergency(
    settings_factory, monkeypatch, caplog
):
    """水位监控接线:60s 轮询(测试压到 0.05s)发现低于阈值 → 紧急清理真实执行。"""
    # Arrange
    monkeypatch.setattr(sweeper_module, "DISK_CHECK_INTERVAL_S", 0.05)
    monkeypatch.setattr(
        sweeper_module, "_disk_free_bytes", lambda path: 1 * GIB
    )
    settings = with_storage(settings_factory(), sweep_interval_s=0, min_free_gb=20)
    fresh = make_job_dir(settings.jobs_root, "freshJob1", finished_at=FRESH_ISO)
    app = create_app(settings)

    # Act
    with TestClient(app):
        time.sleep(0.5)
        assert app.state.sweeper.is_running is True

    # Assert(新鲜终态目录被紧急清理删除;候选耗尽 → error 报警)
    assert not fresh.exists()
    assert any("需人工介入" in record.getMessage() for record in caplog.records)
