#!/usr/bin/env python3
"""cdb → 回放工件转换器:把存量作业的 MAPDL cdb + 节点表桥接成 hip-playback 组件工件.

输入(由 ``*CDWRITE`` 写出的归档 cdb 与 ``PRNSOL`` 节点表桥接 csv):
    - cdb:解析 ETBLOCK(TYPE 序号 → 单元名)/ NBLOCK(SOLID 节点定宽黏连格式)/
      EBLOCK((19i10) token 流,SOLID187 记录 21 字段跨 19+2 两行)三块,其余行跳过;
    - nodes csv:表头截断为 "node x y",数据行 7 列空白分隔,首列为占位星号
      (节点号丢失),后 6 列为 E16.8 token(x/y/z/ux/uy/uz);行序 = NBLOCK 节点序。

输出(hip-playback 组件契约,覆盖同名文件写入 --out 目录):
    - frame_1.csv:表头 ``node,x,y,z,ux,uy,uz``;NBLOCK 真节点号 + 原 E16.8 token
      逐字符透传(零精度损失,组件 Number() 容忍前导空格);
    - emap.csv:表头 ``elem,n1..n10``(11 列 tet10 单一宽度),仅固体单元;
    - epart.csv:表头 ``elem,part``;part = TYPE 序号(不用 MAT:MAT 会把接触单元
      混进部件)。

健壮性约定(全部显式中文失败,不静默):
    - NBLOCK 行长 69(z 字段整段省略)兜 0.0;负坐标与 E 字段黏连按定宽切开;
    - EBLOCK 是 token 流不是行:续行不足 190 字符时右侧绝不补 0
      (补 0 会注入幻影 token 全毁);
    - 头部计数(NBLOCK/EBLOCK/ETBLOCK)与实际解析数对账;
    - 固体过滤按 ET 名(默认 187)不按 nn 猜;固体单元节点数必须一致(emap 单一
      宽度契约),节点须互异正数且 ∈ NBLOCK;
    - 节点表行数与 NBLOCK 计数对账,全行坐标断言(超差报行号 + 两侧值)。

摘要体检(非致命,警告输出):接触锚定 —— ET 名 ∈ {TARGE170, CONTA174} 的接触
单元去重节点集应恰等于某固体部件的一个边界面节点集(边界面 = 角点升序键在全模型
恰出现一次的面),失配打警告。

用法::

    python scripts/convert_cdb_frames.py \\
        --cdb job.cdb --nodes nodes.csv --out artifacts/ [--etypes 187] [--coord-tol 1e-3]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# 常量(MAPDL cdb 定宽格式与 hip-playback 组件契约)
# ---------------------------------------------------------------------------

ET_CHUNK_WIDTH = 9           # ETBLOCK (2i9,19a9):[0:9] 序号、[9:18] 单元名
NBLOCK_INT_WIDTH = 9         # NBLOCK (3i9,6e21.13e3):3 个 i9 字段
NBLOCK_FLOAT_WIDTH = 21      # 坐标 E 字段宽 21(e21.13e3)
NBLOCK_FLOAT_OFFSET = NBLOCK_INT_WIDTH * 3  # 坐标区起点(x 从第 28 列起)
NBLOCK_FLOAT_COUNT = 3       # x/y/z
EBLOCK_FIELD_WIDTH = 10      # EBLOCK (19i10):token 宽 10
EBLOCK_LINE_CHARS = EBLOCK_FIELD_WIDTH * 19  # 整行 190 字符;短行绝不补 0
TET10_NODE_COUNT = 10        # emap 单一宽度契约: tet10
DEFAULT_ETYPES = "187"
DEFAULT_COORD_TOL = 1e-3

CONTACT_ETYPE_NAMES = frozenset({"TARGE170", "CONTA174"})
CONTACT_ETYPE_TRAILING_DIGITS = frozenset({"170", "174"})


def _is_contact_etype(name: str) -> bool:
    """接触单元判定:ET 名可为全名(TARGE170/CONTA174)或裸数字(170/174)。

    实测 ``*CDWRITE`` 的 ETBLOCK 单元名字段存裸数字,部分写法存全名,按尾部
    数字归一化识别。
    """
    upper = name.strip().upper()
    digits = ""
    for ch in reversed(upper):
        if not ch.isdigit():
            break
        digits = ch + digits
    return digits in CONTACT_ETYPE_TRAILING_DIGITS

# SOLID187 节点序:1..4 角点,5..10 棱中点(mid 1-2 / 2-3 / 3-1 / 1-4 / 2-4 / 3-4)。
# 四个面:(角点下标组, 棱中点下标组)(0 基)。
TET10_FACES: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((0, 1, 2), (4, 5, 6)),
    ((0, 1, 3), (4, 8, 7)),
    ((1, 2, 3), (5, 9, 8)),
    ((0, 2, 3), (6, 7, 9)),
)


class ConversionError(RuntimeError):
    """转换显式失败(全部附中文诊断,不静默)。"""


# ---------------------------------------------------------------------------
# 数据模型(不可变)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CdbNode:
    """NBLOCK 单个节点:编号 + 三向坐标。"""

    nid: int
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class CdbElement:
    """EBLOCK 单条单元记录(节点数随单元类型可变)。"""

    eid: int
    mat: int
    type_seq: int          # TYPE 属性序号(即 part 划分依据,非 MAT)
    etype: str             # ETBLOCK 单元名(如 "187" / "CONTA174")
    nodes: tuple[int, ...]


@dataclass(frozen=True)
class Cdb:
    """parse_cdb 结果:三块解析产物。"""

    etypes: dict[int, str]     # TYPE 序号 → ET 名
    nodes: tuple[CdbNode, ...]     # NBLOCK 顺序
    elements: tuple[CdbElement, ...]  # EBLOCK 顺序


# ---------------------------------------------------------------------------
# 底层定宽解析助手
# ---------------------------------------------------------------------------

def _fixed_chunks(line: str, width: int) -> list[str]:
    """按定宽切行(先去行尾空白):短行只切实际长度,右侧绝不补字符。"""
    text = line.rstrip("\r\n \t")
    return [text[i:i + width] for i in range(0, len(text), width)]


def _is_block_end(stripped: str) -> bool:
    """块内数据行终结:裸 `-1`,或遇到命令行(字母开头,如 N,UNBL / TYPE,4)。"""
    return stripped == "-1" or (stripped != "" and stripped[0].isalpha())


def _int_chunk(chunk: str, line_no: int) -> int:
    """定宽整数字段:空白 = 0;非数字即显式失败。"""
    token = chunk.strip()
    if not token:
        return 0
    try:
        return int(token)
    except ValueError as exc:
        raise ConversionError(f"第 {line_no} 行整数段无法解析:{chunk!r}") from exc


def _float_chunk(chunk: str, line_no: int) -> float:
    """定宽 E 格式字段;非数字即显式失败。"""
    token = chunk.strip()
    if not token:
        return 0.0
    try:
        return float(token)
    except ValueError as exc:
        raise ConversionError(f"第 {line_no} 行浮点段无法解析:{chunk!r}") from exc


# ---------------------------------------------------------------------------
# 三块解析
# ---------------------------------------------------------------------------

def _parse_etblock(lines: list[str], start: int, etypes: dict[int, str]) -> int:
    """ETBLOCK 数据区:(2i9,19a9) 定宽行,`-1` 结束;返回终结行下标。"""
    i = start
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if _is_block_end(stripped):
            return i
        if stripped.startswith("(") or not stripped:
            i += 1
            continue
        chunks = _fixed_chunks(lines[i], ET_CHUNK_WIDTH)
        seq = _int_chunk(chunks[0], i + 1)
        name = chunks[1].strip() if len(chunks) > 1 else ""
        if name:
            etypes[seq] = name
        i += 1
    raise ConversionError("ETBLOCK 未找到 -1 终结行(cdb 截断)")


def _parse_nblock(lines: list[str], start: int, nodes: list[CdbNode]) -> int:
    """NBLOCK 数据区:(3i9,6e21.13e3) 定宽黏连;z 缺席(短行)兜 0.0。"""
    i = start
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if _is_block_end(stripped):
            return i
        if stripped.startswith("(") or not stripped:
            i += 1
            continue
        text = lines[i].rstrip("\r\n \t")
        nid = _int_chunk(text[:NBLOCK_INT_WIDTH], i + 1)
        coords: list[float] = []
        for k in range(NBLOCK_FLOAT_COUNT):
            begin = NBLOCK_FLOAT_OFFSET + k * NBLOCK_FLOAT_WIDTH
            end = begin + NBLOCK_FLOAT_WIDTH
            coords.append(
                _float_chunk(text[begin:end], i + 1) if len(text) >= end else 0.0
            )
        nodes.append(CdbNode(nid=nid, x=coords[0], y=coords[1], z=coords[2]))
        i += 1
    raise ConversionError("NBLOCK 未找到 -1 终结行(cdb 截断)")


def _parse_eblock(
    lines: list[str],
    start: int,
    elements: list[CdbElement],
    etypes: dict[int, str],
) -> int:
    """EBLOCK 数据区:(19i10) token 流(不是行);`-1` 结束。

    记录布局:字段 [0]=MAT [1]=TYPE [8]=nn(节点数) [10]=EID,[11:11+nn] 节点;
    SOLID187 记录 21 字段跨 19+2 两行。续行短于 190 字符时只切实际长度,
    绝不右侧补 0(补 0 会注入幻影 token 全毁)。
    """
    tokens: list[int] = []
    i = start
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if _is_block_end(stripped):
            break
        if stripped.startswith("(") or not stripped:
            i += 1
            continue
        for chunk in _fixed_chunks(lines[i], EBLOCK_FIELD_WIDTH):
            tokens.append(_int_chunk(chunk, i + 1))
        i += 1
    else:
        raise ConversionError("EBLOCK 未找到 -1 终结行(cdb 截断)")

    idx = 0
    total = len(tokens)
    while idx < total:
        if idx + 11 > total:
            raise ConversionError(
                f"EBLOCK token 流在 {total} 个 token 处截断:头部字段不足 11 个"
            )
        nn = tokens[idx + 8]
        if nn <= 0:
            raise ConversionError(f"EBLOCK 记录的节点数字段非法:{nn}(第 {idx} token 起)")
        if idx + 11 + nn > total:
            raise ConversionError(
                f"EBLOCK token 流截断:记录声称 {nn} 个节点,但只剩 {total - idx - 11} 个 token"
            )
        elements.append(
            CdbElement(
                eid=tokens[idx + 10],
                mat=tokens[idx],
                type_seq=tokens[idx + 1],
                etype=etypes.get(tokens[idx + 1], ""),
                nodes=tuple(tokens[idx + 11:idx + 11 + nn]),
            )
        )
        idx += 11 + nn
    return i


def parse_cdb(path: Path | str) -> Cdb:
    """解析 cdb:ETBLOCK / NBLOCK / EBLOCK 三块,其余行跳过;含计数对账。"""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    etypes: dict[int, str] = {}
    nodes: list[CdbNode] = []
    elements: list[CdbElement] = []
    expected = {"ETBLOCK": None, "NBLOCK": None, "EBLOCK": None}

    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("ETBLOCK"):
            expected["ETBLOCK"] = _int_chunk(stripped.split(",")[1], i + 1)
            i = _parse_etblock(lines, i + 1, etypes)
        elif stripped.startswith("NBLOCK"):
            expected["NBLOCK"] = _int_chunk(stripped.split(",")[3], i + 1)
            i = _parse_nblock(lines, i + 1, nodes)
        elif stripped.startswith("EBLOCK"):
            expected["EBLOCK"] = _int_chunk(stripped.split(",")[3], i + 1)
            i = _parse_eblock(lines, i + 1, elements, etypes)
        else:
            i += 1

    if not etypes:
        raise ConversionError("cdb 中未找到 ETBLOCK 块")
    if not nodes:
        raise ConversionError("cdb 中未找到 NBLOCK 块")
    if not elements:
        raise ConversionError("cdb 中未找到 EBLOCK 块")
    for block, actual in (
        ("ETBLOCK", len(etypes)),
        ("NBLOCK", len(nodes)),
        ("EBLOCK", len(elements)),
    ):
        want = expected[block]
        if want is not None and want != actual:
            raise ConversionError(
                f"{block} 计数对账不符:头部声明 {want},实际解析 {actual}"
            )
    return Cdb(etypes=etypes, nodes=tuple(nodes), elements=tuple(elements))


# ---------------------------------------------------------------------------
# 节点表桥接对齐
# ---------------------------------------------------------------------------

def align_nodes_csv(
    rows: Sequence[str],
    nblock: Sequence[CdbNode],
    tol: float,
) -> list[tuple[int, tuple[str, ...]]]:
    """按行序把节点表对齐到 NBLOCK:全行坐标断言,返回 (真节点号, 6 个 E16.8 token).

    节点号列在导出时丢失(全为星号占位),真节点号取自 NBLOCK 行序;超差即显式
    失败并报行号与两侧值。
    """
    if len(rows) != len(nblock):
        raise ConversionError(
            f"行数不符:节点表 {len(rows)} 行 vs NBLOCK {len(nblock)} 个节点"
        )
    aligned: list[tuple[int, tuple[str, ...]]] = []
    # 数据行 7 列:占位星号 1 + 坐标 x/y/z 3 + 位移 ux/uy/uz 3
    expected_columns = 1 + NBLOCK_FLOAT_COUNT + NBLOCK_FLOAT_COUNT
    for row_no, (row, nb) in enumerate(zip(rows, nblock), start=1):
        tokens = row.split()
        if len(tokens) != expected_columns:
            raise ConversionError(
                f"节点表数据第 {row_no} 行列数为 {len(tokens)},应为 {expected_columns} 列"
            )
        value_tokens = tuple(tokens[1:])
        csv_xyz = tuple(_float_token(t, row_no) for t in value_tokens[:NBLOCK_FLOAT_COUNT])
        for axis, got, want in zip("xyz", csv_xyz, (nb.x, nb.y, nb.z)):
            delta = abs(got - want)
            if delta > tol:
                raise ConversionError(
                    f"节点表数据第 {row_no} 行 {axis} 坐标超差:"
                    f"csv={got!r} vs cdb 节点 {nb.nid}={want!r}(差 {delta:.3g} > 容差 {tol})"
                )
        aligned.append((nb.nid, value_tokens))
    return aligned


def _float_token(token: str, row_no: int) -> float:
    """节点表 token 转 float(用于坐标断言;透传走原字符串)。"""
    try:
        return float(token)
    except ValueError as exc:
        raise ConversionError(f"节点表数据第 {row_no} 行数值段无法解析:{token!r}") from exc


# ---------------------------------------------------------------------------
# 拓扑:边界面统计与接触锚定体检
# ---------------------------------------------------------------------------

def _face_stats(
    solids: Iterable[CdbElement],
) -> dict[int, dict[tuple[int, ...], int]]:
    """逐 part 统计四面角点升序键出现次数(键恰一次 = 边界面)。"""
    faces: dict[int, dict[tuple[int, ...], int]] = {}
    for element in solids:
        part_faces = faces.setdefault(element.type_seq, {})
        for corner_idx, _mid_idx in TET10_FACES:
            key = tuple(sorted(element.nodes[i] for i in corner_idx))
            part_faces[key] = part_faces.get(key, 0) + 1
    return faces


def boundary_face_counts(solids: Iterable[CdbElement]) -> dict[int, int]:
    """逐 part 统计边界面数(角点升序键在全模型恰出现一次的面)。"""
    return {
        part: sum(1 for count in counts.values() if count == 1)
        for part, counts in _face_stats(solids).items()
    }


def _boundary_face_node_sets(solids: Sequence[CdbElement]) -> dict[int, list[frozenset[int]]]:
    """逐 part 收集边界面完整节点集(角点 + 棱中点,用于接触锚定比对)。"""
    stats = _face_stats(solids)
    result: dict[int, list[frozenset[int]]] = {}
    for element in solids:
        part_sets = result.setdefault(element.type_seq, [])
        for corner_idx, mid_idx in TET10_FACES:
            key = tuple(sorted(element.nodes[i] for i in corner_idx))
            if stats[element.type_seq].get(key) == 1:
                part_sets.append(frozenset(element.nodes[i] for i in (*corner_idx, *mid_idx)))
    return result


def check_contact_anchoring(
    solids: Sequence[CdbElement],
    contacts: Sequence[CdbElement],
) -> list[str]:
    """接触锚定体检:每个接触单元去重节点集应恰等于某部件一个边界面节点集。

    返回警告列表(非致命);皮节点集含棱中点,接触单元退化四边形去重后恰为
    面 6 节点(3 角点 + 3 棱中点)。
    """
    boundary = _boundary_face_node_sets(solids)
    warnings: list[str] = []
    for element in contacts:
        node_set = frozenset(element.nodes)
        anchored = any(
            node_set in face_sets for face_sets in boundary.values()
        )
        if not anchored:
            warnings.append(
                f"接触单元 {element.eid}(TYPE {element.type_seq} {element.etype})"
                "节点集未锚定到任何固体部件边界面"
            )
    return warnings


# ---------------------------------------------------------------------------
# 固体单元校验(emap 单一宽度契约)
# ---------------------------------------------------------------------------

def _validate_solids(solids: Sequence[CdbElement], node_ids: frozenset[int]) -> None:
    """固体节点数一致(单一宽度)、节点互异正数且 ∈ NBLOCK;违规即显式失败。"""
    widths = {len(element.nodes) for element in solids}
    if len(widths) > 1:
        detail = "、".join(
            f"EID {element.eid} 为 {len(element.nodes)} 节点"
            for element in solids
            if len(element.nodes) != TET10_NODE_COUNT
        )
        raise ConversionError(f"固体单元节点数不一致(emap 单一宽度契约):{detail}")
    width = widths.pop()
    if width != TET10_NODE_COUNT:
        raise ConversionError(
            f"固体单元节点数为 {width},emap 契约仅支持 tet10({TET10_NODE_COUNT} 节点)"
        )
    for element in solids:
        if len(set(element.nodes)) != len(element.nodes):
            raise ConversionError(
                f"固体单元 {element.eid}(TYPE {element.type_seq})节点非互异:"
                f"{element.nodes}"
            )
        bad = [n for n in element.nodes if n <= 0]
        if bad:
            raise ConversionError(
                f"固体单元 {element.eid}(TYPE {element.type_seq})含非正节点号:{bad}"
            )
        missing = sorted(set(element.nodes) - node_ids)
        if missing:
            raise ConversionError(
                f"固体单元 {element.eid}(TYPE {element.type_seq})"
                f"节点不在 NBLOCK 中:{missing}"
            )


# ---------------------------------------------------------------------------
# 工件写出
# ---------------------------------------------------------------------------

def _write_lines(path: Path, lines: Sequence[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_artifacts(
    out_dir: Path,
    aligned: Sequence[tuple[int, tuple[str, ...]]],
    solids: Sequence[CdbElement],
) -> None:
    """写出 frame_1.csv / emap.csv / epart.csv(覆盖同名)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = ["node,x,y,z,ux,uy,uz"]
    frame.extend(f"{nid},{','.join(tokens)}" for nid, tokens in aligned)
    emap = ["elem," + ",".join(f"n{i}" for i in range(1, TET10_NODE_COUNT + 1))]
    emap.extend(
        f"{element.eid},{','.join(str(n) for n in element.nodes)}" for element in solids
    )
    epart = ["elem,part"]
    epart.extend(f"{element.eid},{element.type_seq}" for element in solids)
    _write_lines(out_dir / "frame_1.csv", frame)
    _write_lines(out_dir / "emap.csv", emap)
    _write_lines(out_dir / "epart.csv", epart)


