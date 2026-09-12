/** emap 通用构网:合成 2×2×2 hex20 立方的面表/计数/绕向/位移/装配,
 * 手造歪单单元钉死虚拟面心系数与绕向翻转,外加全部错误路径。
 * 顶点计数依据:hex20(serendipity)无面心节点,emap 只引用角点(全偶索引)
 * 与棱中点(恰一奇索引)→ 外壳被引用节点 = 26 + 48 = 74(面心位置处的
 * 24 个表面节点不在单元连接表里,由虚拟面心顶点补位)。 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  parseEmapCsv,
  parseFrameCsv,
  parseProgressCsv,
  parseResultsCsv,
} from "../src/csv";
import type { EmapTable, Vec6 } from "../src/csv";
import { round5 } from "../src/mesh/common";
import { buildEmapMesh } from "../src/mesh/emap";
import type { MeshData } from "../src/mesh/types";

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

const ART = "fixtures/synthetic/artifacts/";
const frames = [1, 2, 3].map((n) => parseFrameCsv(load(`${ART}frame_${n}.csv`)));
const emap = parseEmapCsv(load(`${ART}emap.csv`));
const stageRows = parseProgressCsv(load(`${ART}progress.csv`));

const buildFixture = (): MeshData =>
  buildEmapMesh({
    frames,
    emap,
    stageLabels: stageRows.map((r) => r.label),
    stageTimes: stageRows.map((r) => r.time),
    results: parseResultsCsv(load(`${ART}results.csv`)),
  });

// ---- 测试侧小型向量工具(独立于实现,只读 MeshData 平铺数组) ----
const vert = (m: MeshData, i: number): number[] => [
  m.vertBase[3 * i]!,
  m.vertBase[3 * i + 1]!,
  m.vertBase[3 * i + 2]!,
];
const normAt = (m: MeshData, i: number): number[] => [
  m.vertNorm[3 * i]!,
  m.vertNorm[3 * i + 1]!,
  m.vertNorm[3 * i + 2]!,
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
/** 扇形三角的扇心 = 出现在全部 8 个三角里的顶点(不依赖三角内部顺序)。 */
const fanCenterOf = (tris: readonly number[]): number => {
  const counts = new Map<number, number>();
  for (const idx of tris) counts.set(idx, (counts.get(idx) ?? 0) + 1);
  const centers = [...counts.entries()].filter(([, n]) => n === 8).map(([i]) => i);
  expect(centers).toHaveLength(1);
  return centers[0]!;
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
/** 边界面在立方 6 个侧面上的分布(角点+中点+面心共面判定)。 */
const sideCounts = (m: MeshData): Record<string, number> => {
  const counts: Record<string, number> = {};
  for (const face of m.faces) {
    const ps = [...new Set(face.tris)].map((i) => vert(m, i));
    let side = "?";
    for (let d = 0; d < 3; d += 1) {
      const val = ps[0]![d]!;
      if (ps.every((p) => p[d]! === val) && (val === 0 || val === 10)) {
        side = "xyz"[d]! + val;
        break;
      }
    }
    counts[side] = (counts[side] ?? 0) + 1;
  }
  return counts;
};

describe("buildEmapMesh · 合成 2×2×2 hex20 立方", () => {
  it("边界面 = 6 大面 × 每面 2×2 单元面 = 24,均匀分布在 6 个侧面", () => {
    const m = buildFixture();
    expect(m.faces).toHaveLength(24);
    expect(sideCounts(m)).toEqual({ x0: 4, x10: 4, y0: 4, y10: 4, z0: 4, z10: 4 });
  });

  it("二次:每面 8 三角扇(总 192);顶点 = 74 共享壳节点 + 24 虚拟面心 = 98", () => {
    const m = buildFixture();
    for (const face of m.faces) expect(face.tris).toHaveLength(24);
    expect(totalTris(m)).toBe(192);
    expect(m.vertBase).toHaveLength(98 * 3);
    expect(m.vertNorm).toHaveLength(98 * 3);
    expect(m.framesU).toHaveLength(3);
    for (const fu of m.framesU) expect(fu).toHaveLength(98 * 3);
  });

  it("顶点去重 + 位移一致:同一节点全局仅一份顶点,逐帧位移 = 帧 CSV 的 ux,uy,uz(round5)", () => {
    const m = buildFixture();
    for (const id of [73, 2, 125]) {          // 角点(5,10,5)/棱中点(2.5,0,0)/角点(10,10,10)
      const [x, y, z] = frames[0]!.get(id)!;
      expect(countVertsAt(m, [x, y, z])).toBe(1);
      const vi = findVert(m, [x, y, z]);
      frames.forEach((fr, f) => {
        const u = fr.get(id)!;
        expect(m.framesU[f]!.slice(3 * vi, 3 * vi + 3)).toEqual([
          round5(u[3]),
          round5(u[4]),
          round5(u[5]),
        ]);
      });
    }
  });

  it("绕向:每面首三角几何法线与(面心 − 立方中心)点积 > 0(外法线朝外)", () => {
    const m = buildFixture();
    for (const face of m.faces) {
      const n = vcross(
        vsub(vert(m, face.tris[1]!), vert(m, face.tris[0]!)),
        vsub(vert(m, face.tris[2]!), vert(m, face.tris[0]!)),
      );
      expect(vdot(n, vsub(faceCentroid(m, face.tris), [5, 5, 5]))).toBeGreaterThan(0);
    }
  });

  it("vertNorm:全部单位向量;虚拟面心的静态法线与(面心 − 立方中心)同向", () => {
    const m = buildFixture();
    for (let i = 0; i < m.vertNorm.length / 3; i += 1) {
      expect(Math.hypot(...normAt(m, i))).toBeCloseTo(1, 6);
    }
    for (const face of m.faces) {
      const ci = fanCenterOf(face.tris);
      expect(vdot(normAt(m, ci), vsub(vert(m, ci), [5, 5, 5]))).toBeGreaterThan(0);
    }
  });

  it("wire:二次面 = 角点→中点→角点 8 段,首尾相连且闭合", () => {
    const m = buildFixture();
    for (const face of m.faces) {
      expect(face.wire).toHaveLength(16);
      for (let k = 0; k < 8; k += 1) {
        expect(face.wire[2 * k + 1]).toBe(face.wire[(2 * k + 2) % 16]);
      }
    }
  });

  it("meta/stages:side=10、nseg=3、colorMax 手算=1(节点 73 |uy|=1)、results 透传、阶段/压深", () => {
    const m = buildFixture();
    expect(m.mode).toBe("emap");
    expect(m.meta).toEqual({
      side: 10,
      nseg: 3,
      colorMax: 1,
      results: { dent_dep: 1, uy_min: -1, seqv_max: 123.4567 },
    });
    expect(m.stages.label).toEqual(["MESH", "DENT_A", "DENT_B", "DENT_C"]);
    expect(m.stages.time).toEqual([0, 300, 600, 900]);
    expect(m.stages.depth).toEqual([0, 0.2, 0.5, 1.0]);
  });
});

describe("buildEmapMesh · 线性单元(9 列角点版)", () => {
  const linearEmap = parseEmapCsv(
    load(`${ART}emap.csv`)
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => l.split(",").slice(0, 9).join(","))
      .join("\n"),
  );
  const m = buildEmapMesh({ frames, emap: linearEmap });   // 无 progress/results → 兜底阶段表

  it("24 面 × 2 三角 = 48;顶点 = 26 个外壳角点,无虚拟面心", () => {
    expect(linearEmap.hasMidnodes).toBe(false);
    expect(m.faces).toHaveLength(24);
    expect(totalTris(m)).toBe(48);
    expect(m.vertBase).toHaveLength(26 * 3);
    for (const face of m.faces) expect(new Set(face.tris).size).toBe(4);
  });

  it("wire = 4 条角点棱的闭合折线", () => {
    for (const face of m.faces) {
      expect(face.wire).toHaveLength(8);
      for (let k = 0; k < 4; k += 1) {
        expect(face.wire[2 * k + 1]).toBe(face.wire[(2 * k + 2) % 8]);
      }
    }
  });

  it("绕向同样外法线朝外;stages 走兜底(MESH,F1..F3)且压深一致", () => {
    for (const face of m.faces) {
      const n = vcross(
        vsub(vert(m, face.tris[1]!), vert(m, face.tris[0]!)),
        vsub(vert(m, face.tris[2]!), vert(m, face.tris[0]!)),
      );
      expect(vdot(n, vsub(faceCentroid(m, face.tris), [5, 5, 5]))).toBeGreaterThan(0);
    }
    expect(m.stages.label).toEqual(["MESH", "F1", "F2", "F3"]);
    expect(m.stages.time).toEqual([0, 0, 0, 0]);
    expect(m.stages.depth).toEqual([0, 0.2, 0.5, 1.0]);
  });
});

