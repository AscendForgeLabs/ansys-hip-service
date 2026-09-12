/** tet 皮肤构网:合成单 tet10/tet4、歪单元棱映射钉测、共享面内部剔除、
 * 绕向几何重定向、epart 部件标注与全部错误路径 + 装配口径(仿 emap.test.ts 风格)。
 * 顶点计数依据:tet 路径无虚拟顶点,单 tet10 引用 10 节点 → 顶点恰 10。 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { parseEmapCsv, parseFrameCsv } from "../src/csv";
import type { EmapTable, EpartTable, FrameNodes, Vec6 } from "../src/csv";
import { buildMeshData } from "../src/mesh";
import { round5 } from "../src/mesh/common";
import { buildTetMesh } from "../src/mesh/tet";
import type { FaceGeom, MeshData } from "../src/mesh/types";

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

// hex+epart 分发报错用例:借合成 fixture 的真实 hex emap(错误在构网前抛出)
const ART = "fixtures/synthetic/artifacts/";
const hexFrames = [1, 2, 3].map((n) => parseFrameCsv(load(`${ART}frame_${n}.csv`)));
const hexEmap = parseEmapCsv(load(`${ART}emap.csv`));

// ---- 测试侧小型向量/查找工具(独立于实现,只读 MeshData 平铺数组) ----
const vert = (m: MeshData, i: number): number[] => [
  m.vertBase[3 * i]!,
  m.vertBase[3 * i + 1]!,
  m.vertBase[3 * i + 2]!,
];
const vsub = (a: readonly number[], b: readonly number[]): number[] => [
  a[0]! - b[0]!,
  a[1]! - b[1]!,
  a[2]! - b[2]!,
];
const vcross = (a: readonly number[], b: readonly number[]): number[] => [
  a[1]! * b[2]! - a[2]! * b[1]!,
  a[2]! * b[0]! - a[0]! * b[2]!,
  a[0]! * b[1]! - a[1]! * b[0]!,
];
const vdot = (a: readonly number[], b: readonly number[]): number =>
  a[0]! * b[0]! + a[1]! * b[1]! + a[2]! * b[2]!;
const totalTris = (m: MeshData): number =>
  m.faces.reduce((sum, f) => sum + f.tris.length / 3, 0);
/** 面顶点集合的质心(用于"面心 − 体心"朝向判据)。 */
const faceCentroid = (m: MeshData, tris: readonly number[]): number[] => {
  const uniq = [...new Set(tris)];
  return [0, 1, 2].map((d) =>
    uniq.reduce((s, i) => s + m.vertBase[3 * i + d]!, 0) / uniq.length,
  );
};
const countVertsAt = (m: MeshData, p: readonly number[]): number => {
  let count = 0;
  for (let i = 0; i < m.vertBase.length / 3; i += 1) {
    if (
      m.vertBase[3 * i] === p[0] && m.vertBase[3 * i + 1] === p[1] &&
      m.vertBase[3 * i + 2] === p[2]
    ) {
      count += 1;
    }
  }
  return count;
};
const findVert = (m: MeshData, p: readonly number[]): number => {
  for (let i = 0; i < m.vertBase.length / 3; i += 1) {
    if (
      m.vertBase[3 * i] === p[0] && m.vertBase[3 * i + 1] === p[1] &&
      m.vertBase[3 * i + 2] === p[2]
    ) {
      return i;
    }
  }
  throw new Error(`测试内部错误:未找到顶点 [${p.join(",")}]`);
};

// ---- 手造帧/连接表助手(字面量 emap 不带 elemIds → 天然覆盖 e+1 回退) ----
type Pos = readonly [number, number, number];
const makeFrames = (
  pos: Readonly<Record<number, Pos>>,
  uOf: (k: number, f: number) => Pos = () => [0, 0, 0],
  nframes = 1,
): FrameNodes[] =>
  Array.from({ length: nframes }, (_, fi) => {
    const fr: FrameNodes = new Map<number, Vec6>();
    for (const [k, p] of Object.entries(pos)) {
      const u = uOf(Number(k), fi + 1);
      fr.set(Number(k), [p[0]!, p[1]!, p[2]!, u[0]!, u[1]!, u[2]!]);
    }
    return fr;
  });
const tet10Emap = (nodes: readonly number[]): EmapTable => ({
  cell: "tet",
  elements: [[...nodes]],
  hasMidnodes: true,
});

