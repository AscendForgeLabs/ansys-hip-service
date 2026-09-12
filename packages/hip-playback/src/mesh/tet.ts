/** tet 皮肤构网 — 手册 §3 档②的四面体分支:emap.csv 5/11 列(tet4/tet10)。
 *
 * 与 emap.ts(hex 分支,与离线参考实现 build_viewer.py 逐条对拍)同构:
 *   * 边界面提取:面键 = 升序 3 角点节点号(中点不入键——实测与入键等价),
 *     全网格恰出现 1 次 = 边界面;>2 次 = 非流形内部面,不渲染;
 *   * 绕向:实测 SOLID187 面表存储绕向 100% 向内,不可依赖 → 每面按几何法线
 *     (p1−p0)×(p2−p0) 与(角点均值 − 单元中心(4 角点均值))点积 < 0 则翻,
 *     定向核用 src/mesh/vec3.ts 的 orientNormal(与 emap 共享);
 *   * tet10 六节点面 = 4 三角子剖分(不用虚拟面心):位移列直接用节点真实
 *     逐帧值,零插值;tet4 面 = 1 三角;
 *   * wire 网格线:tet10 面 = 外边界 6 段(不画子剖分内线);tet4 = 3 条角点棱;
 *   * 顶点共享:按节点号全局去重(与 emap 同构,法线以首个使用面为准);
 *     tet 路径无虚拟顶点;
 *   * part 标注:epart.csv 侧车(单元号 → part 号)按 emap.elemIds(缺席按
 *     行序 e+1,csv.ts 接口注释承诺的回退)查表,缺行记 0;hex emap + epart
 *     由分发层(mesh/index.ts)显式报错。
 */
import type { EmapTable, EpartTable, FrameNodes } from "../csv";
import { buildStages, colorMaxOf, depthSeries } from "./common";
import type { FaceGeom, MeshData, MeshInput } from "./types";
import { bboxMaxSide, mean3, orientNormal, posOf, sub3, uOf, type Vec3 } from "./vec3";

/** SOLID187 四面体面表(1 基角点号;绕向由几何法线定,不依赖表序——实测存储绕向 100% 向内)。 */
const TET_FACES: ReadonlyArray<readonly [number, number, number]> = [
  [1, 2, 3], [1, 4, 2], [2, 4, 3], [3, 4, 1],
];

/** n5..n10 所在棱(1 基角点对):5=1-2、6=2-3、7=3-1、8=1-4、9=2-4、10=3-4
 * (官方 Element Reference + 全量实测 1e-13 级验证)。 */
const TET_MID_EDGES: ReadonlyArray<readonly [number, number]> = [
  [1, 2], [2, 3], [3, 1], [1, 4], [2, 4], [3, 4],
];

/** 棱(升序角点对)→ 中点在单元节点数组中的 0 基下标(n5 → 4)。 */
const TET_MID_COL_BY_EDGE: ReadonlyMap<string, number> = new Map(
  TET_MID_EDGES.map(([a, b], i) => [a < b ? `${a}-${b}` : `${b}-${a}`, i + 4]),
);

/** 每个面沿角点环 3 条棱的中点下标(与 TET_FACES 一一对应)。 */
const TET_FACE_MID_COLS: ReadonlyArray<readonly [number, number, number]> =
  TET_FACES.map(([a, b, c]) => [
    TET_MID_COL_BY_EDGE.get(`${Math.min(a, b)}-${Math.max(a, b)}`)!,
    TET_MID_COL_BY_EDGE.get(`${Math.min(b, c)}-${Math.max(b, c)}`)!,
    TET_MID_COL_BY_EDGE.get(`${Math.min(c, a)}-${Math.max(c, a)}`)!,
  ]);

/** tet 边界面:角点/棱中点节点号(沿面环序)+ 单元中心(绕向参照)+ 单元索引。 */
interface TetBoundaryFace {
  corners: readonly number[];
  mids: readonly number[];
  elemCenter: Vec3;
  elemNo: number;                        // 0 基单元索引(part 缺席回退 e+1 用)
}

/** 校验单元表并收集边界面(面键出现 1 次;>2 次 = 非流形,按内部面处理不渲染)。 */
function collectTetBoundaryFaces(emap: EmapTable, first: FrameNodes): TetBoundaryFace[] {
  const want = emap.hasMidnodes ? 10 : 4;
  const byKey = new Map<string, TetBoundaryFace[]>();
  emap.elements.forEach((nodes, e) => {
    if (nodes.length !== want) {
      throw new Error(`tet 单元 ${e + 1} 应有 ${want} 个节点,实际 ${nodes.length}`);
    }
    for (const n of nodes) {
      if (!first.has(n)) throw new Error(`tet 单元 ${e + 1} 引用了帧中不存在的节点 ${n}`);
    }
    const elemCenter = mean3(nodes.slice(0, 4).map((n) => posOf(first.get(n)!)));
    TET_FACES.forEach((face, f) => {
      const corners = face.map((c) => nodes[c - 1]!);
      const mids = emap.hasMidnodes ? TET_FACE_MID_COLS[f]!.map((col) => nodes[col]!) : [];
      const key = [...corners].sort((a, b) => a - b).join("-");
      let shared = byKey.get(key);
      if (shared === undefined) {
        shared = [];
        byKey.set(key, shared);
      }
      shared.push({ corners, mids, elemCenter, elemNo: e });
    });
  });
  const boundary = [...byKey.values()]
    .filter((fs) => fs.length === 1)
    .map((fs) => fs[0]!);
  if (boundary.length === 0) {
    throw new Error("tet 未检出边界面:每个单元面都被相邻单元共享(重复单元或非流形网格)");
  }
  return boundary;
}