# ---------------------------------------------------------------------------
# CLI 主流程
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="cdb + 节点表 → hip-playback 回放工件(frame/emap/epart)",
    )
    parser.add_argument("--cdb", required=True, type=Path, help="MAPDL 归档 cdb 路径")
    parser.add_argument("--nodes", required=True, type=Path, help="节点表桥接 csv 路径")
    parser.add_argument("--out", required=True, type=Path, help="工件输出目录(覆盖同名)")
    parser.add_argument(
        "--etypes", default=DEFAULT_ETYPES,
        help=f"按 ET 名过滤的固体单元(逗号分隔,默认 {DEFAULT_ETYPES})",
    )
    parser.add_argument(
        "--coord-tol", type=float, default=DEFAULT_COORD_TOL,
        help=f"节点表 ↔ NBLOCK 坐标断言容差(默认 {DEFAULT_COORD_TOL})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """转换主流程:解析 → 校验 → 对齐 → 体检 → 写出 → 摘要;失败抛 ConversionError。"""
    args = _build_arg_parser().parse_args(argv)

    cdb = parse_cdb(args.cdb)
    wanted_etypes = {name.strip() for name in args.etypes.split(",") if name.strip()}
    solids = tuple(e for e in cdb.elements if e.etype in wanted_etypes)
    skipped = tuple(e for e in cdb.elements if e.etype not in wanted_etypes)
    if not solids:
        raise ConversionError(
            f"etype 过滤后无固体单元:期望 ET 名 {sorted(wanted_etypes)},"
            f"cdb 实有 ET 名 {sorted(set(cdb.etypes.values()))}"
        )
    _validate_solids(solids, frozenset(n.nid for n in cdb.nodes))

    csv_lines = [
        line for line in args.nodes.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not csv_lines or not csv_lines[0].lstrip().startswith("node"):
        raise ConversionError(f"节点表 {args.nodes} 缺少表头(首行应以 node 开头)")
    aligned = align_nodes_csv(csv_lines[1:], cdb.nodes, args.coord_tol)

    contacts = tuple(e for e in skipped if _is_contact_etype(e.etype))
    warnings = check_contact_anchoring(solids, contacts)

    write_artifacts(args.out, aligned, solids)

    _print_summary(cdb, solids, skipped, contacts, warnings, aligned, args.out)
    return 0


def _print_summary(
    cdb: Cdb,
    solids: Sequence[CdbElement],
    skipped: Sequence[CdbElement],
    contacts: Sequence[CdbElement],
    warnings: Sequence[str],
    aligned: Sequence[tuple[int, tuple[str, ...]]],
    out_dir: Path,
) -> None:
    """stdout 摘要:节点/单元计数、逐 part 单元数与边界面数、跳过单元、输出路径。"""
    solid_by_type = Counter(e.type_seq for e in solids)
    skipped_by_type = Counter(e.type_seq for e in skipped)
    faces = boundary_face_counts(solids)
    print(f"节点数(NBLOCK): {len(cdb.nodes)};对齐节点表行数: {len(aligned)}")
    print(f"单元总数: {len(cdb.elements)};固体单元: {len(solids)};跳过单元: {len(skipped)}")
    for part in sorted(solid_by_type):
        etype = next(e.etype for e in solids if e.type_seq == part)
        print(f"  part {part}({etype}):"
              f"单元 {solid_by_type[part]} 个,边界面 {faces.get(part, 0)} 个")
    for part in sorted(skipped_by_type):
        etype = next((e.etype for e in skipped if e.type_seq == part), "?")
        print(f"  跳过 TYPE {part}({etype}):{skipped_by_type[part]} 个")
    anchored = len(contacts) - len(warnings)
    print(f"接触锚定: {anchored}/{len(contacts)} 已锚定")
    for warning in warnings:
        print(f"警告: {warning}")
    print(f"输出目录: {out_dir}")
    for name in ("frame_1.csv", "emap.csv", "epart.csv"):
        print(f"  {out_dir / name}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConversionError as error:
        print(f"错误: {error}", file=sys.stderr)
        sys.exit(1)
