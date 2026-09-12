"""尾部读取 — 反向 seek 只读末 N 行,不整读大文件.

服务两类日志端点(GET /jobs/{id}/log 与 GET /service/log)共用:tail 给定时
只做 O(尾部) IO 而非整个文件读进内存再切片(access.log 按天轮转单文件可达
上百 MB;MAPDL 大模型 job.out 同理)。缺省全文经 read_log_view 防过长:
超 LOG_FULL_MAX_BYTES 自动截尾 DEFAULT_TAIL_LINES 行并标记截尾。

行界按字节 b"\\n" 判定(服务自身产出的两类日志均为 \\n 行界;含 \\r\\n 的
文件返回值保留原始行尾)。与整读后 splitlines(keepends)[-N:] 在 \\n 行界
文件上逐字等价(测试钉住);\\f/\\u2028 等罕见边界不在此列。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

# 反向回读的块大小(64KB:一次系统调用的开销与尾部覆盖率的平衡)
_TAIL_CHUNK_BYTES = 64 * 1024

# 缺省"全文"的字节阈值与回退行数:NERR 失控作业的 job.out 可达数百 MB,
# 无界回传会打死浏览器/网络层(渲染层 2000 行截断来不及生效);超阈值自动
# 截尾(2000 与面板 MAX_RENDER_LINES 对齐 — 更多行面板本来也只渲染末 2000)
LOG_FULL_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TAIL_LINES = 2000


def read_log_view(path: Path, tail: int | None) -> tuple[str, bool]:
    """日志端点共用的读取口径:tail 给定 → 精确尾读;缺省全文但文件超过
    LOG_FULL_MAX_BYTES → 回退末 DEFAULT_TAIL_LINES 行。返回 (文本, 是否截尾)。"""
    if tail is not None:
        return read_tail(path, tail), False
    try:
        oversized = path.stat().st_size > LOG_FULL_MAX_BYTES
    except OSError:
        oversized = False
    if not oversized:
        return read_tail(path, None), False
    return read_tail(path, DEFAULT_TAIL_LINES), True


def read_tail(path: Path, tail: int | None) -> str:
    """读文件文本;tail 为 None → 全文;缺文件/读失败 → 空串(不抛)。

    tail 给定时自末尾按块回读,凑足 tail+1 个换行即止 — 末行无换行符
    同样计一行;返回保留原始行尾(keepends 语义)。
    """
    if not path.is_file():
        return ""
    if tail is None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            _warn_read_failure(path)
            return ""
    try:
        with path.open("rb") as handle:
            data = _tail_bytes(handle, tail)
    except OSError:
        _warn_read_failure(path)
        return ""
    return data.decode("utf-8", errors="replace")


def _warn_read_failure(path: Path) -> None:
    """与"文件不存在(空串)"区分留痕:存在但读失败(权限/I/O/轮转竞态)须可事后定位。"""
    logger.warning("读取日志失败: %s", path)


def _tail_bytes(handle: BinaryIO, tail: int) -> bytes:
    """自文件末尾按块回读,返回足以切出末 tail 行的连续字节段。"""
    handle.seek(0, os.SEEK_END)
    remaining = handle.tell()
    newline_count = 0
    chunks: list[bytes] = []
    # 回读到换行数严格超过 tail 为止:保证段首被截断的残行必然被丢弃
    while remaining > 0 and newline_count <= tail:
        size = min(_TAIL_CHUNK_BYTES, remaining)
        remaining -= size
        handle.seek(remaining)
        chunks.append(handle.read(size))
        newline_count += chunks[-1].count(b"\n")
    return _last_lines(b"".join(reversed(chunks)), tail)


def _last_lines(data: bytes, tail: int) -> bytes:
    """从字节段切出末 tail 行(保留行尾)。

    以 \\n 切分后,末段为空串(段以 \\n 结尾)不计行;末段非空(末行无
    换行)计一行;[-tail:] 丢弃段首残行(其可能起于行/字符中间)。
    """
    segments = data.split(b"\n")
    lines = [segment + b"\n" for segment in segments[:-1]]
    if segments[-1]:
        lines.append(segments[-1])
    return b"".join(lines[-tail:])
