/** CSV 解析层:帧/进度/结果/单元连接表(MAPDL E16.8 前导空格口径,真机切片钉格式)。 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  CsvError,
  parseEmapCsv,
  parseEpartCsv,
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

  it("9 列/21 列档 cell='hex',elemIds 恒填首列单元号", () => {
    const emap = parseEmapCsv(SYNTH_EMAP);
    expect(emap.cell).toBe("hex");
    expect(emap.elemIds).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });

  it("5 列 tet4:cell='tet'、hasMidnodes=false,节点取首列后 4 列", () => {
    const emap = parseEmapCsv(
      "elem,n1,n2,n3,n4\n" +
        "101, 1, 2, 3, 4\n" +
        "102, 5, 6, 7, 8\n",
    );
    expect(emap.cell).toBe("tet");
    expect(emap.hasMidnodes).toBe(false);
    expect(emap.elements).toEqual([[1, 2, 3, 4], [5, 6, 7, 8]]);
    expect(emap.elemIds).toEqual([101, 102]);
  });

  it("13 列 tet10:cell='tet'、hasMidnodes=true,每单元 10 节点", () => {
    const emap = parseEmapCsv(
      "elem,n1,n2,n3,n4,n5,n6,n7,n8,n9,n10\n" +
        "7, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10\n" +
        "8, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20\n",
    );
    expect(emap.cell).toBe("tet");
    expect(emap.hasMidnodes).toBe(true);
    expect(emap.elements).toHaveLength(2);
    expect(emap.elements[1]).toHaveLength(10);
    expect(emap.elemIds).toEqual([7, 8]);
  });

  it("四档混宽 9↔13 → CsvError 带两宽度与分文件提示", () => {
    const mixed =
      "elem,n1,n2,n3,n4,n5,n6,n7,n8\n" +
      "1, 1, 2, 3, 4, 5, 6, 7, 8\n" +
      "2, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20\n";
    expect(() => parseEmapCsv(mixed)).toThrow(/列数 13 与首行 9 不一致/);
    expect(() => parseEmapCsv(mixed)).toThrow(/混合单元类型请分文件导出/);
  });

  it("四档混宽 5↔21 同样报错(先 tet 后 hex)", () => {
    const mixed =
      "elem,n1,n2,n3,n4\n" +
      "1, 1, 2, 3, 4\n" +
      "2, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20\n";
    expect(() => parseEmapCsv(mixed)).toThrow(/列数 21 与首行 5 不一致/);
    expect(() => parseEmapCsv(mixed)).toThrow(/混合单元类型请分文件导出/);
  });

  it("非法宽度行(7 列)跳过 + warn,不定宽也不致命", () => {
    const rows =
      "elem,n1,n2,n3,n4,n5,n6,n7,n8\n" +
      "1, 1, 2, 3, 4, 5, 6, 7, 8\n" +
      "77, 1, 2, 3, 4, 5, 6\n" +            // 7 列:不在四档集合
      "2, 9, 10, 11, 12, 13, 14, 15, 16\n";
    const emap = parseEmapCsv(rows);
    expect(emap.elements).toHaveLength(2);
    expect(emap.elemIds).toEqual([1, 2]);
    expect(emap.hasMidnodes).toBe(false);
  });
});

describe("parseEpartCsv", () => {
  it("有表头:elem,part 首行跳过,解析为单元号 → part 号", () => {
    const t = parseEpartCsv("elem,part\n1,100\n2,100\n3,200\n");
    expect(t.byElem.get(1)).toBe(100);
    expect(t.byElem.get(2)).toBe(100);
    expect(t.byElem.get(3)).toBe(200);
    expect(t.byElem.size).toBe(3);
  });

  it("无表头容忍;值前后空白剥离", () => {
    const t = parseEpartCsv(" 1 , 100 \n2,200\n");
    expect(t.byElem.get(1)).toBe(100);
    expect(t.byElem.get(2)).toBe(200);
  });

  it("坏行(非两列/非正整数/撕裂半行)跳过,好行保留", () => {
    const t = parseEpartCsv("elem,part\n1,100\n垃圾行\n0,50\n-3,60\n4.5,70\n5,80\n");
    expect(t.byElem.size).toBe(2);
    expect(t.byElem.get(1)).toBe(100);
    expect(t.byElem.get(5)).toBe(80);
  });

  it("全部坏/空 → CsvError(不静默返回空)", () => {
    expect(() => parseEpartCsv("")).toThrow(CsvError);
    expect(() => parseEpartCsv("elem,part\n垃圾,行\n")).toThrow(/没有可用数据行/);
  });

  it("重复单元号后写覆盖先写", () => {
    const t = parseEpartCsv("1,100\n1,300\n");
    expect(t.byElem.size).toBe(1);
    expect(t.byElem.get(1)).toBe(300);
  });
});
