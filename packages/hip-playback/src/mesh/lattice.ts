/** 规则 hex20 格构网 — build_viewer.py(main 构网逻辑)的逐语义 TS 移植。
 *
 * 适用范围与参考实现一致(docs/examples/passthrough-demo/playback/build_viewer.py):
 * 规则结构网格六面体域 —— 每轴唯一坐标 = 角点等距格 + 棱中点(唯一值个数 2k+1
 * 且均匀);非规则网格抛错退场,指引 docs/playback-handbook.md §3(emap.csv 或点云)。
 *
 * 六面渲染网格构造规则(与 SOLID186 网格结构一一对应):
 *   * 每面 = k×k 单元面;hex20 单元面 = 4 角点 + 4 棱中点(无面心节点);
 *   * 每个单元面补 1 个"虚拟面心"渲染顶点,按 8 节点 Serendipity 形函数面心系数
 *     取值:pos/u = -0.25×Σ角点 + 0.5×Σ棱中点(位移同式线性外推,凹陷边缘是弯的
 *     而不是折线);
 *   * 三角化 = 面心扇形 8 三角;绕向按 a×b 与面外法线点积定正反(点积为负则整
 *     三角翻序,否则该面从外侧看是背面、被照成黑面);
 *   * 网格线 = 角点格的行/列段。
 * 共享顶点缓冲:6 面 + 网格线引用同一份顶点表。数值口径与 Python 端 golden 对拍
 * 钉死:顶点坐标 round4、位移 round5;检测/键口径沿用参考实现的
 * round3/round6(仅内部使用)。
 */

import type { FrameNodes, Vec6 } from "../csv";
import { buildStages, colorMaxOf, depthSeries } from "./common";
import type { FaceGeom, MeshData, MeshInput } from "./types";

const HANDBOOK_HINT = "其他网格见 docs/playback-handbook.md §3(emap.csv 或点云)";

type AxisName = "x" | "y" | "z";
const AXI: Record<AxisName, 0 | 1 | 2> = { x: 0, y: 1, z: 2 };

/** 面表:(固定轴, min/max, 网格轴 a, 网格轴 b);外法线 = a×b 或其反。 */
const FACES: ReadonlyArray<readonly [AxisName, "max" | "min", AxisName, AxisName]> = [
  ["y", "max", "x", "z"], ["y", "min", "x", "z"],
  ["x", "max", "z", "y"], ["x", "min", "z", "y"],
  ["z", "max", "x", "y"], ["z", "min", "x", "y"],
];

/** a×b 的轴手性:+1 = 右手正向(x×y=z 类),-1 = 反向(绕向修正判据用)。 */
const CROSS: Record<string, 1 | -1> = {
  "x,y": 1, "y,z": 1, "z,x": 1,
  "y,x": -1, "z,y": -1, "x,z": -1,
};

/** 单轴结构格:lo = 轴最小值(原始坐标,面定位用);cell = 单元边长;k = 每边单元数。 */
interface AxisLattice {
  lo: number;
  cell: number;
  k: number;
}

const sideOf = (l: AxisLattice): number => l.cell * l.k;   // 每轴边长 = cell × k

/** round 到 3/6 位(检测与面内键口径,镜像参考实现的 round(x,3)/round(x,6);
 *  对外数值口径用 common.ts 的 round4/round5,勿混用)。 */
const roundTo = (v: number, digits: 3 | 6): number => {
  const scale = digits === 3 ? 1e3 : 1e6;
  return Math.round(v * scale) / scale;
};

/** 面内 (a,b) 坐标键(round3;入键与查键同口径,即 Python 的 (round(a,3), round(b,3)))。 */
const keyAt = (a: number, b: number): string => `${roundTo(a, 3)},${roundTo(b, 3)}`;

/** Python round(x, digits) 语义:对 double 的精确十进制展开正确舍入。
 *
 * 不能用 common.ts 的 round4/round5(= Math.round(v*1eN)/1eN):缩放乘法会把
 * 十进制 .5 平局值(如 -0.013735,来自 E16.8 解析或 Serendipity 0.25/0.5 系数
 * 作用于 5 位小数)乘成恰好 k.5,Math.round 半值朝 +∞ 单侧舍入,与 Python 的
 * 正确舍入在负平局/偶数位平局处相差 1e-N —— golden 对拍实测 1566 个位移值中
 * 24 条因此偏差 1e-5。toFixed 按 spec 对精确二进制值正确舍入,与 Python round
 * 一致(仅二进精确平局的双整数 half 规则不同,本数据域不会出现)。
 * → golden 逐位对拍必须走本实现;common.ts 助手该口径问题需上游修(emap 同理)。 */
const pyRound = (v: number, digits: 4 | 5): number => Number(v.toFixed(digits));

/** Σ flat[3t+d](t ∈ idxs)。镜像 CPython ≥3.12 内置 sum() 的 Neumaier 补偿求和
 * (gh-100425):朴素左结合 reduce 在 4 项求和上会偶差 1 ulp,恰好把后续 round5
 * 的十进制平局值推错方向(golden 对拍实测 1566 个位移值中 24 条偏差 1e-5;
 * golden 由本机 Python 3.12.3 的 build_viewer.py 生成)。逐位一致是对拍硬前提。 */
