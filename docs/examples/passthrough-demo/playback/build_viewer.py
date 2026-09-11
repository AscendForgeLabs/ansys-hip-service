#!/usr/bin/env python3
"""cube 型三维回放页构建器:frame_N.csv → 数据注入模板 → index.html。

用法:
    python build_viewer.py <帧目录> [--output-dir <目录>]
    # 帧目录 = 已下载的 passthrough 工件目录(含 frame_*.csv,
    #          可选 progress.csv / results.csv);index.html 默认生成在帧目录。

配套 cube.template.html(同目录)与 three.min.js(随输出复制)。
数据契约与渲染配方详见 docs/playback-handbook.md。

适用范围(参考实现的明确边界):
  * 规则结构网格六面体域(hex20 二次单元:每轴坐标 = 角点等距格 + 棱中点,
    即唯一坐标值个数 = 2k+1 且均匀);cube_dent.inp 即此形态。
  * 非规则网格/非六面体域:请按手册 §3 档②(自导 emap.csv)或档③(点云)自建。

六面渲染网格构造规则(与 SOLID186 网格结构一一对应):
  * 每个面 = k x k 个单元面;hex20 单元面 = 4 角点 + 4 棱中点(无面心节点)。
  * 每个单元面加 1 个"虚拟面心"渲染顶点,取值按 8 节点 Serendipity 形函数
    在面心的系数:pos/u = -0.25*Σ角点 + 0.5*Σ棱中点(位移场同式线性外推,
    得到真实的二次曲面观感,凹陷边缘是弯的而不是折线)。
  * 三角化 = 面心扇形 8 个三角形;绕向按 a轴x b轴 与面外法线点积定正反
    (点积为负则翻,否则该面从外侧看是背面、被照成黑面 —— 实测踩坑)。
  * 网格线 = 角点格的行/列段。
共享顶点缓冲:6 个面 + 网格线全部引用同一份 position/color BufferAttribute,
逐帧只更新一次。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "cube.template.html"
THREE_JS = HERE / "three.min.js"


def read_frame(path: Path) -> dict[int, list[float]]:
    """frame_N.csv → {节点号: [x,y,z,ux,uy,uz]}。"""
    nodes: dict[int, list[float]] = {}
    for ln in path.read_text().splitlines()[1:]:   # 首行表头
        if not ln.strip():
            continue
        v = [float(t) for t in ln.split(",")]
        nodes[int(v[0])] = v[1:]
    return nodes


def read_stage_data(art: Path) -> tuple[list[str], list[float], dict[str, float]]:
    """progress.csv → (标签, 累计秒);results.csv → {标签: 数值}。两者均可缺席。"""
    labels, times, values = [], [], {}
    prog = art / "progress.csv"
    if prog.exists():
        for ln in prog.read_text().splitlines():
            if ln.strip():
                k, v = ln.split(",")
                labels.append(k.strip())
                times.append(float(v))
    res = art / "results.csv"
    if res.exists():
        for ln in res.read_text().splitlines():
            if ln.strip():
                k, v = ln.split(",")
                values[k.strip()] = float(v)
    return labels, times, values


def detect_axis_lattice(coords: list[float], axis: str) -> tuple[float, int, list[float]]:
    """单轴结构格检测:返回 (单元边长, 每边单元数, 角点坐标表)。

    hex20 检测口径:唯一坐标值均匀间距 g(=半单元边),角点 = g 的偶数倍处,
    棱中点 = 奇数倍处;角点数 = k+1、总点数 = 2k+1。不满足即报错退场。"""
    uniq = sorted({round(c, 3) for c in coords})
    gaps = {round(b - a, 6) for a, b in zip(uniq, uniq[1:])}
    if len(gaps) != 1 or uniq[0] != 0:
        raise SystemExit(
            f"轴 {axis} 非均匀结构格(唯一值 {len(uniq)} 个,间距集 {gaps});"
            f"本参考实现仅支持规则 hex20 六面体域,其他网格见 docs/playback-handbook.md §3")
    g = gaps.pop()
    corners = [v for v in uniq if round(v / g) % 2 == 0]
    if len(corners) * 2 - 1 != len(uniq):
        raise SystemExit(
            f"轴 {axis} 不是 hex20 角点+棱中点形态(角点 {len(corners)} / 总 {len(uniq)});"
            f"线性单元或非规则网格见 docs/playback-handbook.md §3")
    return 2 * g, len(corners) - 1, corners


def main() -> int:
    ap = argparse.ArgumentParser(description="frame_N.csv → 交互式 3D 回放页")
    ap.add_argument("frames_dir", type=Path, help="工件目录(含 frame_*.csv)")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="index.html 输出目录(默认 = 帧目录)")
    args = ap.parse_args()
    art = args.frames_dir
    out_dir = args.output_dir or art

    frame_files = sorted(
        (p for p in art.glob("frame_*.csv") if re.fullmatch(r"frame_\d+\.csv", p.name)),
        key=lambda p: int(re.search(r"\d+", p.name).group()))
    if not frame_files:
        raise SystemExit(f"{art} 下没有 frame_N.csv")
    frames = [read_frame(p) for p in frame_files]
    nseg = len(frames)
    stages_labels, stages_times, results = read_stage_data(art)

    # 包围盒与三轴结构格(以首帧初始构型为基准;检测按"减去轴最小值"归零,
    # 故只需各轴最小值)
    first = frames[0]
    lo_by_axis = {ax: min(v[i] for v in first.values()) for i, ax in enumerate("xyz")}
    lat = {}
    for i, ax in enumerate("xyz"):
        cell, k, corners = detect_axis_lattice(
            [v[i] - lo_by_axis[ax] for v in first.values()], ax)
        lat[ax] = (lo_by_axis[ax], cell, k, corners)
    side = {ax: lat[ax][1] * lat[ax][2] for ax in "xyz"}   # 每轴边长

    # 面表:(固定轴, 网格轴 a, 网格轴 b);外法线 = a×b 或其反
    AXI = {"x": 0, "y": 1, "z": 2}
    FACES = [("y", "max", "x", "z"), ("y", "min", "x", "z"),
             ("x", "max", "z", "y"), ("x", "min", "z", "y"),
             ("z", "max", "x", "y"), ("z", "min", "x", "y")]
    CROSS = {("x", "y"): 1, ("y", "z"): 1, ("z", "x"): 1,
             ("y", "x"): -1, ("z", "y"): -1, ("x", "z"): -1}

    verts: list[list[float]] = []           # 渲染顶点基准坐标
    norms: list[list[float]] = []           # 每顶点面外法线(静态;光照用)
    frames_u: list[list[list[float]]] = [[] for _ in range(nseg)]

    def add_vert(pos, u_per_frame, normal) -> int:
        verts.append([round(c, 4) for c in pos])
        norms.append(normal)
        for f, u in enumerate(u_per_frame):
            frames_u[f].append([round(c, 5) for c in u])
        return len(verts) - 1

    face_geoms: list[dict] = []
    for fix_ax, side_, a_ax, b_ax in FACES:
        fidx, aidx, bidx = AXI[fix_ax], AXI[a_ax], AXI[b_ax]
        lo, cell, k, _ = lat[fix_ax]
        fix_val = lo + side[fix_ax] if side_ == "max" else lo
        sign = 1.0 if side_ == "max" else -1.0
        normal = [0.0, 0.0, 0.0]
        normal[fidx] = sign
        flip = CROSS[(a_ax, b_ax)] * sign < 0   # 绕向修正判据(见模块 docstring)

        key2vi: dict[tuple[float, float], int] = {}
        for nid, v in first.items():
            if abs(v[fidx] - fix_val) < 1e-4:
                key2vi[(round(v[aidx], 3), round(v[bidx], 3))] = add_vert(
                    v[0:3], [fr[nid][3:6] for fr in frames], normal)

        def vi(a: float, b: float) -> int:
            return key2vi[(round(a, 3), round(b, 3))]

        tris: list[int] = []
        for i in range(k):
            for j in range(k):
                x0, x1, y0, y1 = cell * i, cell * (i + 1), cell * j, cell * (j + 1)
                h = cell / 2
                c = [vi(x0, y0), vi(x1, y0), vi(x1, y1), vi(x0, y1)]
                m = [vi(x0 + h, y0), vi(x1, y0 + h), vi(x0 + h, y1), vi(x0, y0 + h)]
                ctr = add_vert(  # 虚拟面心:Serendipity 面心系数
                    [-0.25 * sum(verts[t][d] for t in c) + 0.5 * sum(verts[t][d] for t in m)
                     for d in range(3)],
                    [[-0.25 * sum(frames_u[f][t][d] for t in c)
                      + 0.5 * sum(frames_u[f][t][d] for t in m) for d in range(3)]
                     for f in range(nseg)],
                    normal)
                celltris = [c[0], m[0], ctr, ctr, m[0], c[1],
                            c[1], m[1], ctr, ctr, m[1], c[2],
                            c[2], m[2], ctr, ctr, m[2], c[3],
                            c[3], m[3], ctr, ctr, m[3], c[0]]
                if flip:
                    celltris = [v for s in range(0, len(celltris), 3)
                                for v in (celltris[s], celltris[s+2], celltris[s+1])]
                tris += celltris
        wire: list[int] = []
        for n in range(k + 1):
            for s in range(k):
                wire += [vi(cell * s, cell * n), vi(cell * (s + 1), cell * n)]
                wire += [vi(cell * n, cell * s), vi(cell * n, cell * (s + 1))]
        face_geoms.append({"tris": tris, "wire": wire})

    color_max = max((ux * ux + uy * uy + uz * uz) ** 0.5
                    for fu in frames_u for ux, uy, uz in fu)
    depths = [0.0] + [round(-min(r[4] for r in fr.values()), 4) for fr in frames]

    data = {
        "meta": {"side": max(side.values()),
                 "nseg": nseg,
                 "colorMax": round(color_max, 4),
                 "results": {k: round(v, 4) for k, v in results.items()}},
        "vertBase": [c for v in verts for c in v],
        "vertNorm": [c for n in norms for c in n],
        "framesU": [[c for u in fu for c in u] for fu in frames_u],
        "faces": face_geoms,
        "stages": {"label": stages_labels or ["MESH"] + [f"F{i}" for i in range(1, nseg + 1)],
                   "time": stages_times or [0.0] * (nseg + 1),
                   "depth": depths},
    }

    html = TEMPLATE.read_text().replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html)
    if not (out_dir / "three.min.js").exists():
        shutil.copy(THREE_JS, out_dir / "three.min.js")
    print(f"index.html 已生成({len(html) // 1024} KB):{out_dir / 'index.html'}")
    print(f"帧数 {nseg},渲染顶点 {len(verts)},colorMax={color_max:.4f} mm;"
          f"浏览器直接打开即可(#t=<秒>&scale=<倍率> 直达定位)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