describe("buildEmapMesh · 手造歪单单元(虚拟面心系数与绕向翻转)", () => {
  // 角点 1..8 不规则、棱中点 9..20 外鼓(非几何中点)→ 面非平面,系数错误立刻可辨
  const P: Readonly<Record<number, [number, number, number]>> = {
    1: [0, 0, 0], 2: [4, 0, 1], 3: [5, 3, 1], 4: [1, 3, 0],
    5: [0.5, 0.5, 4], 6: [4.5, 0.2, 4.5], 7: [5, 3.5, 5], 8: [1, 3.5, 4],
    9: [2.2, -0.4, 0.6], 10: [4.6, 1.4, 1.1], 11: [2.8, 3.5, 0.4], 12: [0.3, 1.7, 0.1],
    13: [2.6, 0.4, 4.3], 14: [4.9, 1.8, 4.9], 15: [3, 3.6, 4.7], 16: [0.7, 2, 4.1],
    17: [0.2, 0.3, 2.1], 18: [4.3, 0.1, 2.9], 19: [5.2, 3.2, 3.1], 20: [1.1, 3.3, 2],
  };
  const uOf = (k: number, f: number): [number, number, number] =>
    [0.001 * k * f, -0.0005 * k * f, 0.0003 * k * f];
  const skewFrames = [1, 2].map((f) => {
    const fr = new Map<number, Vec6>();
    for (let k = 1; k <= 20; k += 1) {
      fr.set(k, [...P[k]!, ...uOf(k, f)]);
    }
    return fr;
  });
  const emap1: EmapTable = {
    cell: "hex",
    elements: [Array.from({ length: 20 }, (_, i) => i + 1)],
    hasMidnodes: true,
  };
  const m = buildEmapMesh({ frames: skewFrames, emap: emap1 });

  /** 手算系数:-0.25×Σ角点 + 0.5×Σ棱中点(逐轴)。 */
  const combine = (
    cornerIds: readonly number[],
    midIds: readonly number[],
    get: (id: number) => readonly number[],
  ): number[] =>
    [0, 1, 2].map((d) => {
      let s = 0;
      for (const id of cornerIds) s -= 0.25 * get(id)[d]!;
      for (const id of midIds) s += 0.5 * get(id)[d]!;
      return s;
    });

  it("单单元 6 面全为边界面:48 三角;顶点 = 20 节点 + 6 虚拟面心 = 26", () => {
    expect(m.faces).toHaveLength(6);
    expect(totalTris(m)).toBe(48);
    expect(m.vertBase).toHaveLength(26 * 3);
  });

  it("虚拟面心:底面(1,2,3,4)面心位置与逐帧位移 = 系数手算值", () => {
    const face0 = m.faces[0]!;                 // 面表第 0 面 = (1,2,3,4)
    const ctrIdx = fanCenterOf(face0.tris);
    const corners = [1, 2, 3, 4];
    const mids = [9, 10, 11, 12];              // 棱 (1-2),(2-3),(3-4),(4-1) 的中点列
    const expectedPos = combine(corners, mids, (id) => P[id]!);
    vert(m, ctrIdx).forEach((got, d) => expect(got).toBeCloseTo(expectedPos[d]!, 3));
    skewFrames.forEach((_, f) => {
      const expectedU = combine(corners, mids, (id) => uOf(id, f + 1));
      m.framesU[f]!.slice(3 * ctrIdx, 3 * ctrIdx + 3).forEach((got, d) => {
        expect(got).toBeCloseTo(expectedU[d]!, 4);
      });
    });
  });

  it("绕向翻转:底面原始角点序几何法线朝内,输出首三角法线与(面心 − 体心)同向", () => {
    const faceCtr = combine([1, 2, 3, 4], [9, 10, 11, 12], (id) => P[id]!);
    const bodyCtr = [0, 1, 2].map((d) => {
      let s = 0;
      for (let k = 1; k <= 8; k += 1) s += P[k]![d]!;
      return s / 8;
    });
    // 原始角点序 (P2−P1)×(P3−P1) 确实内翻 → 该面走了翻转分支
    const natural = vcross(vsub(P[2]!, P[1]!), vsub(P[3]!, P[1]!));
    expect(vdot(natural, vsub(faceCtr, bodyCtr))).toBeLessThan(0);

    const face0 = m.faces[0]!;
    const n = vcross(
      vsub(vert(m, face0.tris[1]!), vert(m, face0.tris[0]!)),
      vsub(vert(m, face0.tris[2]!), vert(m, face0.tris[0]!)),
    );
    expect(vdot(n, vsub(faceCentroid(m, face0.tris), bodyCtr))).toBeGreaterThan(0);
  });
});