const sumAt = (flat: readonly number[], idxs: readonly number[], d: number): number => {
  let total = 0;
  let comp = 0;                              // 补偿项(Kahan–Neumaier)
  for (const t of idxs) {
    const x = flat[3 * t + d]!;
    const s = total + x;
    comp += Math.abs(total) >= Math.abs(x) ? (total - s) + x : (x - s) + total;
    total = s;
  }
  return total + comp;
};

/** 单轴结构格检测(输入 = 减去轴最小值归零后的坐标):不满足 hex20 形态即抛错退场。
 *
 * hex20 检测口径:唯一坐标值均匀间距 g(=半单元边),角点 = g 的偶数倍处,
 * 棱中点 = 奇数倍处;角点数 = k+1、总点数 = 2k+1。 */
function detectAxisLattice(shifted: readonly number[], axis: AxisName): { cell: number; k: number } {
  const uniq = Array.from(new Set(shifted.map((c) => roundTo(c, 3)))).sort((p, q) => p - q);
  const gaps = new Set<number>();
  for (let i = 1; i < uniq.length; i++) gaps.add(roundTo(uniq[i]! - uniq[i - 1]!, 6));
  if (gaps.size !== 1 || uniq[0] !== 0) {
    throw new Error(
      `轴 ${axis} 非均匀结构格(唯一值 ${uniq.length} 个,间距集 [${[...gaps].join(", ")}]);` +
      `本参考实现仅支持规则 hex20 六面体域,${HANDBOOK_HINT}`);
  }
  const g = [...gaps][0]!;
  const cornerCount = uniq.filter((v) => Math.round(v / g) % 2 === 0).length;
  if (cornerCount * 2 - 1 !== uniq.length) {
    throw new Error(
      `轴 ${axis} 不是 hex20 角点+棱中点形态(角点 ${cornerCount} / 总 ${uniq.length});` +
      `线性单元或非规则网格,${HANDBOOK_HINT}`);
  }
  return { cell: 2 * g, k: cornerCount - 1 };
}

/** 以首帧为基准求某轴结构格(检测前减去轴最小值归零)。 */
function latticeFor(first: FrameNodes, i: 0 | 1 | 2, axis: AxisName): AxisLattice {
  let lo = Infinity;
  for (const v of first.values()) if (v[i] < lo) lo = v[i];
  const shifted: number[] = [];
  for (const v of first.values()) shifted.push(v[i] - lo);
  return { ...detectAxisLattice(shifted, axis), lo };
}

/** results.csv 标量按 round4 入 meta(参考实现同款,Python 舍入口径)。 */
const roundResults = (results: Record<string, number> | undefined): Record<string, number> =>
  Object.fromEntries(Object.entries(results ?? {}).map(([k, v]) => [k, pyRound(v, 4)]));

