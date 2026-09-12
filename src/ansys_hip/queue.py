"""asyncio 作业队列 — 状态机 pending→running→succeeded/failed/cancelled.

- 并发度 = settings.queue.max_concurrent(默认 1,单许可),每并发一个工作协程;
- 同步内核经 asyncio.to_thread 在线程池执行,超过 job_timeout_s → TIMEOUT;
- 作业目录 var/jobs/<id>/:state.json(JobState 除 stages 外全字段+resolved_params;
  stages 不落盘,一律读时投影 progress.csv)、
  resolved-params.json、result.json(成功后)、artifacts/(内核工件)、job.log(状态事件);
- 异常映射:KernelError→其 code/message;超时→TIMEOUT;其他 Exception→INTERNAL;
- 取消:pending 直接置 cancelled;running 先防御性调用 ansys_hip.runner.cancel_job
  (懒加载,runner 未就绪时跳过)再放弃等待(内核线程随 MAPDL 终止自行退出);
- 启动时:上次进程遗留的 pending/running 作业标记为 failed,并清扫超过保留期的目录。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import secrets
import shutil
import traceback
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .registry import Executor, KernelError, MethodSpec
from .results import parse_progress_csv
from .schemas import ErrorBody, Fidelity, JobState, JobStatusEnum, RunContext
from .settings import Settings

logger = logging.getLogger(__name__)

JOB_ID_TOKEN_BYTES = 8                      # token_urlsafe(8) → 11 字符作业 ID
STATE_FILENAME = "state.json"
RESOLVED_PARAMS_FILENAME = "resolved-params.json"
RESULT_FILENAME = "result.json"
LOG_FILENAME = "job.log"
ARTIFACTS_DIRNAME = "artifacts"
TERMINAL_STATUS_VALUES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({JobStatusEnum.PENDING, JobStatusEnum.RUNNING})
# 裸目录名守卫:拒空串/路径分隔/当前与父目录(路由正则已挡 '/',此处纵深防御,
# 防 jobs_root / "" 等拼接退化为 jobs 根自身)
ILLEGAL_DIR_NAMES = frozenset({"", "/", "\\", ".", ".."})


@dataclass(frozen=True)
class JobRecord:
    """内存中的作业快照(不可变;状态迁移用 dataclasses.replace 生成新实例)。"""

    id: str
    method: str
    part: str | None
    status: JobStatusEnum
    fidelity: Fidelity
    created_at: str
    job_dir: Path
    params: BaseModel
    executor: Executor
    started_at: str | None = None
    finished_at: str | None = None
    error: ErrorBody | None = None


@dataclass(frozen=True)
class JobSnapshot:
    """API 层作业视图:活跃作业实时投影;历史作业由 state.json 只读重建
    (服务重启后仍可见)。历史作业无从(也不应)重建 params/executor,
    故与 JobRecord 平行提供只读视图,不带执行语义 — id/status/error 一律
    经 state 取得,仅 job_dir(JobState 不携带)随快照携带。"""

    job_dir: Path
    state: JobState


class JobQueue:
    """单实例作业队列(生命周期由 FastAPI lifespan 管理:start/stop)。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jobs_root = settings.jobs_root
        self._jobs: dict[str, JobRecord] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """恢复遗留作业并启动 max_concurrent 个工作协程。

        清扫(按天/配额/水位的超期删除)归 StorageSweeper(sweeper.py),
        启动时由其即刻轮承接旧版启动清扫语义。
        """
        self._stopping = False
        _recover_jobs(self._jobs_root)
        worker_count = max(1, self._settings.queue.max_concurrent)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"hip-queue-worker-{index}")
            for index in range(worker_count)
        ]
        logger.info("作业队列已启动(%d 并发,根目录 %s)", worker_count, self._jobs_root)

    async def stop(self) -> None:
        """停止全部工作协程与运行中的执行任务(防御性终止内核)。

        先置 _stopping:工作协程收到 CancelledError 时凭它区分『关机』(必须
        退出,否则取消信号会被误判为客户端取消而被吞掉,协程回到 queue.get()
        永久挂起)与『客户端取消单个作业』(跳过该作业继续消费)。
        """
        self._stopping = True
        for job_id, task in list(self._running.items()):
            self._runner_cancel(job_id)
            task.cancel()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, *self._running.values(), return_exceptions=True)
        self._workers = []
        self._running = {}
        logger.info("作业队列已停止")

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------

    def active_job_ids(self) -> frozenset[str]:
        """内存中 pending/running 的作业 ID 快照。

        须在事件循环线程调用:StorageSweeper 每 tick 现取快照后再放线程执行,
        避免跨线程遍历可变 dict。
        """
        return frozenset(
            job_id
            for job_id, record in self._jobs.items()
            if record.status in ACTIVE_STATUSES
        )

    def submit(
        self,
        *,
        spec: MethodSpec,
        params: BaseModel,
        part: str | None,
        executor: Executor,
    ) -> JobRecord:
        """受理作业:建目录、落盘初始状态与解析后参数、入队。"""
        job_id = secrets.token_urlsafe(JOB_ID_TOKEN_BYTES)
        job_dir = self._jobs_root / job_id
        (job_dir / ARTIFACTS_DIRNAME).mkdir(parents=True, exist_ok=True)
        record = JobRecord(
            id=job_id,
            method=spec.name,
            part=part,
            status=JobStatusEnum.PENDING,
            fidelity=Fidelity(spec.fidelity),
            created_at=_now_iso(),
            job_dir=job_dir,
            params=params,
            executor=executor,
        )
        self._jobs[job_id] = record
        _write_json(job_dir / RESOLVED_PARAMS_FILENAME, params.model_dump(mode="json"))
        self._persist(record)
        _append_log(job_dir, f"作业受理 method={spec.name} part={part or '-'}")
        self._queue.put_nowait(job_id)
        return record

    def state(self, record: JobRecord, *, include_stages: bool = True) -> JobState:
        """JobState 快照(含标准子资源 URL)。

        stages 为读时投影:GET 组装响应时解析 job_dir 根的 progress.csv 侧车,
        无后台轮询协程、无状态迁移 — 文件不存在(尚无侧车)自然得 None;
        终态后文件在盘上保留,快照即末帧。include_stages=False 供不需要
        stages 的端点(日志/工件/结果/删除)跳过这次读盘与逐行解析。
        """
        return JobState(
            id=record.id,
            method=record.method,
            part=record.part,
            status=record.status,
            fidelity=record.fidelity,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error=record.error,
            log_url=f"/jobs/{record.id}/log",
            result_url=f"/jobs/{record.id}/result",
            artifacts_url=f"/jobs/{record.id}/artifacts",
            stages=parse_progress_csv(record.job_dir) if include_stages else None,
        )

    def snapshot(self, job_id: str, *, include_stages: bool = True) -> JobSnapshot | None:
        """按 ID 取作业快照:内存命中 → 实时投影;未命中 → 盘上历史只读重建。"""
        record = self._jobs.get(job_id)
        if record is not None:
            return JobSnapshot(
                job_dir=record.job_dir,
                state=self.state(record, include_stages=include_stages),
            )
        return self._historic_snapshot(job_id, include_stages=include_stages)

    def _historic_snapshot(
        self, job_id: str, *, include_stages: bool = True
    ) -> JobSnapshot | None:
        """盘上历史作业的只读视图(state.json 末帧);目录不存在/损坏 → None。

        stages 与活跃路径同源:读时投影 job_dir 根的 progress.csv(旧档里
        历史遗留的 stages 键被投影值覆盖,单一事实源)。
        """
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            return None
        state_path = job_dir / STATE_FILENAME
        if not state_path.is_file():
            return None
        payload = load_state_payload(state_path)
        if payload is None:
            return None
        try:
            state = JobState.model_validate({
                **payload,
                "stages": parse_progress_csv(job_dir) if include_stages else None,
            })
        except ValidationError:
            logger.warning("历史作业 %s 的 %s 不合法,已跳过", job_id, STATE_FILENAME)
            return None
        return JobSnapshot(job_dir=job_dir, state=state)

    def _job_dir(self, job_id: str) -> Path | None:
        """受统一守卫的作业目录路径;裸目录名非法 → None。

        守卫 ''/'/'/'\\'/'.'/'..':防 jobs_root / "" 等拼接退化为
        jobs 根自身(snapshot 读、remove 删、runner 终止共用此入口)。
        """
        if job_id in ILLEGAL_DIR_NAMES:
            return None
        return self._jobs_root / job_id

    def list_jobs(self) -> list[JobState]:
        """全部已知作业的 JobState:内存活跃(实时,含 stages 投影)∪
        盘上有 state.json 的历史目录(只读末帧),按 (created_at, id) 倒序
        (受理时间新者在前;created_at 秒级精度,同秒以 id 消除不确定性)。

        submit() 先注册内存再落盘,内存 ⊇ 全部活跃作业,无竞态窗口;
        坏 state.json 目录经 snapshot 返回 None 自然跳过(不 500)。
        """
        ids = set(self._jobs)
        if self._jobs_root.is_dir():
            ids.update(
                state_path.parent.name
                for state_path in self._jobs_root.glob(f"*/{STATE_FILENAME}")
            )
        states = [
            snapshot.state
            for job_id in ids
            if (snapshot := self.snapshot(job_id)) is not None
        ]
        # 终序唯一由本行决定;上文集合迭代顺序无关紧要
        states.sort(key=lambda state: (state.created_at, state.id), reverse=True)
        return states

    def counts(self) -> tuple[int, int]:
        """队列计数 (running, pending) — /health 用。"""
        statuses = [record.status for record in self._jobs.values()]
        return statuses.count(JobStatusEnum.RUNNING), statuses.count(JobStatusEnum.PENDING)

    def cancel(self, job_id: str) -> bool:
        """取消作业:pending 直接置 cancelled;running 先终止内核再取消执行任务。"""
        record = self._jobs.get(job_id)
        if record is None or record.status not in ACTIVE_STATUSES:
            return False
        if record.status is JobStatusEnum.RUNNING:
            _append_log(record.job_dir, "客户端请求取消 → 防御性终止内核")
            self._runner_cancel(job_id)
            exec_task = self._running.get(job_id)
            if exec_task is not None:
                exec_task.cancel()
        self._transition(record, status=JobStatusEnum.CANCELLED, finished_at=_now_iso())
        return True

    def remove(self, job_id: str) -> bool:
        """删除作业记录与目录(DELETE /jobs/{id});无论内存与否都清目录。

        已知 = 内存在册 或 盘上 state.json 存在(历史作业删除成为真清理);
        目录路径经 _job_dir 统一守卫,非法名不误删 jobs 根。
        """
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            return False
        known_in_memory = self._jobs.pop(job_id, None) is not None
        known_on_disk = (job_dir / STATE_FILENAME).is_file()
        shutil.rmtree(job_dir, ignore_errors=True)
        return known_in_memory or known_on_disk

    # ------------------------------------------------------------------
    # 工作协程
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """消费队列;单个作业的任何异常都不拖垮工作协程。"""
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            except Exception:  # noqa: BLE001 — 隔离单作业故障
                logger.exception("处理作业 %s 时发生未预期异常", job_id)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: str) -> None:
        """取出 pending 作业并执行;已取消/删除的跳过。"""
        record = self._jobs.get(job_id)
        if record is None or record.status is not JobStatusEnum.PENDING:
            return
        exec_task = asyncio.create_task(self._execute(record), name=f"hip-job-{job_id}")
        self._running[job_id] = exec_task
        try:
            await exec_task
        except asyncio.CancelledError:
            if not self._stopping and exec_task.cancelled():
                _append_log(record.job_dir, "作业已取消,放弃等待内核线程退出")
                return  # 仅该作业被取消:工作协程继续消费下一个
            raise  # 关机:取消信号向上传播,工作协程随之退出
        finally:
            self._running.pop(job_id, None)

    async def _execute(self, record: JobRecord) -> None:
        """执行单个作业:状态迁移、超时控制、异常映射、结果落盘。"""
        ctx = self._build_context(record)
        record = self._transition(
            record, status=JobStatusEnum.RUNNING, started_at=_now_iso()
        )
        _append_log(
            record.job_dir,
            f"内核启动 method={record.method} timeout_s={ctx.job_timeout_s}",
        )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(record.executor, record.params, ctx),
                timeout=ctx.job_timeout_s,
            )
        except asyncio.CancelledError:
            raise  # 外部取消(cancel/关机);状态已由取消方标记
        except (TimeoutError, FuturesTimeoutError):
            _append_log(record.job_dir, f"作业超时(>{ctx.job_timeout_s}s)")
            self._runner_cancel(record.id)
            self._fail(
                record,
                ErrorBody(
                    code="TIMEOUT",
                    message=f"作业超过 {ctx.job_timeout_s}s 未完成,已防御性终止 MAPDL 进程",
                ),
            )
        except KernelError as exc:
            _append_log(record.job_dir, f"内核主动失败 [{exc.code}] {exc.message}")
            self._fail(record, ErrorBody(code=exc.code, message=exc.message))
        except Exception as exc:  # noqa: BLE001 — 内核任意异常统一映射 INTERNAL
            _append_log(record.job_dir, "内核未捕获异常:\n" + traceback.format_exc())
            self._fail(record, ErrorBody(code="INTERNAL", message=_exception_summary(exc)))
        else:
            self._succeed(record, result)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _build_context(self, record: JobRecord) -> RunContext:
        """构造传给内核的不可变执行上下文。"""
        ansys = self._settings.ansys
        return RunContext(
            job_dir=record.job_dir,
            ansys_bin=ansys.bin,
            license_file=ansys.license_file,
            ansys_np=ansys.np,
            job_timeout_s=ansys.job_timeout_s,
        )

    def _transition(
        self,
        record: JobRecord,
        *,
        status: JobStatusEnum,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: ErrorBody | None = None,
    ) -> JobRecord:
        """生成新状态并落盘;作业已被删除(记录不一致)时只返回原记录。"""
        if self._jobs.get(record.id) is not record:
            return record
        updated = dataclasses.replace(
            record,
            status=status,
            started_at=started_at or record.started_at,
            finished_at=finished_at or record.finished_at,
            error=error,
        )
        self._jobs[record.id] = updated
        _append_log(record.job_dir, _transition_message(record.status, status, error))
        self._persist(updated)
        return updated

    def _fail(self, record: JobRecord, error: ErrorBody) -> None:
        """置为 failed 并记录错误体。"""
        self._transition(
            record, status=JobStatusEnum.FAILED, finished_at=_now_iso(), error=error
        )

    def _succeed(self, record: JobRecord, result: dict[str, Any]) -> None:
        """结果落盘后置为 succeeded(客户端看到 succeeded 时 result.json 必已存在)。"""
        payload = _ensure_fidelity(result, record.fidelity)
        _write_json(record.job_dir / RESULT_FILENAME, payload)
        self._transition(record, status=JobStatusEnum.SUCCEEDED, finished_at=_now_iso())

    def _persist(self, record: JobRecord) -> None:
        """state.json = JobState 除 stages 外全字段 + resolved_params(保证可追溯)。

        stages 不归档:progress.csv 是唯一事实源(活跃与历史路径统一读时
        投影),省去每次状态迁移附带的一次 CSV 解析。
        """
        payload = {
            **self.state(record, include_stages=False).model_dump(mode="json"),
            "resolved_params": record.params.model_dump(mode="json"),
        }
        _write_json(record.job_dir / STATE_FILENAME, payload)

    def _runner_cancel(self, job_id: str) -> None:
        """防御性终止 MAPDL 进程(懒加载 runner.cancel_job,未就绪时跳过)。"""
        job_dir = self._job_dir(job_id)
        if job_dir is None:
            return
        try:
            from .runner import cancel_job  # noqa: PLC0415 — 懒加载,runner 未就绪不阻断
        except ImportError:
            _append_log(job_dir, "runner 未就绪,跳过 MAPDL 防御性终止(仅放弃等待)")
            return
        try:
            cancel_job(job_id)
        except Exception as exc:  # noqa: BLE001 — 终止失败不阻断取消流程
            logger.warning("防御性终止作业 %s 失败: %r", job_id, exc)
            _append_log(job_dir, f"防御性终止失败: {exc!r}")


