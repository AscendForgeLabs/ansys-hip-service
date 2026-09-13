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
落盘之间存在微秒级无 state.json 窗口 — 配额/水位路「删到达标」没有年龄
下限,故孤儿候选设 ORPHAN_GRACE_S 宽限期:不满宽限期的新目录(mtime=now,
即落盘窗口内)不入任何候选,使「永不删在册目录」对全部三路硬成立。

本文件前半为模块级同步纯函数(文件系统副作用,可单测),供 StorageSweeper
经 asyncio.to_thread 调用,不在事件循环线程直接执行。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
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

# GiB(与 df 的 G 同口径):配置的 GB 值 → 字节
GIB = 1024**3

# 磁盘水位轮询间隔(秒;常量不进配置 — 粒度无调优需求,statvfs 级开销)
DISK_CHECK_INTERVAL_S = 60.0
# 紧急清理恢复目标 = min_free_gb + 此 GiB 数(滞回防抖:删到 25G 才停,
# 避免在 20G 阈值边界每 60s 反复触发/停止)
EMERGENCY_HEADROOM_GB = 5.0
# 孤儿候选宽限期(秒):submit 的 mkdir→state.json 落盘窗口内目录必带新
# mtime,不满宽限期不入候选(retention 路天级 cutoff 本就不受影响)
ORPHAN_GRACE_S = 300.0

# 清理原因标签(逐项删除行「删除[原因]」与汇总行共用;grep「删除[」收齐全部删除)
REASON_RETENTION = "保留期"
REASON_QUOTA = "配额"
REASON_WATERMARK = "水位"


@dataclass(frozen=True)
class JobDirInfo:
    """可清理候选目录的只读画像(终态或孤儿;活跃目录不入列)。"""

    job_dir: Path
    status: str | None            # None = 孤儿(无/坏 state.json)
    sort_key: datetime            # finished_at → created_at → 目录 mtime 逐级回退