describe("buildTetMesh · 单 tet10", () => {
  // 角四面体 + 棱中点取几何中点(计数/闭合与几何无关,用最简正 tet)
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0], 4: [0, 0, 4],
    5: [2, 0, 0], 6: [2, 2, 0], 7: [0, 2, 0], 8: [0, 0, 2], 9: [2, 0, 2], 10: [0, 2, 2],
  };
  const m = buildTetMesh({
    frames: makeFrames(pos),
    emap: tet10Emap([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
  });

  it("4 面全边界 × 每面 4 三角;顶点恰 10(无虚拟面心)", () => {
    expect(m.faces).toHaveLength(4);
    for (const face of m.faces) expect(face.tris).toHaveLength(12);
    expect(totalTris(m)).toBe(16);
    expect(m.vertBase).toHaveLength(10 * 3);
    for (const k of Object.keys(pos)) {
      expect(countVertsAt(m, pos[Number(k)]!)).toBe(1);
    }
    expect(m.parts).toBeUndefined();          // 无 epart 输入 → 部件字段缺席
    for (const face of m.faces) expect(face.part).toBeUndefined();
  });

  it("wire:每面 6 段闭合(只画外边界,不画子剖分内线)", () => {
    for (const face of m.faces) {
      expect(face.wire).toHaveLength(12);
      for (let k = 0; k < 6; k += 1) {
        expect(face.wire[2 * k + 1]).toBe(face.wire[(2 * k + 2) % 12]);
      }
    }
  });
});

describe("buildTetMesh · 歪 tet10 棱映射", () => {
  // 棱中点全部偏离几何中点 → 列映射放错立刻可辨;角点序 (1,2,3) 面朝外(不走翻转分支)
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0.6], 3: [0.7, 3.5, 0.9], 4: [0.9, 1.1, -2.7],
    5: [2.4, -0.3, 0.2], 6: [2.3, 1.6, 1.0], 7: [0.3, 1.9, 0.3],
    8: [0.6, 0.4, -1.5], 9: [2.6, 0.5, -0.8], 10: [0.9, 2.3, -0.9],
  };
  const m = buildTetMesh({
    frames: makeFrames(pos),
    emap: tet10Emap([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
  });
  const vi = (k: number): number => findVert(m, pos[k]!);
  const faceOf = (cornerKs: readonly number[]): FaceGeom => {
    const want = cornerKs.map(vi);
    const hit = m.faces.filter((f) => want.every((i) => f.tris.includes(i)));
    expect(hit).toHaveLength(1);
    return hit[0]!;
  };

  it("前提:面 (1,2,3) 原始角点序几何法线朝外(不翻转,子三角序可精确断言)", () => {
    const natural = vcross(vsub(pos[2]!, pos[1]!), vsub(pos[3]!, pos[1]!));
    const cornerMean = [0, 1, 2].map((d) =>
      (pos[1]![d]! + pos[2]![d]! + pos[3]![d]!) / 3
    );
    const bodyMean = [0, 1, 2].map((d) =>
      (pos[1]![d]! + pos[2]![d]! + pos[3]![d]! + pos[4]![d]!) / 4
    );
    expect(vdot(natural, vsub(cornerMean, bodyMean))).toBeGreaterThan(0);
  });

  it("面 (1,2,3) 子三角 (c0,m0,m2) = (n1,n5,n7):m0=节点5(棱1-2)、m2=节点7(棱3-1)", () => {
    const f = faceOf([1, 2, 3]);
    expect(f.tris.slice(0, 3)).toEqual([vi(1), vi(5), vi(7)]);   // (c0,m0,m2)
    expect(f.tris.slice(3, 6)).toEqual([vi(5), vi(2), vi(6)]);   // (m0,c1,m1)
    expect(f.tris.slice(6, 9)).toEqual([vi(6), vi(3), vi(7)]);   // (m1,c2,m2)
    expect(f.tris.slice(9, 12)).toEqual([vi(5), vi(6), vi(7)]);  // (m0,m1,m2)
  });
});

