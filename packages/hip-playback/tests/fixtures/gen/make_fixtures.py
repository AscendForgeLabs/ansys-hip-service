#!/usr/bin/env python3
"""生成 hip-playback 单测 fixture(在仓库根或本目录运行均可,产物全部入库)。

产物:
  tests/fixtures/real/            真机 cube-dent 工件的头部切片(钉住 MAPDL 真实格式)
  tests/fixtures/synthetic/       合成规则 hex20 网格(2x2x2 单元,10mm 立方):
      artifacts/frame_1..3.csv    MAPDL E16.8 格式(0.xxx 尾数、前导空格、2 位指数)
      artifacts/progress.csv / results.csv
      artifacts/emap.csv          8 角点 + 12 棱中点(21 列,hex20 全节点)
      cube-mesh.golden.json       由真 build_viewer.py 生成的 DATA 基准(JS 移植对拍用)

重新生成:python tests/fixtures/gen/make_fixtures.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parents[2]                     # packages/hip-playback(gen → fixtures → tests → 包根)
REPO = PKG.parent.parent                  # ansys-hip-service
BUILD_VIEWER = REPO / "docs/examples/passthrough-demo/playback/build_viewer.py"
REAL_ART = Path.home() / "ansys-hip-e2e-20260911/cube-dent/artifacts"

SYNTH = PKG / "tests/fixtures/synthetic"
ART = SYNTH / "artifacts"
REAL = PKG / "tests/fixtures/real"

# 合成网格参数:边长 10mm,每轴 2 单元(g=2.5,cell=5,k=2)→ 每轴 5 个唯一坐标。
# 节点集 = 真实 hex20(serendipity)网格会有的节点:角点(全偶索引)+ 棱中点
# (恰一个奇索引)= 81 个;面心/体心(≥2 个奇索引)不生成 —— MAPDL 不会为
# serendipity 单元创建这些节点,帧里有它们则 lattice(按坐标)与 emap(按连接表)
# 两路天然不同构,交叉验证无法成立。节点号保持格点公式(留空洞,不重排)。
K = 2
SIDE = 10.0
G = SIDE / (2 * K)                        # 半单元边(棱中点步长)
NUNIQ = 2 * K + 1                         # 每轴唯一坐标数
DEPTHS = [0.2, 0.5, 1.0]                  # 三帧压深


def odd_count(i: int) -> int:
    """节点索引(i-1 展开到三轴)中奇数索引的个数(≥2 = 面心/体心幽灵节点)。"""
    rem = i - 1
    ix, iy, iz = rem % NUNIQ, (rem // NUNIQ) % NUNIQ, rem // (NUNIQ * NUNIQ)
    return ix % 2 + iy % 2 + iz % 2


def e16_8(v: float) -> str:
    """按 MAPDL *VWRITE (E16.8) 口径格式化:0.xxx 尾数 8 位、2 位指数、前导空格补齐 16 列。"""
    if v == 0:
        return "  0.00000000E+00"
    s = f"{v: .8E}"                       # 如 " 1.00000000E+00" / "-5.11288660E-02"
    mant, exp = s.split("E")
    digits = mant.replace(" ", "").replace(".", "").removeprefix("-")   # 9 位有效数字
    sign = "-" if mant.strip().startswith("-") else ""
    # D.d2..d9 → 0.Dd2..d8 E(指数+1):尾数取前 8 位有效,末位截断(与 E16.8 精度一致)
    return f"{sign}0.{digits[:8]}E{int(exp) + 1:+03d}".rjust(16)


def node_id(ix: int, iy: int, iz: int) -> int:
    """节点号:1 + ix + iy*NUNIQ + iz*NUNIQ²(ix/iy/iz ∈ 0..2k,坐标 = G*索引)。"""
    return 1 + ix + iy * NUNIQ + iz * NUNIQ * NUNIQ


def node_xyz(i: int) -> tuple[float, float, float]:
    ix = (i - 1) % NUNIQ
    iy = ((i - 1) // NUNIQ) % NUNIQ
    iz = (i - 1) // (NUNIQ * NUNIQ)
    return (G * ix, G * iy, G * iz)


def disp(i: int, depth: float) -> tuple[float, float, float]:
    """位移场:顶部高斯凹陷 + 轻微侧向挤出的确定性公式(数值上有区分度即可)。"""
    x, y, z = node_xyz(i)
    g = depth * pow(2.718281828459045, -((x - 5) ** 2 + (z - 5) ** 2) / 8.0)
    return (0.06 * g * (x - 5) / 5, -g * (y / SIDE), 0.06 * g * (z - 5) / 5)


def write_frames() -> None:
    ids = [i for i in range(1, NUNIQ ** 3 + 1) if odd_count(i) <= 1]   # 81 个真实节点
    for f, depth in enumerate(DEPTHS, start=1):
        lines = ["node,x_mm,y_mm,z_mm,ux_mm,uy_mm,uz_mm"]
        for i in ids:
            x, y, z = node_xyz(i)
            ux, uy, uz = disp(i, depth)
            vals = [float(i), x, y, z, ux, uy, uz]
            lines.append(",".join(e16_8(v) for v in vals))
        (ART / f"frame_{f}.csv").write_text("\n".join(lines) + "\n")
    # progress:标签补齐 8 字符;results:同口径
    (ART / "progress.csv").write_text("\n".join(
        f"{lbl:<8s},{e16_8(t)}" for lbl, t in
        [("MESH", 0.0), ("DENT_A", 300.0), ("DENT_B", 600.0), ("DENT_C", 900.0)]) + "\n")
    (ART / "results.csv").write_text("\n".join(
        f"{lbl:<8s},{e16_8(v)}" for lbl, v in
        [("dent_dep", DEPTHS[-1]), ("uy_min", -DEPTHS[-1]), ("seqv_max", 123.4567)]) + "\n")


def write_emap() -> None:
    """hex20 单元表:每单元 8 角点(n1..n8)+ 12 棱中点(n9..n20),列序 = SOLID186 编号。"""
    def nid(ix: int, iy: int, iz: int) -> int:
        return node_id(ix, iy, iz)

    lines = ["elem," + ",".join(f"n{i}" for i in range(1, 21))]
    for cz in range(K):
        for cy in range(K):
            for cx in range(K):
                # 角点:底面 1-4(逆时针)、顶面 5-8;坐标索引步长 2(角点在偶数索引)
                x0, y0, z0 = 2 * cx, 2 * cy, 2 * cz
                x1, y1, z1 = x0 + 2, y0 + 2, z0 + 2
                c = [nid(x0, y0, z0), nid(x1, y0, z0), nid(x1, y1, z0), nid(x0, y1, z0),
                     nid(x0, y0, z1), nid(x1, y0, z1), nid(x1, y1, z1), nid(x0, y1, z1)]
                m = [  # 棱中点 = 两角点索引均值
                    nid((x0 + x1) // 2, y0, z0), nid(x1, (y0 + y1) // 2, z0),
                    nid((x0 + x1) // 2, y1, z0), nid(x0, (y0 + y1) // 2, z0),
                    nid((x0 + x1) // 2, y0, z1), nid(x1, (y0 + y1) // 2, z1),
                    nid((x0 + x1) // 2, y1, z1), nid(x0, (y0 + y1) // 2, z1),
                    nid(x0, y0, (z0 + z1) // 2), nid(x1, y0, (z0 + z1) // 2),
                    nid(x1, y1, (z0 + z1) // 2), nid(x0, y1, (z0 + z1) // 2)]
                vals = [float(2 * (cz * K + cy) + cx + 1)] + [float(v) for v in c + m]
                lines.append(",".join(e16_8(v) for v in vals))
    (ART / "emap.csv").write_text("\n".join(lines) + "\n")


def slice_real() -> None:
    """真机工件切片:frame 头 26 行 + 完整 progress/results(钉住真实格式,全部小文件)。"""
    if not REAL_ART.is_dir():
        sys.exit(f"真机工件目录不存在:{REAL_ART}(本机 e2e 环境才有,见 memory)")
    (REAL / "frame_head.csv").write_text(
        "\n".join(REAL_ART.joinpath("frame_1.csv").read_text().splitlines()[:26]) + "\n")
    for name in ("progress.csv", "results.csv"):
        shutil.copy(REAL_ART / name, REAL / name)


def make_golden() -> None:
    """用真 build_viewer.py 跑合成工件 → 提取 DATA → golden JSON(JS 对拍基准)。"""
    out = SYNTH / "_viewer_out"
    if out.exists():
        shutil.rmtree(out)
    subprocess.run([sys.executable, str(BUILD_VIEWER), str(ART), "--output-dir", str(out)],
                   check=True, capture_output=True)
    html = (out / "index.html").read_text()
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    if not m:
        sys.exit("未能从 index.html 提取 DATA")
    (SYNTH / "cube-mesh.golden.json").write_text(
        json.dumps(json.loads(m.group(1)), ensure_ascii=False, indent=1) + "\n")
    shutil.rmtree(out)
    print("golden 已生成:cube-mesh.golden.json")


if __name__ == "__main__":
    ART.mkdir(parents=True, exist_ok=True)
    REAL.mkdir(parents=True, exist_ok=True)
    write_frames()
    write_emap()
    slice_real()
    make_golden()
    n_frames = len(list(ART.glob("frame_*.csv")))
    print(f"synthetic artifacts:{n_frames} 帧 + emap.csv;real 切片完成")