def _path_mtime(path: Path) -> datetime:
    """路径 mtime(UTC);竞态消失时返回 epoch(排序垫底,不抛出)。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _tree_size_bytes(path: Path) -> int:
    """目录树递归大小(字节);不存在返回 0,单文件竞态消失按 0 计。"""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _error: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _fmt_size(size_bytes: int) -> str:
    """字节数 → 人类可读自适应单位(B/K/M/G/T;删除行与汇总行共用)。"""
    if size_bytes >= 1024**4:
        return f"{size_bytes / 1024**4:.1f}T"
    if size_bytes >= GIB:
        return f"{size_bytes / GIB:.1f}G"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f}M"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}K"
    return f"{size_bytes}B"


def _path_size(path: Path) -> int:
    """文件或目录的大小(字节);竞态消失返回 0,不抛出。

    不能用 _tree_size_bytes 打文件 — os.walk 对文件路径返回空恒 0。
    """
    try:
        if path.is_dir():
            return _tree_size_bytes(path)
        return path.stat().st_size
    except OSError:
        return 0


def _log_removed_job(reason: str, info: JobDirInfo, size_bytes: int) -> None:
    """逐作业目录删除行:哪个(名)/为何(reason)/删了什么(终态画像|孤儿 + 大小)。"""
    portrait = "孤儿" if info.status is None else f"终态{info.status}"
    level = logging.WARNING if reason == REASON_WATERMARK else logging.INFO
    logger.log(
        level,
        "删除[%s] 作业目录 %s(%s,%s)",
        reason,
        info.job_dir.name,
        portrait,
        _fmt_size(size_bytes),
    )


def _log_removed_upload(reason: str, name: str, size_bytes: int) -> None:
    """逐上传删除行(上传无终态画像,仅大小)。"""
    level = logging.WARNING if reason == REASON_WATERMARK else logging.INFO
    logger.log(level, "删除[%s] 上传 %s(%s)", reason, name, _fmt_size(size_bytes))


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
            # 孤儿:无/坏 state.json,按目录 mtime 判龄;不满宽限期的新目录
            # 可能处于 submit 落盘窗口,不入任何清理候选(含配额/水位路)
            mtime = _path_mtime(job_dir)
            if mtime > datetime.now(timezone.utc) - timedelta(seconds=ORPHAN_GRACE_S):
                continue
            entries.append(JobDirInfo(job_dir, None, mtime))
            continue
        if payload.get("status") not in TERMINAL_STATUS_VALUES:
            continue
        sort_key = (
            parse_iso(payload.get("finished_at"))
            or parse_iso(payload.get("created_at"))
            or _path_mtime(job_dir)
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
    freed_bytes = 0
    for info in scan_job_dirs(jobs_root, active_ids):
        if info.sort_key >= cutoff:
            continue
        size_bytes = _path_size(info.job_dir)
        shutil.rmtree(info.job_dir, ignore_errors=True)
        if not info.job_dir.exists():  # 部分失败不计入,留待下周期重试
            removed += 1
            freed_bytes += size_bytes
            _log_removed_job(REASON_RETENTION, info, size_bytes)
    if removed:
        logger.info(
            "已清扫超期作业目录 %d 个(保留 %d 天,共释放 %s,磁盘剩余 %s)",
            removed,
            retention_days,
            _fmt_size(freed_bytes),
            _free_g(jobs_root),
        )
    return removed


def sweep_expired_uploads(uploads_root: Path, retention_days: int) -> int:
    """删除超过保留期的上传文件(按修改时间);返回删除数。

    自 queue.py 迁入,单文件判龄不变;执行频率从「仅启动」变为周期 —
    上传自此有硬 TTL:上传后隔 retention_days 才提交的作业会在受理后
    内核拷贝时失败(按天保留语义的自然结果,上游应上传后及时提交)。
    极端盘压下紧急清理还可能删到未超期上传(见模块 docstring 已知边界)。
    """
    if not uploads_root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    freed_bytes = 0
    for upload in sorted(path for path in uploads_root.iterdir() if path.is_file()):
        try:
            stat = upload.stat()
        except OSError:
            continue
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if modified_at >= cutoff:
            continue
        upload.unlink(missing_ok=True)
        if not upload.exists():
            removed += 1
            freed_bytes += stat.st_size
            _log_removed_upload(REASON_RETENTION, upload.name, stat.st_size)
    if removed:
        logger.info(
            "已清扫超期上传 %d 个(保留 %d 天,共释放 %s,磁盘剩余 %s)",
            removed,
            retention_days,
            _fmt_size(freed_bytes),
            _free_g(uploads_root),
        )
    return removed


def sweep_over_quota(
    jobs_root: Path,
    uploads_root: Path,
    max_total_bytes: int,
    active_ids: frozenset[str],
) -> int:
    """jobs+uploads 合计超配额 → 按最老优先删终态作业,删尽再删最老上传,至达标。

    第二道保险(49G 累积事故):按天保留拦不住作业风暴或单作业超大文件,
    总量上限直接封顶。删除顺序与紧急清理同口径(sort_key 升序 → 上传 mtime
    升序);每删一个候选复测总量,恰好达标即停。返回删除的目录/文件数。
    """
    if max_total_bytes <= 0:
        return 0

    def total_bytes() -> int:
        return _tree_size_bytes(jobs_root) + _tree_size_bytes(uploads_root)

    if total_bytes() <= max_total_bytes:
        return 0
    removed_jobs = 0
    removed_uploads = 0
    freed_bytes = 0
    for info in scan_job_dirs(jobs_root, active_ids):  # 已按 sort_key 升序
        if total_bytes() <= max_total_bytes:
            break
        size_bytes = _path_size(info.job_dir)
        shutil.rmtree(info.job_dir, ignore_errors=True)
        if not info.job_dir.exists():  # 部分失败不计入,留待下周期重试
            removed_jobs += 1
            freed_bytes += size_bytes
            _log_removed_job(REASON_QUOTA, info, size_bytes)
    if total_bytes() > max_total_bytes and uploads_root.is_dir():
        # 作业候选删尽仍超:最老上传文件兜底(可能删到已上传未提交的文件 —
        # 最老排序兜底,仅极端超配额时发生,见模块 docstring 已知边界)
        uploads = sorted(
            (path for path in uploads_root.iterdir() if path.is_file()),
            key=_path_mtime,
        )
        for upload in uploads:
            if total_bytes() <= max_total_bytes:
                break
            size_bytes = _path_size(upload)
            upload.unlink(missing_ok=True)
            if not upload.exists():
                removed_uploads += 1
                freed_bytes += size_bytes
                _log_removed_upload(REASON_QUOTA, upload.name, size_bytes)
    removed = removed_jobs + removed_uploads
    if removed:
        logger.info(
            "配额清理:删除作业目录 %d 个、上传 %d 个(上限 %s,现总量 %s,磁盘剩余 %s)",
            removed_jobs,
            removed_uploads,
            _fmt_size(max_total_bytes),
            _fmt_size(total_bytes()),
            _free_g(jobs_root),
        )
    if total_bytes() > max_total_bytes:
        # 可删对象耗尽仍超限:不静默(否则违例持续期间运维毫无感知),
        # 与水位路「候选耗尽需人工介入」同语义,每周期重复报警不节流
        logger.warning(
            "配额超限:可删对象已耗尽仍超限(上限 %s,现总量 %s;"
            "活跃作业现场不删),需调大配额或人工清理",
            _fmt_size(max_total_bytes),
            _fmt_size(total_bytes()),
        )
    return removed


def _disk_free_bytes(path: Path) -> int:
    """路径所在文件系统剩余字节(独立封装,测试以 monkeypatch 替换)。"""
    return shutil.disk_usage(path).free


def _free_g(path: Path) -> str:
    """单路径磁盘剩余(固定 G 口径;保留期/配额汇总行的「磁盘剩余」字段)。"""
    return f"{_disk_free_bytes(path) / GIB:.1f}G"


def _free_desc(paths: tuple[Path, ...]) -> str:
    """受监视盘剩余描述(水位触发行/紧急汇总/耗尽告警共用,逐盘列出)。"""
    return ", ".join(f"{path} 剩 {_disk_free_bytes(path) / GIB:.1f}G" for path in paths)


def configure_sweep_logging(settings: Settings) -> None:
    """清理日志 → service_log 同目录 sweep.log(按天轮转;幂等重绑不累积)。

    位置随 service_log.file 的目录走(自定义日志位置即同时 relocate);
    propagate 保持 True — 清理事件同时进 service.log(完整运行日志),
    sweep.log 是运维聚焦视图,GET /service/sweep-log 尾读。
    """
    for stale_handler in list(logger.handlers):
        logger.removeHandler(stale_handler)
        stale_handler.close()
    logger.setLevel(logging.INFO)
    target = settings.sweep_log_path
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        target,
        when="midnight",
        backupCount=settings.service_log.retention_days,
        encoding="utf-8",
    )
    handler.name = "hip-sweep-file"
    # 时间戳 + 级别:清扫事件没有时间戳就答不了"何时"(对齐 service.log 口径,
    # 单 logger 聚焦视图故省 %(name)s)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    logger.addHandler(handler)


def _watched_filesystems(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """受监视路径按所在设备去重(jobs/uploads 同盘只查一次);缺失路径剔除。

    只监控服务自己的数据盘:清理性动作只能释放 jobs/uploads 的数据,系统盘
    告警属运维面(见文档)。存储根目录尚未创建时不监控(无数据无压力)。
    """
    seen_devices: set[int] = set()
    watched: list[Path] = []
    for path in paths:
        try:
            device = path.stat().st_dev
        except OSError:
            continue
        if device not in seen_devices:
            seen_devices.add(device)
            watched.append(path)
    return tuple(watched)


def sweep_emergency(
    jobs_root: Path,
    uploads_root: Path,
    target_free_bytes: int,
    watched_paths: tuple[Path, ...],
    active_ids: frozenset[str],
) -> bool:
    """水位触发的紧急清理:无视保留期按最老删(作业→上传)至受监视盘达标。

    返回是否恢复到目标;候选耗尽仍低 → logger.error(每次水位检查都会重复
    报警,事故级噪音刻意不节流),只能人工介入 — 活跃作业目录是运行现场,
    任何盘压下都不删。
    """
    def recovered() -> bool:
        return all(
            _disk_free_bytes(path) >= target_free_bytes for path in watched_paths
        )

    if recovered():
        return True
    removed_jobs = 0
    removed_uploads = 0
    freed_bytes = 0
    for info in scan_job_dirs(jobs_root, active_ids):  # 已按 sort_key 升序
        if recovered():
            break
        size_bytes = _path_size(info.job_dir)
        shutil.rmtree(info.job_dir, ignore_errors=True)
        if info.job_dir.exists():  # 部分失败不计入,留待下轮水位检查重试
            continue
        removed_jobs += 1
        freed_bytes += size_bytes
        _log_removed_job(REASON_WATERMARK, info, size_bytes)
    if not recovered() and uploads_root.is_dir():
        # 作业候选删尽仍低:最老上传兜底(可能删到已上传未提交的文件 —
        # 最老排序兜底,仅极端盘压时发生,见模块 docstring 已知边界)
        uploads = sorted(
            (path for path in uploads_root.iterdir() if path.is_file()),
            key=_path_mtime,
        )
        for upload in uploads:
            if recovered():
                break
            size_bytes = _path_size(upload)
            upload.unlink(missing_ok=True)
            if not upload.exists():
                removed_uploads += 1
                freed_bytes += size_bytes
                _log_removed_upload(REASON_WATERMARK, upload.name, size_bytes)
    if removed_jobs or removed_uploads:
        # 删后现值(与水位触发行的删前值互补,水位全程可追)
        logger.warning(
            "紧急清理:删除作业目录 %d 个、上传 %d 个(共释放 %s,现剩 %s)",
            removed_jobs,
            removed_uploads,
            _fmt_size(freed_bytes),
            _free_desc(watched_paths),
        )
    if recovered():
        return True
    logger.error(
        "磁盘水位紧急清理后仍低于目标(%s < %dG):已无可清理对象"
        "(活跃作业 %d 个在跑、运行现场不删),需人工介入",
        _free_desc(watched_paths),
        target_free_bytes // GIB,
        len(active_ids),
    )
    return False


def _run_watermark_check(
    settings: Settings, active_ids: frozenset[str]
) -> None:
    """一轮水位检查:任一受监视盘剩余 < min_free_gb → 紧急清理(滞回目标)。"""
    threshold_gb = settings.storage.min_free_gb
    watched = _watched_filesystems((settings.jobs_root, settings.uploads_root))
    if not watched:
        return
    if all(_disk_free_bytes(path) >= threshold_gb * GIB for path in watched):
        return
    logger.warning(
        "磁盘水位告警(%s 低于 %gG):触发紧急清理,目标恢复到 %gG",
        _free_desc(watched),
        threshold_gb,
        threshold_gb + EMERGENCY_HEADROOM_GB,
    )
    sweep_emergency(
        settings.jobs_root,
        settings.uploads_root,
        int((threshold_gb + EMERGENCY_HEADROOM_GB) * GIB),
        watched,
        active_ids,
    )


def run_sweep_once(settings: Settings, active_ids: frozenset[str]) -> None:
    """一轮完整清扫:按天(jobs + uploads)+ 配额;启动即刻轮与周期轮共用入口。"""
    storage = settings.storage
    sweep_expired_jobs(settings.jobs_root, storage.retention_days, active_ids)
    sweep_expired_uploads(settings.uploads_root, storage.retention_days)
    if storage.max_total_gb > 0:
        sweep_over_quota(
            settings.jobs_root,
            settings.uploads_root,
            int(storage.max_total_gb * GIB),
            active_ids,
        )


# ---------------------------------------------------------------------------
# 生命周期(后台任务;磁盘水位监控随后接入同一任务的双时钟)
# ---------------------------------------------------------------------------

# 周期循环的最小休眠(秒):防配置极小值时忙转
_TICK_FLOOR_S = 0.1


class StorageSweeper:
    """周期清扫 + 磁盘水位监控的后台任务(生命周期由 FastAPI lifespan 管理)。

    - start():先执行一轮即刻清扫(= 旧启动清扫语义 + 孤儿修复),再按配置
      起后台任务(sweep_interval_s 与 min_free_gb 均为 0 → 不建任务);
    - 单任务双时钟:周期清扫与水位检查各自计时,谁到点谁跑,取最近者休眠;
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
        """后台任务是否在跑(周期与水位均关闭 = 恒 False)。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """先播报配置(横幅不代表删除),再即刻清扫一轮,最后起后台任务。

        横幅前置:首轮删除行紧随横幅之下,运维读日志先见配置再见动作。
        """
        storage = self._settings.storage
        interval_s = storage.sweep_interval_s
        watch_disk = storage.min_free_gb > 0
        no_task = interval_s <= 0 and not watch_disk
        logger.info(
            "存储清扫服务已上线(本行仅播报配置,不代表删除任何内容;"
            "删除事件逐行以「删除[原因] 对象(状态,大小)」记录):"
            "周期清扫 %s、按天保留 %d 天、配额上限 %s、水位阈值 %s%s、作业根 %s",
            f"{interval_s}s" if interval_s > 0 else "关",
            storage.retention_days,
            f"{storage.max_total_gb:g}G" if storage.max_total_gb > 0 else "关",
            f"{storage.min_free_gb:g}G" if watch_disk else "关",
            ";无后台任务,仅启动这一轮" if no_task else "",
            self._settings.jobs_root,
        )
        active = self._queue.active_job_ids()
        await asyncio.to_thread(run_sweep_once, self._settings, active)
        if no_task:
            return
        self._task = asyncio.create_task(self._run(), name="hip-storage-sweeper")

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
        started = time.monotonic()
        next_sweep = started + (interval_s if interval_s > 0 else math.inf)
        next_disk = (
            started + DISK_CHECK_INTERVAL_S
            if self._settings.storage.min_free_gb > 0
            else math.inf
        )
        while True:
            now = time.monotonic()
            await asyncio.sleep(max(min(next_sweep, next_disk) - now, _TICK_FLOOR_S))
            now = time.monotonic()
            try:
                if now >= next_disk:
                    # 先排下一轮再执行:本轮抛错也按整周期退避,不刷屏重试
                    next_disk = now + DISK_CHECK_INTERVAL_S
                    await self._check_disk_watermark()
                if now >= next_sweep:
                    next_sweep = now + interval_s
                    active = self._queue.active_job_ids()
                    await asyncio.to_thread(run_sweep_once, self._settings, active)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单周期故障不拖垮长跑任务
                logger.exception("清扫周期执行异常,下一周期继续")

    async def _check_disk_watermark(self) -> None:
        """水位检查经线程执行(disk_usage + 紧急清理均为同步重活)。"""
        active = self._queue.active_job_ids()
        await asyncio.to_thread(_run_watermark_check, self._settings, active)