describe("buildTetMesh · 两 tet10 共享面", () => {
  // 单元 A(1,2,3,4) 与单元 B(1,2,3,5) 共享面 (1,2,3)(含棱中点 6,7,8):
  // 内部面键出现 2 次被剔除,余 6 面全边界。
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0], 4: [1, 1, -3], 5: [1, 1, 3],
    6: [2, 0, 0], 7: [2, 2, 0], 8: [0, 2, 0],
    9: [0.5, 0.5, -1.5], 10: [2.5, 0.5, -1.5], 11: [0.5, 2.5, -1.5],
    12: [0.5, 0.5, 1.5], 13: [2.5, 0.5, 1.5], 14: [0.5, 2.5, 1.5],
  };
  const emap: EmapTable = {
    cell: "tet",
    elements: [
      [1, 2, 3, 4, 6, 7, 8, 9, 10, 11],
      [1, 2, 3, 5, 6, 7, 8, 12, 13, 14],
    ],
    hasMidnodes: true,
  };
  const m = buildTetMesh({ frames: makeFrames(pos), emap });

  it("6 边界面 × 4 三角 = 24;顶点 = 14 节点全局去重", () => {
    expect(m.faces).toHaveLength(6);
    expect(totalTris(m)).toBe(24);
    expect(m.vertBase).toHaveLength(14 * 3);
  });

  it("非流形防御:三个 tet 共享同一面(面键出现 3 次)→ 该面不渲染,其余 9 面正常", () => {
    // 单元 A(顶点 4)/B(顶点 5)/C(顶点 15)都骑在面 (1,2,3) 上:
    // 共享面键 "1-2-3" 出现 3 次 > 2 → 按非流形内部面剔除;
    // 三单元各自余下 3 面恰现 1 次,全为边界面。
    const pos: Readonly<Record<number, Pos>> = {
      1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0],
      4: [1, 1, -3], 5: [1, 1, 3], 15: [-3, 1, 1],
      6: [2, 0, 0], 7: [2, 2, 0], 8: [0, 2, 0],
      9: [0.5, 0.5, -1.5], 10: [2.5, 0.5, -1.5], 11: [0.5, 2.5, -1.5],
      12: [0.5, 0.5, 1.5], 13: [2.5, 0.5, 1.5], 14: [0.5, 2.5, 1.5],
      16: [-1.5, 0.5, 0.5], 17: [-1.5, 1.2, 0.3], 18: [-1.2, 2, 0.5],
    };
    const emap3: EmapTable = {
      cell: "tet",
      elements: [
        [1, 2, 3, 4, 6, 7, 8, 9, 10, 11],
        [1, 2, 3, 5, 6, 7, 8, 12, 13, 14],
        [1, 2, 3, 15, 6, 7, 8, 16, 17, 18],
      ],
      hasMidnodes: true,
    };
    const m3 = buildTetMesh({ frames: makeFrames(pos), emap: emap3 });

    expect(m3.faces).toHaveLength(9);              // 3 单元 × 3 面,共享面被剔除
    expect(totalTris(m3)).toBe(36);
    // 共享面(含角点 1/2/3 的面)不存在于输出
    const v1 = findVert(m3, pos[1]!), v2 = findVert(m3, pos[2]!), v3 = findVert(m3, pos[3]!);
    for (const face of m3.faces) {
      expect(face.tris.includes(v1) && face.tris.includes(v2) && face.tris.includes(v3)).toBe(false);
    }
    // 共享面的棱中点 6/7/8 仍被相邻边界面引用 → 顶点恰 18 节点全局去重
    expect(countVertsAt(m3, pos[6]!)).toBe(1);
    expect(countVertsAt(m3, pos[7]!)).toBe(1);
    expect(countVertsAt(m3, pos[8]!)).toBe(1);
    expect(m3.vertBase).toHaveLength(18 * 3);
  });
});

