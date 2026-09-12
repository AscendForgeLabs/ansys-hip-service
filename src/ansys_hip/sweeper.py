"""存储清扫 — 按天保留 / 配额 / 磁盘水位三路清理 + 孤儿目录兜底.

清理目标路径全部来自 Settings(jobs_root / uploads_root),不写死任何位置。
触发三路(开关见 StorageConfig):

- 启动一次:StorageSweeper.start 即刻轮(语义同旧版启动清扫 + 孤儿修复);
- 周期任务:sweep_interval_s(0 = 关闭);
- 磁盘水位:min_free_gb(0 = 关闭;60s 轮询,紧急清理无视保留期)。

永不删除:pending/running 的作业目录(state.json 为准)与 active_ids 内的
目录(内存注册先于 state.json 落盘,防落盘滞后的第二道保险)— 运行中/在册
作业目录是排障现场,任何清理路都不碰。

孤儿兜底(修复旧版「无 state.json / 终态缺 finished_at 永远跳过」盲区):
按目录 mtime 对齐 retention_days 判删。submit 的 mkdir→内存注册→state.json
落盘之间存在微秒级无 state.json 窗口,但该窗口目录 mtime=now,远新于天级
cutoff,天然安全。

本文件前半为模块级同步纯函数(文件系统副作用,可单测),供 StorageSweeper
经 asyncio.to_thread 调用,不在事件循环线程直接执行。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .queue import (
    STATE_FILENAME,
    TERMINAL_STATUS_VALUES,
    JobQueue,
    load_state_payload,
    parse_iso,
)
from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobDirInfo:
    """可清理候选目录的只读画像(终态或孤儿;活跃目录不入列)。"""

    job_dir: Path
    status: str | None            # None = 孤儿(无/坏 state.json)
    sort_key: datetime            # finished_at → created_at → 目录 mtime 逐级回退


def _dir_mtime(path: Path) -> datetime:
    """目录 mtime(UTC);目录竞态消失时返回 epoch(排序垫底,不抛出)。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def scan_job_dirs(
    jobs_root: Path, active_ids: frozenset[str]
) -> tuple[JobDirInfo, ...]:
    """扫描 jobs 根,返回可清理候选画像(终态 + 孤儿,按 sort_key 升序)。

    排除两类:pending/running(以 state.json 为准,无条件保护)与
    active_ids 内的目录;未知 status 一律保守跳过(可能是未来格式)。
    """
    if not jobs_root.is_dir():
        return ()
    entries: list[JobDirInfo] = []
    for job_dir in sorted(path for path in jobs_root.iterdir() if path.is_dir()):
        if job_dir.name in active_ids:
            continue
        payload = load_state_payload(job_dir / STATE_FILENAME)
        if payload is None:
            # 孤儿:无/坏 state.json,按目录 mtime 判龄
            entries.append(JobDirInfo(job_dir, None, _dir_mtime(job_dir)))
            continue
        if payload.get("status") not in TERMINAL_STATUS_VALUES:
            continue
        sort_key = (
            parse_iso(payload.get("finished_at"))
            or parse_iso(payload.get("created_at"))
            or _dir_mtime(job_dir)
        )
        entries.append(JobDirInfo(job_dir, payload.get("status"), sort_key))
    return tuple(sorted(entries, key=lambda info: info.sort_key))


def sweep_expired_jobs(
    jobs_root: Path, retention_days: int, active_ids: frozenset[str]
) -> int:
    """删除超过保留期的终态/孤儿作业目录(sort_key < cutoff);返回删除数。

    终态以 finished_at 判龄,缺失逐级回退 created_at → 目录 mtime;
    孤儿按目录 mtime — 与旧版启动清扫的差异仅在修复「永远跳过」盲区。
    """
    if not jobs_root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for info in scan_job_dirs(jobs_root, active_ids):
        if info.sort_key >= cutoff:
            continue
        shutil.rmtree(info.job_dir, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("已清扫超期作业目录 %d 个(保留 %d 天)", removed, retention_days)
    return removed


def sweep_expired_uploads(uploads_root: Path, retention_days: int) -> int:
    """删除超过保留期的上传文件(按修改时间);返回删除数。

    自 queue.py 迁入(行为不变);周期调用时上传可能仍被排队作业引用
    (内核运行时才复制进 job_dir),极端盘压下删到新上传属已 documented
    的取舍 — 见模块 docstring 与 StorageSweeper 紧急清理说明。
    """
    if not uploads_root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for upload in sorted(path for path in uploads_root.iterdir() if path.is_file()):
        try:
            modified_at = datetime.fromtimestamp(upload.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if modified_at >= cutoff:
            continue
        upload.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info("已清扫超期上传 %d 个(保留 %d 天)", removed, retention_days)
    return removed


def run_sweep_once(settings: Settings, active_ids: frozenset[str]) -> None:
    """一轮按天清扫(jobs + uploads);启动即刻轮与周期轮共用的入口。"""
    retention_days = settings.storage.retention_days
    sweep_expired_jobs(settings.jobs_root, retention_days, active_ids)
    sweep_expired_uploads(settings.uploads_root, retention_days)


# ---------------------------------------------------------------------------
# 生命周期(后台任务;磁盘水位监控随后接入同一任务的双时钟)
# ---------------------------------------------------------------------------

# 周期循环的最小休眠(秒):防配置极小值时忙转
_TICK_FLOOR_S = 0.1


class StorageSweeper:
    """周期清扫后台任务(生命周期由 FastAPI lifespan 管理)。

    - start():先执行一轮即刻清扫(= 旧启动清扫语义 + 孤儿修复),
      再按配置起后台任务(sweep_interval_s=0 → 不建任务);
    - 每 tick 在事件循环线程现取 active_ids 快照,重活经 asyncio.to_thread
      进线程池,不阻塞事件循环;
    - 单周期故障仅记日志,任务继续(清扫是长跑保障,不能因一次 OSError 挂掉)。
    """

    def __init__(self, settings: Settings, queue: JobQueue) -> None:
        self._settings = settings
        self._queue = queue
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """后台任务是否在跑(未配置周期 = 恒 False)。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """启动即刻清扫一轮,再按配置起周期后台任务。"""
        active = self._queue.active_job_ids()
        await asyncio.to_thread(run_sweep_once, self._settings, active)
        interval_s = self._settings.storage.sweep_interval_s
        if interval_s <= 0:
            logger.info("存储清扫:仅启动时执行一轮(sweep_interval_s=0)")
            return
        self._task = asyncio.create_task(self._run(), name="hip-storage-sweeper")
        logger.info(
            "存储清扫任务已启动(周期 %ds,作业根 %s)", interval_s, self._settings.jobs_root
        )

    async def stop(self) -> None:
        """取消后台任务并等待收尸(幂等,未建任务时为空操作)。"""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        interval_s = float(self._settings.storage.sweep_interval_s)
        next_sweep = time.monotonic() + interval_s
        while True:
            now = time.monotonic()
            await asyncio.sleep(max(next_sweep - now, _TICK_FLOOR_S))
            now = time.monotonic()
            try:
                if now >= next_sweep:
                    # 先排下一轮再执行:本周期抛错也按整周期退避,不刷屏重试
                    next_sweep = now + interval_s
                    active = self._queue.active_job_ids()
                    await asyncio.to_thread(run_sweep_once, self._settings, active)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单周期故障不拖垮长跑任务
                logger.exception("清扫周期执行异常,下一周期继续")
