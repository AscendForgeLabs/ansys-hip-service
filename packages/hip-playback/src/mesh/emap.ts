/** emap 通用构网 — 手册 §3 档②:emap.csv 单元连接表 → 外壳 MeshData。
 *
 * 与 lattice(规则格反推)互补:只要上游随工件导出 emap.csv(SOLID186
 * 角点序,hex20 可带 12 个棱中点列),任意六面体网格都能构网,不要求规则域。
 *
 * 构造规则(与参考实现 build_viewer.py 逐条对应,见其模块 docstring):
 *   * 六面体面表:SOLID186 角点序 1..8(底面 1-2-3-4 逆时针、顶面 5-8-7-6
 *     对应、竖直棱 1-5/2-6/3-7/4-8),每单元 6 面 × 4 角点;面键 = 升序
 *     角点号,全网格出现 1 次 = 边界面(内部面被相邻两单元共享,出现 2 次)。
 *   * 二次单元(hasMidnodes)面的 4 个棱中点按 n9..n20 的棱映射取列;
 *     每面再加 1 个虚拟面心:位置与逐帧位移均按 8 节点 Serendipity 形函数
 *     在面心的系数 -0.25×Σ角点 + 0.5×Σ棱中点(由已量化 round4/round5 的
 *     角点/中点值线性组合后再量化 —— 与参考实现读回已存顶点缓冲同口径)。
 *   * 三角化 = 角点-中点-面心 8 三角扇(celltris 同构);线性单元面 = 2 三角。
 *   * 绕向:每面按 (v1−v0)×(v2−v0) 求几何法线,与(面心 − 所属单元中心)
 *     点积 < 0 则翻三角绕向并取反法线,保证外法线朝外(内翻面会被逐像素
 *     光照渲染成黑面 —— 参考实现实测踩坑)。
 *   * 顶点共享:角点/棱中点按节点号全局去重;虚拟面心每面一份;同一节点
 *     被多面引用时静态法线以首个使用的面为准(共享 BufferAttribute 口径)。
 *   * wire 网格线:线性面 = 4 条角点棱;二次面 = 角点→中点→角点两段
 *     (hex20 的网格线观感)。
 *
 * 向量算术与法线定向核抽在 src/mesh/vec3.ts(与 tet 皮肤构网共享);
 * tet 四面体分支见 src/mesh/tet.ts。
 */
import type { EmapTable, FrameNodes } from "../csv";
import { buildStages, colorMaxOf, depthSeries, round4, round5 } from "./common";
import type { FaceGeom, MeshData, MeshInput } from "./types";
import {
  bboxMaxSide,
  mean3,
  orientNormal,
  posOf,
  sub3,
  uOf,
  type Vec3,
} from "./vec3";

/** SOLID186 六面体面表(1 基角点号):底面、顶面、4 个侧面。 */
const HEX_FACES: ReadonlyArray<readonly [number, number, number, number]> = [
  [1, 2, 3, 4], [5, 6, 7, 8], [1, 2, 6, 5], [2, 3, 7, 6], [3, 4, 8, 7], [4, 1, 5, 8],
];

/** n9..n20 所在的棱(1 基角点对):底面棱 9-12、顶面棱 13-16、竖直棱 17-20。 */
const MID_EDGES: ReadonlyArray<readonly [number, number]> = [
  [1, 2], [2, 3], [3, 4], [4, 1],
  [5, 6], [6, 7], [7, 8], [8, 5],
  [1, 5], [2, 6], [3, 7], [4, 8],
];

/** 棱(升序角点对)→ 中点在单元节点数组中的 0 基下标(n9 → 8)。 */
const MID_COL_BY_EDGE: ReadonlyMap<string, number> = new Map(
  MID_EDGES.map(([a, b], i) => [a < b ? `${a}-${b}` : `${b}-${a}`, i + 8]),
);

/** 每个面沿角点序 4 条棱的中点下标(与 HEX_FACES 一一对应)。 */
const FACE_MID_COLS: ReadonlyArray<readonly [number, number, number, number]> =
  HEX_FACES.map(([a, b, c, d]) => [
    MID_COL_BY_EDGE.get(`${Math.min(a, b)}-${Math.max(a, b)}`)!,
    MID_COL_BY_EDGE.get(`${Math.min(b, c)}-${Math.max(b, c)}`)!,
    MID_COL_BY_EDGE.get(`${Math.min(c, d)}-${Math.max(c, d)}`)!,
    MID_COL_BY_EDGE.get(`${Math.min(d, a)}-${Math.max(d, a)}`)!,
  ]);