describe("buildTetMesh · 绕向定向", () => {
  // 同歪几何但节点 2/3 互换 → 面表原始存储序朝内(实测 SOLID187 存储绕向不可依赖,
  // 构网必须按几何法线 × (角点均值 − 体心) 重定向)。
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [0.7, 3.5, 0.9], 3: [4, 0, 0.6], 4: [0.9, 1.1, -2.7],
    5: [0.3, 1.9, 0.3], 6: [2.3, 1.6, 1.0], 7: [2.4, -0.3, 0.2],
    8: [0.6, 0.4, -1.5], 9: [0.9, 2.3, -0.9], 10: [2.6, 0.5, -0.8],
  };
  const m = buildTetMesh({
    frames: makeFrames(pos),
    emap: tet10Emap([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
  });
  const bodyCtr = [0, 1, 2].map((d) =>
    (pos[1]![d]! + pos[2]![d]! + pos[3]![d]! + pos[4]![d]!) / 4
  );

  it("前提:面 (1,2,3) 原始角点序几何法线朝内(翻转分支必经)", () => {
    const natural = vcross(vsub(pos[2]!, pos[1]!), vsub(pos[3]!, pos[1]!));
    const cornerMean = [0, 1, 2].map((d) =>
      (pos[1]![d]! + pos[2]![d]! + pos[3]![d]!) / 3
    );
    expect(vdot(natural, vsub(cornerMean, bodyCtr))).toBeLessThan(0);
  });

  it("输出:每面首三角法线与(面心 − 体心)同向(全部重定向为外法线)", () => {
    for (const face of m.faces) {
      const n = vcross(
        vsub(vert(m, face.tris[1]!), vert(m, face.tris[0]!)),
        vsub(vert(m, face.tris[2]!), vert(m, face.tris[0]!)),
      );
      expect(vdot(n, vsub(faceCentroid(m, face.tris), bodyCtr))).toBeGreaterThan(0);
    }
  });
});

describe("buildTetMesh · epart 部件标注", () => {
  // 两个不相邻 tet4:单元号 10/20(elemIds 显式)→ part 2/1;每单元 4 面全边界
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [1, 0, 0], 3: [0, 1, 0], 4: [0, 0, 1],
    5: [5, 0, 0], 6: [6, 0, 0], 7: [5, 1, 0], 8: [5, 0, 1],
  };
  const emap: EmapTable = {
    cell: "tet",
    elements: [[1, 2, 3, 4], [5, 6, 7, 8]],
    hasMidnodes: false,
    elemIds: [10, 20],
  };
  const epartOf = (pairs: ReadonlyArray<readonly [number, number]>): EpartTable => ({
    byElem: new Map(pairs),
  });

  it("faces[].part 按单元号标注;parts 升序去重", () => {
    const m = buildTetMesh({
      frames: makeFrames(pos),
      emap,
      epart: epartOf([[10, 2], [20, 1]]),
    });
    expect(m.faces.slice(0, 4).map((f) => f.part)).toEqual([2, 2, 2, 2]);
    expect(m.faces.slice(4).map((f) => f.part)).toEqual([1, 1, 1, 1]);
    expect(m.parts).toEqual([1, 2]);
  });

  it("epart 缺行 → part 0(仍入 parts)", () => {
    const m = buildTetMesh({
      frames: makeFrames(pos),
      emap,
      epart: epartOf([[10, 2]]),
    });
    expect(m.faces.slice(0, 4).map((f) => f.part)).toEqual([2, 2, 2, 2]);
    expect(m.faces.slice(4).map((f) => f.part)).toEqual([0, 0, 0, 0]);
    expect(m.parts).toEqual([0, 2]);
  });

  it("epart 含 emap 外单元号 → 忽略", () => {
    const m = buildTetMesh({
      frames: makeFrames(pos),
      emap,
      epart: epartOf([[10, 2], [20, 1], [999, 7]]),
    });
    expect(m.parts).toEqual([1, 2]);
  });

  it("elemIds 缺席 → 按行序 e+1 回退(csv.ts 接口注释承诺)", () => {
    const noIds: EmapTable = {
      cell: "tet",
      elements: [[1, 2, 3, 4], [5, 6, 7, 8]],
      hasMidnodes: false,
    };
    const m = buildTetMesh({
      frames: makeFrames(pos),
      emap: noIds,
      epart: epartOf([[1, 5], [2, 3]]),
    });
    expect(m.faces.slice(0, 4).map((f) => f.part)).toEqual([5, 5, 5, 5]);
    expect(m.faces.slice(4).map((f) => f.part)).toEqual([3, 3, 3, 3]);
    expect(m.parts).toEqual([3, 5]);
  });

  it("elemIds 长度与单元数不一致 → 显式报错(不静默回退 e+1 混编号)", () => {
    const badIds: EmapTable = {
      cell: "tet",
      elements: [[1, 2, 3, 4], [5, 6, 7, 8]],
      hasMidnodes: false,
      elemIds: [10, 20, 30],
    };
    expect(() =>
      buildTetMesh({
        frames: makeFrames(pos),
        emap: badIds,
        epart: epartOf([[10, 1]]),
      }),
    ).toThrow(/elemIds 长度 3 与单元数 2 不一致/);
  });

  it("hex emap + epart → 分发层显式报错(不静默忽略)", () => {
    expect(() =>
      buildMeshData({ frames: hexFrames, emap: hexEmap, epart: epartOf([[1, 1]]) }),
    ).toThrow(/epart 部件分组当前仅支持 tet 皮肤/);
  });

  it("epart 无 emap → 分发层显式报错(不静默走规则格反推)", () => {
    expect(() =>
      buildMeshData({ frames: hexFrames, epart: epartOf([[1, 1]]) }),
    ).toThrow(/epart 需与 emap.csv 同供/);
  });
});

