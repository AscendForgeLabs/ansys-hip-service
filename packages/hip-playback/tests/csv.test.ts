/** CSV 解析层:帧/进度/结果/单元连接表(MAPDL E16.8 前导空格口径,真机切片钉格式)。 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  CsvError,
  parseEmapCsv,
  parseFrameCsv,
  parseProgressCsv,
  parseResultsCsv,
} from "../src/csv";

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

const REAL_FRAME_HEAD = load("fixtures/real/frame_head.csv");
const REAL_PROGRESS = load("fixtures/real/progress.csv");
const REAL_RESULTS = load("fixtures/real/results.csv");
const SYNTH_FRAME1 = load("fixtures/synthetic/artifacts/frame_1.csv");
const SYNTH_EMAP = load("fixtures/synthetic/artifacts/emap.csv");

describe("parseFrameCsv", () => {
  it("真机切片:表头跳过、E16.8 前导空格按 float 解析", () => {
    const nodes = parseFrameCsv(REAL_FRAME_HEAD);
    expect(nodes.size).toBe(25);                      // 26 行 - 表头
    const n1 = nodes.get(1);
    expect(n1).toBeDefined();
    expect(n1![0]).toBeCloseTo(0.0, 10);              // x
    expect(n1![1]).toBeCloseTo(20.0, 10);             // y
    expect(n1![2]).toBeCloseTo(0.0, 10);              // z
    expect(n1![3]).toBeCloseTo(0.0, 10);              // ux
    expect(n1![4]).toBeCloseTo(-0.051128866, 9);      // uy(真机 frame_1 首节点)
    expect(n1![5]).toBeCloseTo(0.0, 10);              // uz
  });

  it("合成网格:81 节点(角点+棱中点,hex20 真实节点集),坐标 = 初始构型", () => {
    const nodes = parseFrameCsv(SYNTH_FRAME1);
    expect(nodes.size).toBe(81);
    expect(Array.from(nodes.keys())).toEqual(expect.arrayContaining([1, 63, 125]));
    const center = nodes.get(63)!;                    // ix=iy=iz=2 → (5,5,5)
    expect(center[0]).toBeCloseTo(5, 10);
    expect(center[1]).toBeCloseTo(5, 10);
    expect(center[2]).toBeCloseTo(5, 10);
  });

  it("容忍尾部空行;坏数值行抛 CsvError 且带行号", () => {
    expect(() => parseFrameCsv(`${SYNTH_FRAME1}\n\n`)).not.toThrow();
    const bad = "node,x_mm,y_mm,z_mm,ux_mm,uy_mm,uz_mm\n  0.1E+01, oops, 0, 0, 0, 0, 0\n";
    expect(() => parseFrameCsv(bad)).toThrow(CsvError);
    try {
      parseFrameCsv(bad);
    } catch (err) {
      expect((err as CsvError).message).toContain("2");
    }
  });
});

describe("parseProgressCsv", () => {
  it("真机 7 行:标签去空白、时间按 float", () => {
    const rows = parseProgressCsv(REAL_PROGRESS);
    expect(rows.map((r) => r.label)).toEqual([
      "MESH", "DENT_03", "DENT_06", "DENT_09", "DENT_12", "DENT_15", "DENT_18",
    ]);
    expect(rows[1]!.time).toBeCloseTo(600, 10);
    expect(rows[6]!.time).toBeCloseTo(3600, 10);
  });

  it("撕裂半行(无逗号)跳过不致命", () => {
    const rows = parseProgressCsv("MESH    ,  0.00000000E+00\nDENT_2\n");
    expect(rows).toHaveLength(1);
    expect(rows[0]!.label).toBe("MESH");
  });
});

describe("parseResultsCsv", () => {
  it("真机 3 键解析为数值", () => {
    const values = parseResultsCsv(REAL_RESULTS);
    expect(values["dent_dep"]).toBeCloseTo(1.8, 10);
    expect(values["uy_min"]).toBeCloseTo(-1.8, 10);
    expect(values["seqv_max"]).toBeCloseTo(1747.7808, 4);
  });

  it("空文本 → 空对象", () => {
    expect(parseResultsCsv("")).toEqual({});
  });
});

describe("parseEmapCsv", () => {
  it("21 列 hex20:8 单元、角点+12 中点齐全", () => {
    const emap = parseEmapCsv(SYNTH_EMAP);
    expect(emap.elements).toHaveLength(8);
    expect(emap.hasMidnodes).toBe(true);
    // 单元 1(cx=cy=cz=0):角点 = 底面 1,3,13,11 + 顶面 51,53,63,61
    expect(emap.elements[0]!.slice(0, 8)).toEqual([1, 3, 13, 11, 51, 53, 63, 61]);
    expect(emap.elements[0]).toHaveLength(20);
    // n9 = 棱 1-2 中点 = nid(1,0,0) = 2
    expect(emap.elements[0]![8]).toBe(2);
  });

  it("9 列角点版:hasMidnodes=false", () => {
    const cornersOnly = SYNTH_EMAP
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => l.split(",").slice(0, 9).join(","))
      .join("\n");
    const emap = parseEmapCsv(cornersOnly);
    expect(emap.elements).toHaveLength(8);
    expect(emap.hasMidnodes).toBe(false);
    expect(emap.elements[0]).toHaveLength(8);
  });

  it("全部行都坏 → CsvError(不静默返回空)", () => {
    expect(() => parseEmapCsv("elem,n1,n2\n垃圾,行\n")).toThrow(CsvError);
  });
});
