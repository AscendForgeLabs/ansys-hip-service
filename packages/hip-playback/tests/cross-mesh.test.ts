/** 构网交叉验证:同一份规则格数据,lattice 反推与 emap 通用两路径必须同构。

 * 这是"emap 通用构网正确性"的最强单测:合成 2×2×2 hex20 立方的 emap.csv
 * 与规则格反推在顶点集合/三角数/colorMax/stages 上逐值一致
 * (顶点顺序两路各自去重不同,按排序后集合比较;wire 拓扑两路口径不同,不计)。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { parseEmapCsv, parseFrameCsv } from "../src/csv";
import { buildEmapMesh } from "../src/mesh/emap";
import { buildLatticeMesh } from "../src/mesh/lattice";
import type { MeshInput } from "../src/mesh/types";

// cwd = 包根(npm test 运行口径);避免 DOM 环境下 URL 全局的 scheme 差异
const FIXTURE_DIR = resolve(process.cwd(), "tests/fixtures/synthetic/artifacts");

const read = (name: string): string =>
  readFileSync(resolve(FIXTURE_DIR, name), "utf-8");

function loadInput(): MeshInput {
  return {
    frames: [1, 2, 3].map((i) => parseFrameCsv(read(`frame_${i}.csv`))),
    emap: parseEmapCsv(read("emap.csv")),
  };
}

/** 平铺顶点数组 → 去重排序后的坐标三元组(顺序/重复无关比较,1e-3 容差)。

 * 顶点口径差异(两者皆正确):lattice 沿参考实现"每面独立顶点表"
 * (跨面共享节点按面复制,保 per-face 法线;6 面 × 25 = 150);
 * emap 按节点号全局去重(74 表面节点 + 24 虚拟面心 = 98)。
 * 比较取 1e-3 容差:虚拟面心两路的求和顺序不同(Neumaier vs 朴素),
 * round 边界可差 1-ulp→1e-4/1e-5,几何上无意义;拓扑/计数则精确断言。
 */
function vertexSet(flat: readonly number[]): string[] {
  const triples = new Set<string>();
  for (let i = 0; i + 2 < flat.length; i += 3) {
    triples.add([flat[i]!, flat[i + 1]!, flat[i + 2]!].map((v) => v.toFixed(3)).join(","));
  }
  return [...triples].sort();
}

const input = loadInput();

describe("lattice × emap 交叉验证(同一规则格数据)", () => {
  const lattice = buildLatticeMesh(input);           // 不带 emap → 规则格反推
  const emap = buildEmapMesh({ ...input, emap: input.emap! });

  it("顶点数符合各自口径:lattice 150(每面独立表)/ emap 98(全局去重)", () => {
    expect(lattice.vertBase.length / 3).toBe(150);   // 6 面 ×(21 节点 + 4 面心)
    expect(emap.vertBase.length / 3).toBe(98);       // 74 表面节点 + 24 虚拟面心
  });

  it("顶点几何集合一致(去重比较,容差 1e-3)", () => {
    expect(vertexSet(lattice.vertBase)).toEqual(vertexSet(emap.vertBase));
    expect(vertexSet(emap.vertBase)).toHaveLength(98);
    for (let f = 0; f < 3; f++) {
      expect(vertexSet(lattice.framesU[f]!)).toEqual(vertexSet(emap.framesU[f]!));
    }
  });

  it("三角总数一致:24 单元面 × 8 三角扇 = 192(tris 为索引数组,3 索引=1 三角)", () => {
    const totalTris = (m: typeof lattice): number =>
      m.faces.reduce((sum, face) => sum + face.tris.length / 3, 0);
    expect(totalTris(lattice)).toBe(192);
    expect(totalTris(emap)).toBe(192);
  });

  it("meta/stages 完全一致(共用装配口径)", () => {
    expect(emap.meta).toEqual(lattice.meta);
    expect(emap.stages).toEqual(lattice.stages);
  });
});