describe("buildTetMesh · tet4 线性单元", () => {
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0], 4: [0, 0, 4],
  };
  const m = buildTetMesh({
    frames: makeFrames(pos),
    emap: { cell: "tet", elements: [[1, 2, 3, 4]], hasMidnodes: false },
  });

  it("4 面全边界 × 每面 1 三角;顶点恰 4", () => {
    expect(m.faces).toHaveLength(4);
    for (const face of m.faces) expect(face.tris).toHaveLength(3);
    expect(totalTris(m)).toBe(4);
    expect(m.vertBase).toHaveLength(4 * 3);
  });

  it("wire = 3 条角点棱的闭合折线", () => {
    for (const face of m.faces) {
      expect(face.wire).toHaveLength(6);
      for (let k = 0; k < 3; k += 1) {
        expect(face.wire[2 * k + 1]).toBe(face.wire[(2 * k + 2) % 6]);
      }
    }
  });
});

describe("buildTetMesh · 错误路径", () => {
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0], 4: [0, 0, 4],
    5: [2, 0, 0], 6: [2, 2, 0], 7: [0, 2, 0], 8: [0, 0, 2], 9: [2, 0, 2], 10: [0, 2, 2],
  };

  it("引用帧中不存在的节点 → Error 带单元号与节点号", () => {
    expect(() =>
      buildTetMesh({
        frames: makeFrames(pos),
        emap: tet10Emap([1, 2, 3, 4, 5, 6, 7, 8, 9, 999]),
      }),
    ).toThrow(/tet 单元 1 引用了帧中不存在的节点 999/);
  });

  it("全部单元面被共享(重复单元)→ 无边界面 Error", () => {
    const dup: EmapTable = {
      cell: "tet",
      elements: [
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
      ],
      hasMidnodes: true,
    };
    expect(() => buildTetMesh({ frames: makeFrames(pos), emap: dup }))
      .toThrow(/tet 未检出边界面/);
  });

  it("退化面(角点重合,法线为零向量)→ 无法定向 Error", () => {
    expect(() =>
      buildTetMesh({
        frames: makeFrames(pos),
        emap: tet10Emap([1, 1, 3, 4, 5, 6, 7, 8, 9, 10]),
      }),
    ).toThrow(/退化/);
  });

  it("单元节点数与档位不符 → Error(tet10 应 10 / tet4 应 4)", () => {
    const five = [1, 2, 3, 4, 5];
    expect(() =>
      buildTetMesh({
        frames: makeFrames(pos),
        emap: { cell: "tet", elements: [five], hasMidnodes: true },
      }),
    ).toThrow(/tet 单元 1 应有 10 个节点,实际 5/);
    expect(() =>
      buildTetMesh({
        frames: makeFrames(pos),
        emap: { cell: "tet", elements: [five], hasMidnodes: false },
      }),
    ).toThrow(/tet 单元 1 应有 4 个节点,实际 5/);
  });

  it("frames 为空 → Error", () => {
    expect(() =>
      buildTetMesh({ frames: [], emap: tet10Emap([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) }),
    ).toThrow(/frames 为空/);
  });
});

describe("buildTetMesh · 装配口径(与 emap 同构)", () => {
  const pos: Readonly<Record<number, Pos>> = {
    1: [0, 0, 0], 2: [4, 0, 0], 3: [0, 4, 0], 4: [0, 0, 4],
  };
  const frames = makeFrames(pos, (k, f) => [0.03 * k * f, -0.04 * k * f, 0], 2);
  const m = buildTetMesh({
    frames,
    emap: { cell: "tet", elements: [[1, 2, 3, 4]], hasMidnodes: false },
    results: { dent_dep: 1.5 },
  });

  it("frames 逐帧位移 = 节点真实逐帧值 round5(零插值);meta.colorMax 手算 = 0.4", () => {
    expect(m.framesU).toHaveLength(2);
    for (const fu of m.framesU) expect(fu).toHaveLength(4 * 3);
    const vi = findVert(m, pos[2]!);
    frames.forEach((fr, f) => {
      const u = fr.get(2)!;
      expect(m.framesU[f]!.slice(3 * vi, 3 * vi + 3)).toEqual([
        round5(u[3]),
        round5(u[4]),
        round5(u[5]),
      ]);
    });
    // max|u| = 0.05 × k × f 的最大值(k=4, f=2)= 0.4
    expect(m.meta.colorMax).toBe(0.4);
  });

  it("meta.side / nseg / results 透传;stages 走兜底(MESH,F1,F2)且压深一致", () => {
    expect(m.mode).toBe("emap");
    expect(m.meta.side).toBe(4);
    expect(m.meta.nseg).toBe(2);
    expect(m.meta.results).toEqual({ dent_dep: 1.5 });
    expect(m.stages.label).toEqual(["MESH", "F1", "F2"]);
    expect(m.stages.time).toEqual([0, 0, 0]);
    // 逐帧 |min uy| = 0.04 × 4 × f
    expect(m.stages.depth).toEqual([0, 0.16, 0.32]);
  });
});