/** tet10 六节点面:4 三角子剖分 + 外边界 6 段网格线(不画子剖分内线)。 */
function tet10FaceGeom(c: readonly number[], m: readonly number[], flip: boolean): FaceGeom {
  const tri = (a: number, b: number, cc: number): number[] => (flip ? [a, cc, b] : [a, b, cc]);
  return {
    tris: [
      ...tri(c[0]!, m[0]!, m[2]!),
      ...tri(m[0]!, c[1]!, m[1]!),
      ...tri(m[1]!, c[2]!, m[2]!),
      ...tri(m[0]!, m[1]!, m[2]!),
    ],
    wire: [
      c[0]!, m[0]!, m[0]!, c[1]!, c[1]!, m[1]!,
      m[1]!, c[2]!, c[2]!, m[2]!, m[2]!, c[0]!,
    ],
  };
}

/** tet4 线性面:1 三角 + 3 条角点棱网格线。 */
function tet4FaceGeom(c: readonly number[], flip: boolean): FaceGeom {
  const tri = (a: number, b: number, cc: number): number[] => (flip ? [a, cc, b] : [a, b, cc]);
  return {
    tris: [...tri(c[0]!, c[1]!, c[2]!)],
    wire: [c[0]!, c[1]!, c[1]!, c[2]!, c[2]!, c[0]!],
  };
}

/** 顶点缓冲与面几何装配:节点号全局去重(法线以首个使用面为准),无虚拟顶点。 */
function buildTetShell(
  boundary: TetBoundaryFace[],
  frames: ReadonlyArray<FrameNodes>,
  epart?: EpartTable,
  elemIds?: readonly number[],
): Pick<MeshData, "vertBase" | "vertNorm" | "framesU" | "faces"> {
  const first = frames[0]!;
  const vertBase: number[] = [];
  const vertNorm: number[] = [];
  const framesU: number[][] = Array.from({ length: frames.length }, () => []);
  const faces: FaceGeom[] = [];
  const vertByNode = new Map<number, number>();

  const sharedVertexOf = (nodeId: number, normal: Vec3): number => {
    const existing = vertByNode.get(nodeId);
    if (existing !== undefined) return existing;   // 法线以首个使用该节点的面为准
    const idx = vertBase.length / 3;
    vertBase.push(...posOf(first.get(nodeId)!));
    vertNorm.push(normal[0], normal[1], normal[2]);
    frames.forEach((fr, f) => {
      const row = fr.get(nodeId);
      if (row === undefined) {
        throw new Error(`帧缺节点 ${nodeId}(各帧节点集应一致,tet 连接表引用了它)`);
      }
      const u = uOf(row);
      framesU[f]!.push(u[0], u[1], u[2]);
    });
    vertByNode.set(nodeId, idx);
    return idx;
  };

  for (const face of boundary) {
    const cornerPos = face.corners.map((n) => posOf(first.get(n)!));
    const { normal, flip } = orientNormal(
      cornerPos,
      sub3(mean3(cornerPos), face.elemCenter),
      `tet 边界面(角点 ${[...face.corners].join(",")})`,
    );
    const cIdx = face.corners.map((n) => sharedVertexOf(n, normal));
    const geom = face.mids.length === 0
      ? tet4FaceGeom(cIdx, flip)
      : tet10FaceGeom(cIdx, face.mids.map((n) => sharedVertexOf(n, normal)), flip);
    if (epart === undefined) {
      faces.push(geom);
      continue;
    }
    const part = epart.byElem.get(elemIds?.[face.elemNo] ?? face.elemNo + 1) ?? 0;
    faces.push({ ...geom, part });
  }
  return { vertBase, vertNorm, framesU, faces };
}

/** tet 皮肤构网:帧节点云 + emap.csv 四面体连接表(5/11 列)→ 外壳 MeshData。
 * meta/stages/colorMax/depth 与 emap(hex)分支逐值同口径(mesh/common.ts 装配)。 */
export function buildTetMesh(input: MeshInput & { emap: EmapTable; epart?: EpartTable }): MeshData {
  const frames = input.frames;
  if (frames.length === 0) {
    throw new Error("frames 为空:tet 构网至少需要一帧初始构型");
  }
  const boundary = collectTetBoundaryFaces(input.emap, frames[0]!);
  const shell = buildTetShell(boundary, frames, input.epart, input.emap.elemIds);
  const nseg = frames.length;
  const parts = [...new Set(shell.faces.map((f) => f.part ?? 0))].sort((a, b) => a - b);
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
    ...(input.epart ? { parts } : {}),
  };
}