# ---------------------------------------------------------------------------
# 模块级纯工具
# ---------------------------------------------------------------------------

def _recover_jobs(jobs_root: Path) -> None:
    """启动恢复:上次进程遗留的 pending/running 作业 → failed(INTERNAL:服务重启中断)。

    超期删除已归 sweeper(按天/配额/水位三路,见 sweeper.py);孤儿目录
    (无/坏 state.json)同样由 sweeper 的 mtime 兜底处理。
    """
    if not jobs_root.is_dir():
        return
    for job_dir in sorted(path for path in jobs_root.iterdir() if path.is_dir()):
        payload = load_state_payload(job_dir / STATE_FILENAME)
        if payload is None:
            continue
        status = payload.get("status")
        if status in {"pending", "running"}:
            _write_json(job_dir / STATE_FILENAME, {
                **payload,
                "status": "failed",
                "finished_at": _now_iso(),
                "error": {"code": "INTERNAL", "message": "服务重启导致作业中断,请重新提交"},
            })
            _append_log(job_dir, "服务重启,遗留作业标记为 failed")
            logger.info("遗留作业 %s 已标记为 failed(服务重启中断)", job_dir.name)


def load_state_payload(state_path: Path) -> dict[str, Any] | None:
    """读 state.json;不存在/损坏/非对象一律返回 None。"""
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _ensure_fidelity(result: dict[str, Any], fallback: Fidelity) -> dict[str, Any]:
    """内核返回 dict 必含 fidelity(契约);缺失时补方法的标称保真度。"""
    if "fidelity" in result:
        return result
    return {**result, "fidelity": fallback.value}


def _exception_summary(exc: Exception) -> str:
    """INTERNAL 错误消息:异常摘要 + traceback 首帧位置(完整堆栈在 job.log)。"""
    tb = exc.__traceback__
    frame = (
        f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb is not None else "无堆栈"
    )
    return f"{type(exc).__name__}: {exc} @ {frame}"


def _transition_message(old: JobStatusEnum, new: JobStatusEnum, error: ErrorBody | None) -> str:
    """job.log 中的状态事件行。"""
    line = f"状态变更 {old.value} → {new.value}"
    if error is not None:
        line += f" [{error.code}] {error.message}"
    return line


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串(秒级)。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    """解析 ISO 时间串;无时区按 UTC 处理,非法返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _append_log(job_dir: Path, line: str) -> None:
    """向 job.log 追加一行带时间戳的事件(目录已被清理时告警不抛出)。"""
    try:
        with (job_dir / LOG_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(f"[{_now_iso()}] {line}\n")
    except OSError:
        logger.warning("写入作业日志失败(目录可能已清理): %s", job_dir / LOG_FILENAME)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """JSON 落盘(ensure_ascii=False,缩进 2);失败告警不抛出,不中断状态机。"""
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("写入 JSON 失败(目录可能已清理): %s", path)