/** 顶点缓冲 + 6 面装配(面顶点入表顺序 = 面节点遍历序 + 逐单元面心,golden 钉序)。 */
function buildGeometry(
  frames: ReadonlyArray<FrameNodes>,
  lat: Record<AxisName, AxisLattice>,
): { vertBase: number[]; vertNorm: number[]; framesU: number[][]; faces: FaceGeom[] } {
  const nseg = frames.length;
  const first = frames[0]!;
  const vertBase: number[] = [];                          // 渲染顶点基准坐标(平铺)
  const vertNorm: number[] = [];                          // 每顶点面外法线(平铺,静态)
  const framesU: number[][] = Array.from({ length: nseg }, () => []);
  const faceGeoms: FaceGeom[] = [];

  /** 入表一个渲染顶点:坐标 round4 / 位移 round5(Python 舍入口径),返回顶点序号。 */
  const addVert = (
    pos: readonly number[],
    uPerFrame: ReadonlyArray<readonly number[]>,
    normal: readonly number[],
  ): number => {
    for (let d = 0; d < 3; d++) {
      vertBase.push(pyRound(pos[d]!, 4));
      vertNorm.push(normal[d]!);
    }
    for (let f = 0; f < nseg; f++) {
      for (let d = 0; d < 3; d++) framesU[f]!.push(pyRound(uPerFrame[f]![d]!, 5));
    }
    return vertBase.length / 3 - 1;
  };

  /** 各帧该节点的整行数据(各帧节点集应一致,缺即报错,不静默)。 */
  const nodeRows = (nid: number): Vec6[] =>
    frames.map((fr) => {
      const row = fr.get(nid);
      if (row === undefined) throw new Error(`帧缺节点 ${nid}(各帧节点集应一致)`);
      return row;
    });

  const buildFace = (fixAx: AxisName, sideMark: "max" | "min", aAx: AxisName, bAx: AxisName): FaceGeom => {
    const lattice = lat[fixAx]!;
    const fidx = AXI[fixAx], aidx = AXI[aAx], bidx = AXI[bAx];
    const fixVal = sideMark === "max" ? lattice.lo + sideOf(lattice) : lattice.lo;
    const sign = sideMark === "max" ? 1 : -1;
    const normal = [0, 0, 0];
    normal[fidx] = sign;
    const flip = CROSS[`${aAx},${bAx}`]! * sign < 0;       // 绕向修正判据(见模块 docstring)

    // 面上节点 → 顶点入表(键 = 原始面内坐标 round3)
    const key2vi = new Map<string, number>();
    for (const [nid, v] of first) {
      if (Math.abs(v[fidx] - fixVal) < 1e-4) {
        key2vi.set(keyAt(v[aidx], v[bidx]),
          addVert([v[0], v[1], v[2]],
            nodeRows(nid).map((u) => [u[3], u[4], u[5]]), normal));
      }
    }
    const vi = (a: number, b: number): number => {
      const idx = key2vi.get(keyAt(a, b));
      if (idx === undefined) {
        throw new Error(`面 ${fixAx}=${sideMark} 缺网格点 (${a},${b}),${HANDBOOK_HINT}`);
      }
      return idx;
    };

    const { cell, k } = lattice;
    const tris: number[] = [];
    for (let i = 0; i < k; i++) {
      for (let j = 0; j < k; j++) {
        const x0 = cell * i, x1 = cell * (i + 1), y0 = cell * j, y1 = cell * (j + 1);
        const h = cell / 2;
        const c = [vi(x0, y0), vi(x1, y0), vi(x1, y1), vi(x0, y1)];              // 4 角点
        const m = [vi(x0 + h, y0), vi(x1, y0 + h), vi(x0 + h, y1), vi(x0, y0 + h)]; // 4 棱中点
        // 虚拟面心:Serendipity 面心系数,从已入表的 round 后值计算再按口径 round
        const ctr = addVert(
          [0, 1, 2].map((d) => -0.25 * sumAt(vertBase, c, d) + 0.5 * sumAt(vertBase, m, d)),
          framesU.map((fu) =>
            [0, 1, 2].map((d) => -0.25 * sumAt(fu, c, d) + 0.5 * sumAt(fu, m, d))),
          normal);
        // 三角化 = 面心扇形 8 三角;flip 则每个三角第 2/3 顶点互换
        let celltris = [
          c[0]!, m[0]!, ctr, ctr, m[0]!, c[1]!,
          c[1]!, m[1]!, ctr, ctr, m[1]!, c[2]!,
          c[2]!, m[2]!, ctr, ctr, m[2]!, c[3]!,
          c[3]!, m[3]!, ctr, ctr, m[3]!, c[0]!,
        ];
        if (flip) {
          const flipped: number[] = [];
          for (let s = 0; s < celltris.length; s += 3) {
            flipped.push(celltris[s]!, celltris[s + 2]!, celltris[s + 1]!);
          }
          celltris = flipped;
        }
        tris.push(...celltris);
      }
    }
    // 网格线 = 角点格的行/列段
    const wire: number[] = [];
    for (let n = 0; n <= k; n++) {
      for (let s = 0; s < k; s++) {
        wire.push(vi(cell * s, cell * n), vi(cell * (s + 1), cell * n));
        wire.push(vi(cell * n, cell * s), vi(cell * n, cell * (s + 1)));
      }
    }
    return { tris, wire };
  };

  for (const [fixAx, sideMark, aAx, bAx] of FACES) faceGeoms.push(buildFace(fixAx, sideMark, aAx, bAx));
  return { vertBase, vertNorm, framesU, faces: faceGeoms };
}

/** 规则 hex20 六面体域构网:帧节点云 → 三角网格 + 网格线 + 阶段/色标/压深度量。
 *
 * 非规则格(任一轴检测失败)抛 Error(中文消息注明轴名与原因,并指引手册 §3)。
 * 逐语义移植自 build_viewer.py main(),数值口径 golden 对拍钉死,勿"改进"算法。 */
export function buildLatticeMesh(input: MeshInput): MeshData {
  const frames = input.frames;
  if (frames.length === 0) throw new Error("frames 为空:没有帧数据可构网");
  const first = frames[0]!;
  if (first.size === 0) throw new Error("首帧没有节点数据,无法检测规则格");
  const nseg = frames.length;

  const lat: Record<AxisName, AxisLattice> = {
    x: latticeFor(first, AXI.x, "x"),
    y: latticeFor(first, AXI.y, "y"),
    z: latticeFor(first, AXI.z, "z"),
  };
  const geometry = buildGeometry(frames, lat);

  return {
    meta: {
      side: Math.max(sideOf(lat.x!), sideOf(lat.y!), sideOf(lat.z!)),
      nseg,
      colorMax: colorMaxOf(geometry.framesU),
      results: roundResults(input.results),
    },
    vertBase: geometry.vertBase,
    vertNorm: geometry.vertNorm,
    framesU: geometry.framesU,
    faces: geometry.faces,
    stages: { ...buildStages(input, nseg), depth: depthSeries(frames) },
    mode: "lattice",
  };
}
