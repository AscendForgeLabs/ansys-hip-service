"""logtail 尾读测试 — 反向 seek 只读末 N 行,不整读大文件.

语义对齐"整读后 splitlines(keepends)[-N:]"(\\n 行界文件):
保留原始行尾、末行无换行也计一行、N 超过行数返回全文、缺文件/读失败 → 空串。
"""

from __future__ import annotations

from pathlib import Path

from ansys_hip import logtail as logtail_module
from ansys_hip.logtail import _TAIL_CHUNK_BYTES, read_text_tail


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "nope.log", None) == ""
    assert read_tail(tmp_path / "nope.log", 5) == ""


def test_tail_none_returns_whole_file(tmp_path: Path) -> None:
    target = tmp_path / "whole.log"
    target.write_text("一\n二\n三\n", encoding="utf-8")
    assert read_tail(target, None) == "一\n二\n三\n"


def test_tail_keeps_endings_and_counts_unterminated_last_line(
    tmp_path: Path,
) -> None:
    """末行无换行符同样计一行;行尾原样保留。"""
    target = tmp_path / "partial.log"
    target.write_text("l1\nl2\nl3", encoding="utf-8")
    assert read_tail(target, 1) == "l3"
    assert read_tail(target, 2) == "l2\nl3"
    assert read_tail(target, 3) == "l1\nl2\nl3"


def test_tail_exceeding_line_count_returns_all(tmp_path: Path) -> None:
    target = tmp_path / "few.log"
    target.write_text("a\nb\n", encoding="utf-8")
    assert read_tail(target, 99) == "a\nb\n"


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "empty.log"
    target.write_text("", encoding="utf-8")
    assert read_tail(target, 10) == ""


def test_matches_whole_file_slice_semantics(tmp_path: Path) -> None:
    """钉测:与整读切片逐 N 对齐(行为等价的回归护栏)。"""
    body = "".join(f"row-{i:02d}\n" for i in range(37))
    target = tmp_path / "rows.log"
    target.write_text(body, encoding="utf-8")
    for n in (1, 2, 5, 36, 37, 40):
        expected = "".join(body.splitlines(keepends=True)[-n:])
        assert read_tail(target, n) == expected, n


def test_reverse_seek_across_chunks(tmp_path: Path) -> None:
    """跨块回读:文件数倍于块大小,块边界落在行中间仍只回末 N 行。"""
    line = "x" * 1000 + "\n"  # 1KB/行
    total_lines = (_TAIL_CHUNK_BYTES // 1001) * 8 + 13  # 强制跨多个 64KB 块
    target = tmp_path / "big.log"
    target.write_text(line * total_lines, encoding="utf-8")
    assert read_tail(target, 3) == line * 3
    assert read_tail(target, 1) == line
    assert read_tail(target, total_lines) == line * total_lines


def test_multibyte_cut_at_chunk_boundary(tmp_path: Path, monkeypatch) -> None:
    """块边界切在多字节 UTF-8 字符中间:段首残行被丢弃,返回值不受污染。"""
    monkeypatch.setattr(logtail_module, "_TAIL_CHUNK_BYTES", 5)  # 3 字节/汉字,必切中
    target = tmp_path / "utf8.log"
    target.write_text("啊啊啊啊\n嗯嗯\n", encoding="utf-8")
    assert read_tail(target, 1) == "嗯嗯\n"
    assert read_tail(target, 2) == "啊啊啊啊\n嗯嗯\n"


def test_directory_target_returns_empty(tmp_path: Path) -> None:
    """读失败(目标是目录)→ 空串,不抛(与缺文件同口径)。"""
    assert read_tail(tmp_path, 3) == ""
    assert read_tail(tmp_path, None) == ""