describe("buildEmapMesh · 错误路径", () => {
  it("引用帧中不存在的节点 → Error 带单元号与节点号", () => {
    const bad = [...emap.elements[0]!];
    bad[4] = 999;                              // n5 角点换成不存在的节点号
    expect(() =>
      buildEmapMesh({ frames, emap: { cell: "hex", elements: [bad], hasMidnodes: true } }),
    ).toThrow(/单元 1 引用了帧中不存在的节点 999/);
  });

  it("全部单元面被共享(重复单元)→ 无边界面 Error", () => {
    const dup: EmapTable = {
      cell: "hex",
      elements: [emap.elements[0]!, emap.elements[0]!],
      hasMidnodes: true,
    };
    expect(() => buildEmapMesh({ frames, emap: dup })).toThrow(/边界面/);
  });

  it("frames 为空 → Error", () => {
    expect(() => buildEmapMesh({ frames: [], emap })).toThrow(/帧/);
  });

  it("单元节点数与 hasMidnodes 不符 → Error", () => {
    expect(() =>
      buildEmapMesh({ frames, emap: { cell: "hex", elements: [[1, 2, 3]], hasMidnodes: false } }),
    ).toThrow(/单元 1 应有 8 个节点,实际 3/);
  });

  it("退化面(角点重合,法线为零向量)→ 无法定向 Error", () => {
    const degenerate = [...emap.elements[0]!];
    degenerate[1] = degenerate[0]!;            // n2 = n1 → 面角点重合
    expect(() =>
      buildEmapMesh({ frames, emap: { cell: "hex", elements: [degenerate], hasMidnodes: true } }),
    ).toThrow(/退化/);
  });
});
