"""MAPDL 批处理执行器 — ansys221 子进程生命周期、超时/取消、输出诊断。

一切真 MAPDL 运行都经由本模块(队列单并发串行调用,单许可约束);本模块:
    run_mapdl(inp_path, job_dir, ctx, job_id) -> dict   启动/轮询/善后
    cancel_job(job_id) -> bool                          杀运行中进程组(queue 防御性调用)
    interactive_available() -> bool                      PyMAPDL 可选(阶段 2 接 gRPC)

诊断顺序:许可错误(LICENSE_UNAVAILABLE)→ MAPDL ERROR 行/非零退出
(CONVERGENCE_FAILED,附 job.out 关键行);required_outputs 非空时缺件 →
INTERNAL(默认 None 跳过,直通通道由内核按 declared_outputs 自行判定)。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from .registry import KernelError
from .results import OUT_FILENAME, extract_error_lines
from .schemas import RunContext

# -j 作业名:MAPDL 各中间文件(.esav/.full 等)以此为前缀;job_id 含 '-'/'_' 不适用,固定短名
JOB_NAME = "hipjob"

# 启动器 stdout/stderr 落盘(许可/环境类故障常先于 job.out 出现,此处留证据)
LAUNCHER_LOG_FILENAME = "launcher.log"

# 子进程轮询间隔与 killpg 宽限(SIGTERM → 宽限 → SIGKILL)
POLL_INTERVAL_S = 0.2
KILL_GRACE_S = 5.0

# job.out 中的许可失败特征(大小写不敏感匹配;命中即 LICENSE_UNAVAILABLE)
LICENSE_ERROR_PATTERNS: tuple[str, ...] = (
    "LICENSE MANAGER ERROR",
    "LICENSING ERROR",
    "LICENSE CHECKOUT",
    "FLEXIBLE LICENSE",
    "FLEXLM",
    "ANSYSLMD",
    "CHECKOUT FAILED",
)

_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[str, subprocess.Popen] = {}
_CANCELLED: set[str] = set()


def interactive_available() -> bool:
    """PyMAPDL 交互模式可用性(阶段 1 以 -b 子进程为主,恒以 import 探测为准)。

    TODO(阶段 2):接 gRPC 长连接实例(ansys.mapdl.core.launch_mapdl),
    在 /health 中区分批处理可用与交互可用两级状态。
    """
    try:
        import ansys.mapdl.core  # noqa: F401 — 仅探测可选依赖
    except ImportError:
        return False
    return True


def cancel_job(job_id: str) -> bool:
    """终止运行中的 MAPDL 作业进程组;返回是否确有进程被杀。

    queue 在用户取消/服务关停时防御性调用;job 未运行时返回 False(幂等)。
    """
    with _ACTIVE_LOCK:
        process = _ACTIVE.get(job_id)
        if process is None or process.poll() is not None:
            _ACTIVE.pop(job_id, None)
            return False
        _CANCELLED.add(job_id)
    _terminate_group(process)
    return True


def run_mapdl(
    inp_path: Path,
    job_dir: Path,
    ctx: RunContext,
    job_id: str,
    required_outputs: Sequence[str] | None = None,
) -> dict:
    """以批处理模式执行 inp:轮询等待,超时/取消 killpg,输出诊断。

    成功返回 {"returncode", "elapsed_s", "out_path"};失败抛 KernelError:
        MAPDL_NOT_FOUND / TIMEOUT / LICENSE_UNAVAILABLE /
        CONVERGENCE_FAILED(附 job.out 错误行) / INTERNAL

    required_outputs:正常结束后必须存在于 job_dir 的文件名清单(缺任一 → INTERNAL);
    默认 None = 跳过该检查(直通通道由内核按 declared_outputs 自行判定产出,
    类型化方法的 summary.csv 契约已随方法库移除)。
    """
    bin_path = Path(ctx.ansys_bin)
    if not bin_path.is_file():
        raise KernelError("MAPDL_NOT_FOUND", f"ansys 可执行文件不存在: {ctx.ansys_bin}")
    inp_path, job_dir = Path(inp_path), Path(job_dir)
    if not inp_path.is_file():
        raise KernelError("INTERNAL", f"待执行的 inp 文件不存在: {inp_path}")

    command = [
        str(bin_path),
        "-b",
        "-np", str(int(ctx.ansys_np)),
        "-j", JOB_NAME,
        "-i", str(inp_path),
        "-o", OUT_FILENAME,
    ]
    env = dict(os.environ)
    if ctx.license_file:
        env["ANSYSLMD_LICENSE_FILE"] = ctx.license_file

    started = time.monotonic()
    with open(job_dir / LAUNCHER_LOG_FILENAME, "wb") as launcher_log:
        process = subprocess.Popen(  # noqa: S603 - 命令各字段均来自受控配置
            command,
            cwd=str(job_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=launcher_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 独立进程组 → killpg 可整组终止
        )

    with _ACTIVE_LOCK:
        _ACTIVE[job_id] = process
    try:
        _wait_or_kill(process, job_id, float(ctx.job_timeout_s))
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(job_id, None)

    elapsed_s = time.monotonic() - started
    out_text = _read_text(job_dir / OUT_FILENAME)

    license_line = _match_license_error(out_text)
    if license_line is not None:
        raise KernelError(
            "LICENSE_UNAVAILABLE",
            f"MAPDL 许可不可用(job.out 关键行): {license_line}",
        )
    if job_id in _CANCELLED:
        _CANCELLED.discard(job_id)
        raise KernelError("TIMEOUT", f"MAPDL 作业被外部终止(取消或 kill,job_id={job_id})")

    error_lines = extract_error_lines(out_text)
    if process.returncode != 0 or error_lines:
        detail = "\n".join(error_lines) if error_lines else f"退出码 {process.returncode}"
        raise KernelError(
            "CONVERGENCE_FAILED",
            f"MAPDL 求解失败(退出码 {process.returncode}),job.out 关键行:\n{detail}",
        )
    if required_outputs is not None:
        missing = [name for name in required_outputs if not (job_dir / name).is_file()]
        if missing:
            raise KernelError(
                "INTERNAL",
                f"MAPDL 正常结束但未产出 {', '.join(missing)}"
                "(检查模板结果写出与求解是否真正执行)",
            )
    return {
        "returncode": process.returncode,
        "elapsed_s": round(elapsed_s, 3),
        "out_path": str(job_dir / OUT_FILENAME),
    }


def _wait_or_kill(process: subprocess.Popen, job_id: str, timeout_s: float) -> None:
    """轮询等待子进程;超时则 killpg 并抛 TIMEOUT。"""
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while process.poll() is None:
        if time.monotonic() >= deadline:
            with _ACTIVE_LOCK:
                _CANCELLED.discard(job_id)  # 超时不是取消,防串扰
            _terminate_group(process)
            raise KernelError(
                "TIMEOUT",
                f"MAPDL 作业超时(>{timeout_s:.0f}s)已终止进程组",
            )
        time.sleep(POLL_INTERVAL_S)


def _terminate_group(process: subprocess.Popen) -> None:
    """SIGTERM 进程组 → 宽限 → SIGKILL 兜底;进程已消失时静默。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    process.wait(timeout=KILL_GRACE_S)


def _read_text(path: Path) -> str:
    """读文本文件;缺失/编码异常返回空串(诊断降级而非失败)。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _match_license_error(out_text: str) -> str | None:
    """在 job.out 中找首条许可失败特征行;无则 None。"""
    for raw in out_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        for pattern in LICENSE_ERROR_PATTERNS:
            if pattern in upper:
                return line
    return None
