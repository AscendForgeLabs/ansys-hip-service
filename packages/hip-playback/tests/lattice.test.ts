/** lattice 规则格构网:与真 build_viewer.py 的 golden 输出逐值对拍 + 非规则格退场。
 *
 * golden = tests/fixtures/synthetic/cube-mesh.golden.json(真 build_viewer.py 跑合成工件
 * 2×2×2 hex20、10mm 立方、3 帧的 DATA 基准,由 tests/fixtures/gen/make_fixtures.py 生成);
 * 输入侧经 parseFrameCsv/parseProgressCsv/parseResultsCsv 从同一份合成工件组装,端到端钉口径。
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  parseFrameCsv,
  parseProgressCsv,
  parseResultsCsv,
  type FrameNodes,
  type Vec6,
} from "../src/csv";
import { buildLatticeMesh } from "../src/mesh/lattice";
import type { MeshInput } from "../src/mesh/types";

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

interface GoldenMesh {
  meta: { side: number; nseg: number; colorMax: number; results: Record<string, number> };
  vertBase: number[];
  vertNorm: number[];
  framesU: number[][];
  faces: Array<{ tris: number[]; wire: number[] }>;
  stages: { label: string[]; time: number[]; depth: number[] };
}

const ART = "fixtures/synthetic/artifacts";
const GOLDEN = JSON.parse(
  load("fixtures/synthetic/cube-mesh.golden.json")) as GoldenMesh;
const FRAMES: FrameNodes[] = [1, 2, 3].map((n) =>
  parseFrameCsv(load(`${ART}/frame_${n}.csv`)));

function synthInput(): MeshInput {
  const stages = parseProgressCsv(load(`${ART}/progress.csv`));
  return {
    frames: FRAMES,
    stageLabels: stages.map((r) => r.label),
    stageTimes: stages.map((r) => r.time),
    results: parseResultsCsv(load(`${ART}/results.csv`)),
  };
}

describe("buildLatticeMesh golden 对拍(合成 2×2×2 hex20、10mm、3 帧)", () => {
  const mesh = buildLatticeMesh(synthInput());

  it("meta:side/nseg/colorMax/results + mode=lattice", () => {
    expect(mesh.mode).toBe("lattice");
    expect(mesh.meta.side).toBeCloseTo(GOLDEN.meta.side, 6);
    expect(mesh.meta.nseg).toBe(GOLDEN.meta.nseg);
    expect(mesh.meta.colorMax).toBeCloseTo(GOLDEN.meta.colorMax, 6);
    expect(Object.keys(mesh.meta.results).sort())
      .toEqual(Object.keys(GOLDEN.meta.results).sort());
    for (const key of Object.keys(GOLDEN.meta.results)) {
      expect(mesh.meta.results[key]).toBeCloseTo(GOLDEN.meta.results[key]!, 6);
    }
  });

  it("vertBase:长度全等 + 逐值(精度 6;含 6×4 个 Serendipity 虚拟面心)", () => {
    expect(mesh.vertBase).toHaveLength(GOLDEN.vertBase.length);   // 150 顶点(6 面 × 25)× 3
    GOLDEN.vertBase.forEach((g, i) => expect(mesh.vertBase[i]).toBeCloseTo(g, 6));
  });

  it("vertNorm:长度全等 + 逐值(静态面外法线)", () => {
    expect(mesh.vertNorm).toHaveLength(GOLDEN.vertNorm.length);
    GOLDEN.vertNorm.forEach((g, i) => expect(mesh.vertNorm[i]).toBeCloseTo(g, 6));
  });

  it("framesU:每帧长度全等 + 逐值(round5 位移,含面心外推)", () => {
    expect(mesh.framesU).toHaveLength(GOLDEN.framesU.length);     // 3 帧
    GOLDEN.framesU.forEach((gf, f) => {
      expect(mesh.framesU[f]).toHaveLength(gf.length);            // 450(150 顶点 × 3)
      gf.forEach((g, i) => expect(mesh.framesU[f]![i]).toBeCloseTo(g, 6));
    });
  });

  it("faces:6 面,tris/wire 索引数组全等(绕向判据 + 8 三角扇 + 角点格线)", () => {
    expect(mesh.faces).toHaveLength(GOLDEN.faces.length);
    GOLDEN.faces.forEach((gf, fi) => {
      expect(mesh.faces[fi]!.tris).toEqual(gf.tris);
      expect(mesh.faces[fi]!.wire).toEqual(gf.wire);
    });
  });

  it("stages:label 全等,time/depth 逐值", () => {
    expect(mesh.stages.label).toEqual(GOLDEN.stages.label);
    expect(mesh.stages.time).toHaveLength(GOLDEN.stages.time.length);
    GOLDEN.stages.time.forEach((g, i) =>
      expect(mesh.stages.time[i]).toBeCloseTo(g, 6));
    expect(mesh.stages.depth).toHaveLength(GOLDEN.stages.depth.length);
    GOLDEN.stages.depth.forEach((g, i) =>
      expect(mesh.stages.depth[i]).toBeCloseTo(g, 6));
  });
});

describe("buildLatticeMesh 非规则格退场", () => {
  it("坐标扰动 → 非均匀结构格,消息含轴名与 §3 指引", () => {
    // 节点 2 = (2.5,0,0):x 扰动 0.37 → x 轴唯一值间距不再唯一
    const perturbed = new Map(FRAMES[0]!);
    const v = perturbed.get(2)!;
    perturbed.set(2, [v[0] + 0.37, v[1], v[2], v[3], v[4], v[5]] as Vec6);
    const input: MeshInput = { frames: [perturbed, ...FRAMES.slice(1)] };
    expect(() => buildLatticeMesh(input)).toThrow(/轴 x/);
    expect(() => buildLatticeMesh(input)).toThrow(/§3/);
  });

  it("角点+棱中点计数不匹配(去顶层后每轴 4 值)→ 消息含轴名与 §3", () => {
    // 去掉 z=10 顶层:z 唯一值 {0,2.5,5,7.5} 间距均匀,但 4 ≠ 2×角点数-1
    const truncated: FrameNodes = new Map(
      Array.from(FRAMES[0]!, ([nid, node]) => [nid, node] as [number, Vec6])
        .filter(([, node]) => node[2] < 9));
    expect(() => buildLatticeMesh({ frames: [truncated] })).toThrow(/轴 z/);
    expect(() => buildLatticeMesh({ frames: [truncated] })).toThrow(/§3/);
  });

  it("空帧列表 / 空首帧 → 明确报错", () => {
    expect(() => buildLatticeMesh({ frames: [] })).toThrow(/帧/);
    expect(() => buildLatticeMesh({ frames: [new Map()] })).toThrow(/节点/);
  });
});