/** 边界面:角点/棱中点节点号(沿面序)+ 所属单元中心(绕向参照)。 */
interface BoundaryFace {
  corners: readonly number[];
  mids: readonly number[];
  elemCenter: Vec3;
}

/** 虚拟面心系数(8 节点 Serendipity 形函数在面心):-0.25×Σ角点 + 0.5×Σ棱中点。 */
const serendipityCenter = (corners: ReadonlyArray<Vec3>, mids: ReadonlyArray<Vec3>): Vec3 => [
  -0.25 * corners.reduce((s, v) => s + v[0], 0) + 0.5 * mids.reduce((s, v) => s + v[0], 0),
  -0.25 * corners.reduce((s, v) => s + v[1], 0) + 0.5 * mids.reduce((s, v) => s + v[1], 0),
  -0.25 * corners.reduce((s, v) => s + v[2], 0) + 0.5 * mids.reduce((s, v) => s + v[2], 0),
];

/** 校验单元表并收集边界面(面键出现 1 次;>2 次 = 非流形,按内部面处理不渲染)。 */
function collectBoundaryFaces(emap: EmapTable, first: FrameNodes): BoundaryFace[] {
  const want = emap.hasMidnodes ? 20 : 8;
  const byKey = new Map<string, BoundaryFace[]>();
  emap.elements.forEach((nodes, e) => {
    const elemNo = e + 1;
    if (nodes.length !== want) {
      throw new Error(`emap 单元 ${elemNo} 应有 ${want} 个节点,实际 ${nodes.length}`);
    }
    for (const n of nodes) {
      if (!first.has(n)) throw new Error(`emap 单元 ${elemNo} 引用了帧中不存在的节点 ${n}`);
    }
    const elemCenter = mean3(nodes.slice(0, 8).map((n) => posOf(first.get(n)!)));
    HEX_FACES.forEach((face, f) => {
      const corners = face.map((c) => nodes[c - 1]!);
      const mids = emap.hasMidnodes ? FACE_MID_COLS[f]!.map((col) => nodes[col]!) : [];
      const key = [...corners].sort((a, b) => a - b).join("-");
      let shared = byKey.get(key);
      if (shared === undefined) {
        shared = [];
        byKey.set(key, shared);
      }
      shared.push({ corners, mids, elemCenter });
    });
  });
  const boundary = [...byKey.values()]
    .filter((fs) => fs.length === 1)
    .map((fs) => fs[0]!);
  if (boundary.length === 0) {
    throw new Error("emap 未检出边界面:每个单元面都被相邻单元共享(重复单元或非流形网格)");
  }
  return boundary;
}

/** 二次面几何:角点-中点-面心 8 三角扇 + 角点→中点→角点网格线(celltris 同构)。 */
function quadFaceGeom(
  c: readonly number[],
  m: readonly number[],
  center: number,
  flip: boolean,
): FaceGeom {
  const tri = (a: number, b: number, cc: number): number[] => (flip ? [a, cc, b] : [a, b, cc]);
  return {
    tris: [
      ...tri(c[0]!, m[0]!, center), ...tri(center, m[0]!, c[1]!),
      ...tri(c[1]!, m[1]!, center), ...tri(center, m[1]!, c[2]!),
      ...tri(c[2]!, m[2]!, center), ...tri(center, m[2]!, c[3]!),
      ...tri(c[3]!, m[3]!, center), ...tri(center, m[3]!, c[0]!),
    ],
    wire: [
      c[0]!, m[0]!, m[0]!, c[1]!, c[1]!, m[1]!, m[1]!, c[2]!,
      c[2]!, m[2]!, m[2]!, c[3]!, c[3]!, m[3]!, m[3]!, c[0]!,
    ],
  };
}

