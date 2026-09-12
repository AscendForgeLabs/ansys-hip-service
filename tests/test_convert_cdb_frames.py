"""cdb → 回放工件转换器用例 — 内联合成 cdb 的离线解析与端到端产出.

覆盖任务书要求的六个方面:
    1. 快乐路径:2 个共享面 tet10 + 1 个被跳过的接触单元 → 三输出逐行断言
       (真节点号、emap 11 列、part = TYPE 序号);
    2. EBLOCK token 流跨行(SOLID187 记录 21 字段拆 19+2 两行)与 `-1` 终结;
    3. NBLOCK 定宽黏连解析:负坐标与 E 字段粘连、行尾短行(z 字段整段省略兜 0.0);
    4. 显式中文失败:坐标超差(报行号+两侧值)/ 行数不符 / etype 过滤后空 /
       NBLOCK·EBLOCK 计数对账不符 / 固体单元节点数混宽;
    5. boundary_face_counts 纯函数:2 个 tet 共享一面 → 边界面 6;
    6. E16.8 token 逐字符透传(零精度损失,不做 float 往返重排)。

全部用内联字符串构造合成 cdb,小到可读;不运行 MAPDL,纯标准库。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import convert_cdb_frames as conv
from convert_cdb_frames import (
    CdbElement,
    boundary_face_counts,
    parse_cdb,
)

# ---------------------------------------------------------------------------
# 合成数据:2 个共享面 (1,2,3) 的 tet10(角点 1..5,棱中点 6..14)+ 1 个
# 贴在边界面 (1,2,4) 上的 CONTA174 退化四边形(nn=8,去重 6 节点)。
# ---------------------------------------------------------------------------

TET_A = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11)
TET_B = (1, 2, 3, 5, 6, 7, 8, 12, 13, 14)
# CONTA174 退化四边形槽位:角点 1,2,4,4(第 4 槽重复第 3 槽)+ 棱中点
CONTACT_NODES = (1, 2, 4, 4, 6, 10, 9, 4)
CONTACT_FACE_SET = frozenset({1, 2, 4, 6, 9, 10})

NODE_XYZ = {
    1: (0.0, 0.0, 0.0),
    2: (1.0, 0.0, 0.0),
    3: (0.0, 1.0, 0.0),
    4: (0.0, 0.0, 1.0),
    5: (0.0, 0.0, -1.0),
    6: (0.5, 0.0, 0.0),
    7: (0.5, 0.5, 0.0),
    8: (0.0, 0.5, 0.0),
    9: (0.0, 0.0, 0.5),
    10: (0.5, 0.0, 0.5),
    11: (0.0, 0.5, 0.5),
    12: (0.0, 0.0, -0.5),
    13: (0.5, 0.0, -0.5),
    14: (0.0, 0.5, -0.5),
}


# ---------------------------------------------------------------------------
# cdb 文本构造助手(按 MAPDL 定宽格式逐字符拼行)
# ---------------------------------------------------------------------------

def nblock_line(nid: int, x: float, y: float, z: float | None) -> str:
    """NBLOCK 数据行:(3i9,6e21.13e3),z=None 模拟行尾短行(z 整段省略)。"""
    head = f"{nid:>9d}{0:>9d}{0:>9d}"
    tail = f"{x:>21.13E}{y:>21.13E}"
    if z is None:
        return head + tail
    return head + tail + f"{z:>21.13E}"


def etblock_entry(seq: int, name: str) -> str:
    """ETBLOCK 数据行:(2i9,19a9):[0:9] TYPE 序号、[9:18] 单元名。"""
    return f"{seq:>9d}{name:>9s}"


def eblock_records(records: list[tuple[int, int, int, tuple[int, ...]]]) -> str:
    """EBLOCK 数据行:(19i10) token 流;nn>9 的记录 21 字段拆 19+2 两行。

    每条记录 (mat, type_seq, eid, nodes);字段 [8] = nn、[10] = eid。
    """
    lines: list[str] = []
    for mat, tseq, eid, nodes in records:
        fields = [mat, tseq, 1, 1, 0, 0, 0, 0, len(nodes), 0, eid, *nodes]
        text = "".join(f"{v:>10d}" for v in fields)
        lines.append(text[:190])
        if text[190:]:
            lines.append(text[190:])
    return "\n".join(lines)


def build_cdb(
    *,
    et_entries: list[tuple[int, str]] | None = None,
    nb_lines: list[str] | None = None,
    eb_records: list[tuple[int, int, int, tuple[int, ...]]] | None = None,
    nb_count: int | None = None,
    eb_count: int | None = None,
) -> str:
    """拼一份最小 cdb:ETBLOCK / NBLOCK / EBLOCK 三块,头部计数可注入错值。"""
    et_entries = et_entries if et_entries is not None else [(1, "187"), (2, "CONTA174")]
    nb_lines = nb_lines if nb_lines is not None else [
        nblock_line(nid, *xyz) for nid, xyz in NODE_XYZ.items()
    ]
    eb_records = eb_records if eb_records is not None else [
        (1, 1, 1, TET_A),
        (1, 1, 2, TET_B),
        (2, 2, 3, CONTACT_NODES),
    ]
    nb_count = len(nb_lines) if nb_count is None else nb_count
    eb_count = len(eb_records) if eb_count is None else eb_count
    parts = [
        "/COM, 合成 cdb(仅用于测试)",
        "ETBLOCK," + f"{len(et_entries):>9d},{len(et_entries):>9d}",
        "(2i9,19a9)",
        *[etblock_entry(seq, name) for seq, name in et_entries],
        "       -1",
        f"NBLOCK,6,SOLID,{nb_count:>10d},{nb_count:>10d}",
        "(3i9,6e21.13e3)",
        *nb_lines,
        "N,UNBL,LOC,       -1,",
        f"EBLOCK,19,SOLID,{eb_count:>10d},{eb_count:>10d}",
        "(19i10)",
        eblock_records(eb_records),
        "       -1",
    ]
    return "\n".join(parts) + "\n"


def build_nodes_csv() -> str:
    """桥接节点表:表头截断为 "node x y",数据行 7 列空白分隔(首列占位星号)。"""
    lines = ["node x y"]
    for nid, (x, y, z) in NODE_XYZ.items():
        ux, uy, uz = 0.1 * nid, -0.2 * nid, 0.3 * nid
        lines.append(
            f"********  {x:.8E}  {y:.8E}  {z:.8E}  {ux:.8E}  {uy:.8E}  {uz:.8E}"
        )
    return "\n".join(lines) + "\n"


def write_inputs(tmp_path: Path, cdb_text: str, csv_text: str) -> tuple[Path, Path]:
    cdb_path = tmp_path / "job.cdb"
    csv_path = tmp_path / "nodes.csv"
    cdb_path.write_text(cdb_text, encoding="utf-8")
    csv_path.write_text(csv_text, encoding="utf-8")
    return cdb_path, csv_path


def run_convert(tmp_path: Path, cdb_path: Path, csv_path: Path, extra: list[str] | None = None):
    out = tmp_path / "artifacts"
    argv = [
        "--cdb", str(cdb_path),
        "--nodes", str(csv_path),
        "--out", str(out),
        *(extra or []),
    ]
    return conv.main(argv), out


# ---------------------------------------------------------------------------
# 用例 1:快乐路径 —— 三输出逐行断言
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_three_artifacts_line_by_line(self, tmp_path, capsys) -> None:
        cdb_path, csv_path = write_inputs(tmp_path, build_cdb(), build_nodes_csv())
        rc, out = run_convert(tmp_path, cdb_path, csv_path)

        assert rc == 0

        # frame_1.csv:表头 7 列 + 14 个 NBLOCK 真节点号
        frame_lines = (out / "frame_1.csv").read_text(encoding="utf-8").splitlines()
        assert frame_lines[0] == "node,x,y,z,ux,uy,uz"
        assert len(frame_lines) == 1 + len(NODE_XYZ)
        assert [line.split(",")[0] for line in frame_lines[1:]] == [str(n) for n in NODE_XYZ]

        # emap.csv:表头 11 列,仅固体,行 = elem + 10 节点
        emap_lines = (out / "emap.csv").read_text(encoding="utf-8").splitlines()
        assert emap_lines[0] == "elem,n1,n2,n3,n4,n5,n6,n7,n8,n9,n10"
        assert emap_lines[1] == "1," + ",".join(map(str, TET_A))
        assert emap_lines[2] == "2," + ",".join(map(str, TET_B))
        assert len(emap_lines) == 3  # 接触单元不入 emap

        # epart.csv:part = TYPE 序号(不是 MAT)
        epart_lines = (out / "epart.csv").read_text(encoding="utf-8").splitlines()
        assert epart_lines == ["elem,part", "1,1", "2,1"]

        # stdout 摘要:计数与锚定情况
        summary = capsys.readouterr().out
        assert "14" in summary and "3" in summary
        assert "未锚定" not in summary

    def test_frame_tokens_passed_through_verbatim(self, tmp_path) -> None:
        """用例 6:E16.8 token 逐字符透传(0.30000000E+02 不能被重排成 30.0)。"""
        csv_lines = [
            "node x y",
            "********  0.30000000E+02  0.30000000E+02  0.30000000E+02"
            "  0.12345670E+01 -0.87654320E-02  0.45678900E+00",
        ]
        node1 = nblock_line(1, 30.0, 30.0, 30.0)
        cdb_text = build_cdb(nb_lines=[node1])
        cdb_path, csv_path = write_inputs(tmp_path, cdb_text, "\n".join(csv_lines) + "\n")
        rc, out = run_convert(tmp_path, cdb_path, csv_path)

        assert rc == 0
        frame_lines = (out / "frame_1.csv").read_text(encoding="utf-8").splitlines()
        assert frame_lines[1] == (
            "1,0.30000000E+02,0.30000000E+02,0.30000000E+02,"
            "0.12345670E+01,-0.87654320E-02,0.45678900E+00"
        )


# ---------------------------------------------------------------------------
# 用例 2:EBLOCK token 流跨行与 -1 终结;用例 3:NBLOCK 黏连与短行
# ---------------------------------------------------------------------------

class TestParseCdb:
    def test_eblock_token_stream_spans_two_lines_and_terminates(self, tmp_path) -> None:
        """21 字段记录跨 19+2 两行;`-1` 终结;后续无关块(SFEBLOCK)跳过。"""
        text = build_cdb() + "SFEBLOCK,1,PRES,       3,        6,0\n(3i9)\n       -1\n"
        cdb_path = tmp_path / "job.cdb"
        cdb_path.write_text(text, encoding="utf-8")

        cdb = parse_cdb(cdb_path)

        assert cdb.etypes == {1: "187", 2: "CONTA174"}
        assert [el.eid for el in cdb.elements] == [1, 2, 3]
        assert cdb.elements[0].nodes == TET_A  # 续行 2 个节点被收进同一记录
        assert cdb.elements[1].nodes == TET_B
        assert cdb.elements[2].nodes == CONTACT_NODES
        assert cdb.elements[0].etype == "187"
        assert cdb.elements[2].etype == "CONTA174"
        assert len(cdb.nodes) == len(NODE_XYZ)
        assert cdb.nodes[0].nid == 1 and cdb.nodes[13].nid == 14

    def test_nblock_glued_negative_coords_and_missing_z(self, tmp_path) -> None:
        """负坐标与 E 字段黏连按定宽切开;行尾短行(69 字符)z 兜 0.0。"""
        nb = [
            nblock_line(1, 1.0, 2.0, 3.0),
            nblock_line(2, -3.0, -2.5, 0.125),
            nblock_line(3, -1.5, 2.0, None),  # 短行:z 整段省略
        ]
        cdb_path = tmp_path / "job.cdb"
        cdb_path.write_text(build_cdb(nb_lines=nb), encoding="utf-8")

        cdb = parse_cdb(cdb_path)

        assert cdb.nodes[0].x == pytest.approx(1.0)
        assert cdb.nodes[1].x == pytest.approx(-3.0)
        assert cdb.nodes[1].y == pytest.approx(-2.5)
        assert cdb.nodes[1].z == pytest.approx(0.125)
        assert cdb.nodes[2].x == pytest.approx(-1.5)
        assert cdb.nodes[2].z == 0.0  # 缺席字段兜 0.0


# ---------------------------------------------------------------------------
# 用例 4:显式中文失败
# ---------------------------------------------------------------------------

class TestExplicitFailures:
    def test_coordinate_mismatch_reports_row_and_both_values(self, tmp_path) -> None:
        rows = build_nodes_csv().splitlines()
        rows[1] = rows[1].replace("1.00000000E+00", "9.00000000E+00")
        cdb_path, csv_path = write_inputs(
            tmp_path, build_cdb(), "\n".join(rows) + "\n"
        )

        with pytest.raises(conv.ConversionError) as excinfo:
            run_convert(tmp_path, cdb_path, csv_path)

        msg = str(excinfo.value)
        assert "第 1 行" in msg and "x" in msg
        assert "9.0" in msg and "1.0" in msg  # 两侧值都报

    def test_row_count_mismatch_fails(self, tmp_path) -> None:
        rows = build_nodes_csv().splitlines()[:-1]  # 少一行
        cdb_path, csv_path = write_inputs(
            tmp_path, build_cdb(), "\n".join(rows) + "\n"
        )

        with pytest.raises(conv.ConversionError, match="行数不符"):
            run_convert(tmp_path, cdb_path, csv_path)

    def test_empty_after_etype_filter_fails(self, tmp_path) -> None:
        cdb_path, csv_path = write_inputs(tmp_path, build_cdb(), build_nodes_csv())

        with pytest.raises(conv.ConversionError, match="过滤后"):
            run_convert(tmp_path, cdb_path, csv_path, extra=["--etypes", "999"])

    def test_nblock_header_count_reconciliation_fails(self, tmp_path) -> None:
        cdb_path = tmp_path / "job.cdb"
        cdb_path.write_text(build_cdb(nb_count=99), encoding="utf-8")

        with pytest.raises(conv.ConversionError, match="NBLOCK"):
            parse_cdb(cdb_path)

    def test_eblock_header_count_reconciliation_fails(self, tmp_path) -> None:
        cdb_path = tmp_path / "job.cdb"
        cdb_path.write_text(build_cdb(eb_count=50), encoding="utf-8")

        with pytest.raises(conv.ConversionError, match="EBLOCK"):
            parse_cdb(cdb_path)

    def test_mixed_solid_node_counts_fail(self, tmp_path) -> None:
        records = [
            (1, 1, 1, TET_A),
            (1, 1, 2, TET_A[:9]),  # 同为 TYPE 1 的固体,nn=9 → 混宽
        ]
        nb = [nblock_line(nid, *xyz) for nid, xyz in NODE_XYZ.items()]
        cdb_path, csv_path = write_inputs(
            tmp_path, build_cdb(eb_records=records, nb_lines=nb), build_nodes_csv()
        )

        with pytest.raises(conv.ConversionError, match="节点数"):
            run_convert(tmp_path, cdb_path, csv_path)


# ---------------------------------------------------------------------------
# 用例 5:boundary_face_counts 纯函数
# ---------------------------------------------------------------------------

class TestBoundaryFaceCounts:
    def test_two_shared_face_tets_yield_six_boundary_faces(self) -> None:
        tets = [
            CdbElement(eid=1, mat=1, type_seq=1, etype="187", nodes=TET_A),
            CdbElement(eid=2, mat=1, type_seq=1, etype="187", nodes=TET_B),
        ]

        assert boundary_face_counts(tets) == {1: 6}  # 4+4 面,共享面 ×1 → 6 个恰一次

    def test_groups_by_type_not_mat(self) -> None:
        tets = [
            CdbElement(eid=1, mat=7, type_seq=1, etype="187", nodes=TET_A),
            CdbElement(eid=2, mat=7, type_seq=2, etype="187", nodes=TET_B),
        ]

        assert boundary_face_counts(tets) == {1: 4, 2: 4}  # 不共享 → 各 4 面


# ---------------------------------------------------------------------------
# 补充:接触锚定体检(非致命警告)
# ---------------------------------------------------------------------------

class TestContactAnchoring:
    def test_unanchored_contact_warns_but_succeeds(self, tmp_path, capsys) -> None:
        """接触单元贴在内部共享面上 → 不属于任何边界面 → 打警告,退出码仍 0。"""
        interior_contact = (1, 2, 3, 3, 6, 7, 8, 3)  # 面 (1,2,3):两 tet 共享
        records = [
            (1, 1, 1, TET_A),
            (1, 1, 2, TET_B),
            (2, 2, 3, interior_contact),
        ]
        cdb_path, csv_path = write_inputs(
            tmp_path, build_cdb(eb_records=records), build_nodes_csv()
        )
        rc, _out = run_convert(tmp_path, cdb_path, csv_path)

        assert rc == 0
        summary = capsys.readouterr().out
        assert "警告" in summary
        assert "未锚定" in summary

    def test_anchored_contact_on_boundary_face_no_warning(self, tmp_path, capsys) -> None:
        cdb_path, csv_path = write_inputs(tmp_path, build_cdb(), build_nodes_csv())
        rc, _out = run_convert(tmp_path, cdb_path, csv_path)

        assert rc == 0
        summary = capsys.readouterr().out
        assert "未锚定" not in summary
        assert "已锚定" in summary