/** 线性面几何:2 三角 + 4 条角点棱网格线。 */
function linearFaceGeom(c: readonly number[], flip: boolean): FaceGeom {
  const tri = (a: number, b: number, cc: number): number[] => (flip ? [a, cc, b] : [a, b, cc]);
  return {
    tris: [...tri(c[0]!, c[1]!, c[2]!), ...tri(c[0]!, c[2]!, c[3]!)],
    wire: [c[0]!, c[1]!, c[1]!, c[2]!, c[2]!, c[3]!, c[3]!, c[0]!],
  };
}

/** 顶点缓冲与面几何装配:节点号全局去重,虚拟面心每面一份。 */
function buildShell(
  boundary: BoundaryFace[],
  frames: ReadonlyArray<FrameNodes>,
): Pick<MeshData, "vertBase" | "vertNorm" | "framesU" | "faces"> {
  const first = frames[0]!;
  const vertBase: number[] = [];
  const vertNorm: number[] = [];
  const framesU: number[][] = Array.from({ length: frames.length }, () => []);
  const faces: FaceGeom[] = [];
  const vertByNode = new Map<number, number>();

  const addVertex = (pos: Vec3, uPerFrame: ReadonlyArray<Vec3>, normal: Vec3): number => {
    const idx = vertBase.length / 3;
    vertBase.push(round4(pos[0]), round4(pos[1]), round4(pos[2]));
    vertNorm.push(normal[0], normal[1], normal[2]);
    uPerFrame.forEach((u, f) => framesU[f]!.push(round5(u[0]), round5(u[1]), round5(u[2])));
    return idx;
  };
  const sharedVertexOf = (nodeId: number, normal: Vec3): number => {
    const existing = vertByNode.get(nodeId);
    if (existing !== undefined) return existing;   // 法线以首个使用该节点的面为准
    const idx = addVertex(
      posOf(first.get(nodeId)!),
      frames.map((fr) => {
        const row = fr.get(nodeId);
        if (row === undefined) {
          throw new Error(`帧缺节点 ${nodeId}(各帧节点集应一致,emap 连接表引用了它)`);
        }
        return uOf(row);
      }),
      normal,
    );
    vertByNode.set(nodeId, idx);
    return idx;
  };

  for (const face of boundary) {
    const cornerPos = face.corners.map((n) => posOf(first.get(n)!));
    const { normal, flip } = orientNormal(
      cornerPos,
      sub3(mean3(cornerPos), face.elemCenter),
      `emap 边界面(角点 ${[...face.corners].join(",")})`,
    );
    const cIdx = face.corners.map((n) => sharedVertexOf(n, normal));
    if (face.mids.length === 0) {
      faces.push(linearFaceGeom(cIdx, flip));
      continue;
    }
    const mIdx = face.mids.map((n) => sharedVertexOf(n, normal));
    const midPos = face.mids.map((n) => posOf(first.get(n)!));
    const centerU = frames.map((fr) =>
      serendipityCenter(
        face.corners.map((n) => uOf(fr.get(n)!)),
        face.mids.map((n) => uOf(fr.get(n)!)),
      ),
    );
    const centerIdx = addVertex(serendipityCenter(cornerPos, midPos), centerU, normal);
    faces.push(quadFaceGeom(cIdx, mIdx, centerIdx, flip));
  }
  return { vertBase, vertNorm, framesU, faces };
}

/** emap 通用构网:帧节点云 + emap.csv 单元连接表 → 外壳 MeshData(mode: "emap")。 */
export function buildEmapMesh(input: MeshInput & { emap: EmapTable }): MeshData {
  const frames = input.frames;
  if (frames.length === 0) {
    throw new Error("frames 为空:emap 构网至少需要一帧初始构型");
  }
  const boundary = collectBoundaryFaces(input.emap, frames[0]!);
  const shell = buildShell(boundary, frames);
  const nseg = frames.length;
  return {
    meta: {
      side: bboxMaxSide(frames[0]!),
      nseg,
      colorMax: colorMaxOf(shell.framesU),
      results: input.results ?? {},
    },
    vertBase: shell.vertBase,
    vertNorm: shell.vertNorm,
    framesU: shell.framesU,
    faces: shell.faces,
    stages: { ...buildStages(input, nseg), depth: depthSeries(frames) },
    mode: "emap",
  };
}
