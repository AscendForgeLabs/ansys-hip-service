/** 加载层测试:服务端 fetch(全局 stub)/ 本地 File / 纯文本 三入口同口径。
 *
 * 服务端用例伪造 /jobs/{id}/artifacts 清单与工件响应,内容取合成 fixture;
 * 纯函数用例(loadFromTexts)用最小手写文本(2 节点 2 帧)钉排序与缺席容忍。
 */
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { loadFromFiles, loadFromService, loadFromTexts } from "../src/loader";

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

const FIX = {
  "frame_1.csv": load("fixtures/synthetic/artifacts/frame_1.csv"),
  "frame_2.csv": load("fixtures/synthetic/artifacts/frame_2.csv"),
  "frame_3.csv": load("fixtures/synthetic/artifacts/frame_3.csv"),
  "progress.csv": load("fixtures/synthetic/artifacts/progress.csv"),
  "results.csv": load("fixtures/synthetic/artifacts/results.csv"),
  "emap.csv": load("fixtures/synthetic/artifacts/emap.csv"),
} as const;

// epart 侧车(单元号 → part 号):内容独立于 emap,取最小两单元映射
const EPART_CSV = "elem,part\n1,100\n2,100\n3,200\n";

// 各帧节点 1 的 ux(用于钉帧序:frame_1 < frame_2 < frame_3)
const UX_FRAME1 = -0.23165449e-4;
const UX_FRAME2 = -0.57913624e-4;
const UX_FRAME3 = -0.11582724e-3;

const BASE = "http://hip.test:8010";
const JOB = "job-2026";
const LIST_URL = `${BASE}/jobs/${JOB}/artifacts`;

interface StubResponse {
  ok: boolean;
  status: number;
  text: () => Promise<string>;
  json: () => Promise<unknown>;
}

const textResponse = (body: string): StubResponse => ({
  ok: true,
  status: 200,
  text: async () => body,
  json: async () => {
    throw new Error("文本端点不该被当 JSON 读");
  },
});

const jsonResponse = (body: unknown): StubResponse => ({
  ok: true,
  status: 200,
  text: async () => JSON.stringify(body),
  json: async () => body,
});

const errorResponse = (status: number): StubResponse => ({
  ok: false,
  status,
  text: async () => "",
  json: async () => ({}),
});

/** 清单 URL 不含 "/artifacts/",工件 URL 恒含 — 以此区分两种响应。 */
function stubServiceFetch(opts: {
  listing: StubResponse;
  files?: Record<string, StubResponse>;
}): { calls: string[] } {
  const calls: string[] = [];
  const mock = async (input: RequestInfo | URL): Promise<StubResponse> => {
    const url = String(input);
    calls.push(url);
    if (!url.includes("/artifacts/")) return opts.listing;
    const name = url.slice(url.lastIndexOf("/") + 1);
    return opts.files?.[name] ?? errorResponse(404);
  };
  vi.stubGlobal("fetch", mock as unknown as typeof fetch);
  return { calls };
}

const fixtureFiles = (): Record<string, StubResponse> => ({
  "frame_1.csv": textResponse(FIX["frame_1.csv"]),
  "frame_2.csv": textResponse(FIX["frame_2.csv"]),
  "frame_3.csv": textResponse(FIX["frame_3.csv"]),
  "progress.csv": textResponse(FIX["progress.csv"]),
  "results.csv": textResponse(FIX["results.csv"]),
  "emap.csv": textResponse(FIX["emap.csv"]),
  "epart.csv": textResponse(EPART_CSV),
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loadFromService", () => {
  it("合成工件全量:清单乱序仍按帧号升序拉取与解析", async () => {
    const { calls } = stubServiceFetch({
      listing: jsonResponse([
        "frame_3.csv", "progress.csv", "frame_1.csv",
        "emap.csv", "frame_2.csv", "results.csv", "epart.csv",
      ]),
      files: fixtureFiles(),
    });
    const art = await loadFromService(BASE, JOB);

    expect(calls[0]).toBe(LIST_URL);
    expect(calls.filter((u) => /frame_\d+\.csv$/.test(u))).toEqual([
      `${LIST_URL}/frame_1.csv`,
      `${LIST_URL}/frame_2.csv`,
      `${LIST_URL}/frame_3.csv`,
    ]);
    expect(art.frames).toHaveLength(3);
    expect(art.frames[0]!.get(1)![3]).toBeCloseTo(UX_FRAME1, 12);
    expect(art.frames[1]!.get(1)![3]).toBeCloseTo(UX_FRAME2, 12);
    expect(art.frames[2]!.get(1)![3]).toBeCloseTo(UX_FRAME3, 12);
    expect(art.stageLabels).toEqual(["MESH", "DENT_A", "DENT_B", "DENT_C"]);
    expect(art.stageTimes).toEqual([0, 300, 600, 900]);
    expect(art.results).toEqual({ dent_dep: 1, uy_min: -1, seqv_max: 123.4567 });
    expect(art.emap?.hasMidnodes).toBe(true);
    expect(art.emap?.elements).toHaveLength(8);
    expect(art.epart?.byElem.get(2)).toBe(100);
    expect(art.epart?.byElem.get(3)).toBe(200);
  });

  it("baseUrl 去尾部斜杠后拼接", async () => {
    const { calls } = stubServiceFetch({
      listing: jsonResponse(["frame_1.csv"]),
      files: { "frame_1.csv": textResponse(FIX["frame_1.csv"]) },
    });
    await loadFromService(`${BASE}///`, JOB);
    expect(calls[0]).toBe(LIST_URL);
  });

  it("progress/results/emap/epart 不在清单 → 字段 undefined(缺席容忍)", async () => {
    stubServiceFetch({
      listing: jsonResponse(["frame_1.csv", "frame_2.csv"]),
      files: {
        "frame_1.csv": textResponse(FIX["frame_1.csv"]),
        "frame_2.csv": textResponse(FIX["frame_2.csv"]),
      },
    });
    const art = await loadFromService(BASE, JOB);
    expect(art.frames).toHaveLength(2);
    expect(art.stageLabels).toBeUndefined();
    expect(art.stageTimes).toBeUndefined();
    expect(art.results).toBeUndefined();
    expect(art.emap).toBeUndefined();
    expect(art.epart).toBeUndefined();
  });

  it("清单响应非 ok → 抛错带状态码与 URL", async () => {
    stubServiceFetch({ listing: errorResponse(503) });
    await expect(loadFromService(BASE, JOB)).rejects.toThrow(/503/);
    await expect(loadFromService(BASE, JOB)).rejects.toThrow(LIST_URL);
  });

  it("工件响应非 ok → 抛错带状态码与 URL", async () => {
    stubServiceFetch({
      listing: jsonResponse(["frame_1.csv", "frame_2.csv"]),
      files: {
        "frame_1.csv": textResponse(FIX["frame_1.csv"]),
        "frame_2.csv": errorResponse(500),
      },
    });
    await expect(loadFromService(BASE, JOB)).rejects.toThrow(/500/);
    await expect(loadFromService(BASE, JOB)).rejects.toThrow(/frame_2\.csv/);
  });

  it("清单没有 frame_N.csv → 抛错", async () => {
    stubServiceFetch({
      listing: jsonResponse(["progress.csv", "results.csv"]),
      files: {
        "progress.csv": textResponse(FIX["progress.csv"]),
        "results.csv": textResponse(FIX["results.csv"]),
      },
    });
    await expect(loadFromService(BASE, JOB)).rejects.toThrow("没有 frame_N.csv 工件");
  });

  it("清单不是字符串数组 → 抛错", async () => {
    stubServiceFetch({ listing: jsonResponse({ error: "boom" }) });
    await expect(loadFromService(BASE, JOB)).rejects.toThrow(/清单/);
  });
});

describe("loadFromTexts", () => {
  const HEADER = "node,x_mm,y_mm,z_mm,ux_mm,uy_mm,uz_mm\n";
  const frame1 = `${HEADER}1, 0, 0, 0, 0, 0, 0\n2, 1, 0, 0, 0.1, 0, 0\n`;
  const frame2 = `${HEADER}1, 0, 0, 0, 0, 0.2, 0\n2, 1, 0, 0, 0.1, 0.4, 0\n`;

  it("最小两帧:按键名中的帧号数字升序", () => {
    const art = loadFromTexts({ "frame_2.csv": frame2, "frame_1.csv": frame1 });
    expect(art.frames).toHaveLength(2);
    expect(art.frames[0]!.get(2)![4]).toBeCloseTo(0, 12);     // frame_1 节点 2 uy=0
    expect(art.frames[1]!.get(1)![4]).toBeCloseTo(0.2, 12);   // frame_2 节点 1 uy=0.2
  });

  it("数字序而非字典序:frame_10 排在 frame_2 之后", () => {
    const frame10 = `${HEADER}1, 0, 0, 0, 0, 10, 0\n`;
    const art = loadFromTexts({
      "frame_10.csv": frame10,
      "frame_2.csv": frame2,
      "frame_1.csv": frame1,
    });
    expect(art.frames).toHaveLength(3);
    expect(art.frames[2]!.get(1)![4]).toBeCloseTo(10, 12);
  });

  it("progress/results/emap/epart 缺席 → 字段 undefined", () => {
    const art = loadFromTexts({ "frame_1.csv": frame1 });
    expect(art.frames).toHaveLength(1);
    expect(art.stageLabels).toBeUndefined();
    expect(art.stageTimes).toBeUndefined();
    expect(art.results).toBeUndefined();
    expect(art.emap).toBeUndefined();
    expect(art.epart).toBeUndefined();
  });

  it("epart.csv 侧车解析为 byElem 映射", () => {
    const art = loadFromTexts({
      "frame_1.csv": frame1,
      "epart.csv": EPART_CSV,
    });
    expect(art.epart?.byElem.get(1)).toBe(100);
    expect(art.epart?.byElem.get(2)).toBe(100);
    expect(art.epart?.byElem.get(3)).toBe(200);
  });

  it("progress 解析为标签数组与时间数组", () => {
    const art = loadFromTexts({
      "frame_1.csv": frame1,
      "progress.csv": "MESH  ,  0.00000000E+00\nLOAD  ,  0.15000000E+01\n",
    });
    expect(art.stageLabels).toEqual(["MESH", "LOAD"]);
    expect(art.stageTimes).toEqual([0, 1.5]);
  });

  it("results 解析为数值字典", () => {
    const art = loadFromTexts({ "frame_1.csv": frame1, "results.csv": "k1, 1.5\n" });
    expect(art.results).toEqual({ k1: 1.5 });
  });

  it("emap 解析(9 列角点版 → hasMidnodes=false)", () => {
    const art = loadFromTexts({
      "frame_1.csv": frame1,
      "emap.csv": "elem,n1,n2,n3,n4,n5,n6,n7,n8\n1, 1, 2, 3, 4, 5, 6, 7, 8\n",
    });
    expect(art.emap?.hasMidnodes).toBe(false);
    expect(art.emap?.elements[0]).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });

  it("无关文件名被忽略(只认四类工件)", () => {
    expect(() =>
      loadFromTexts({ "frame_1.csv": frame1, "job.out": "...", "readme.txt": "x" }),
    ).not.toThrow();
  });

  it("没有帧 → 抛'没有 frame_N.csv 工件'", () => {
    expect(() => loadFromTexts({ "progress.csv": "MESH, 0\n" })).toThrow(
      "没有 frame_N.csv 工件",
    );
  });
});

describe("loadFromFiles", () => {
  it("按文件名筛选清单内工件,File 经 text() 读取", async () => {
    const files = [
      new File([FIX["frame_2.csv"]], "frame_2.csv"),
      new File([FIX["frame_1.csv"]], "frame_1.csv"),
      new File([FIX["progress.csv"]], "progress.csv"),
      new File([EPART_CSV], "epart.csv"),
      new File(["(无关文件,不该被读取)"], "job.out"),
    ];
    const art = await loadFromFiles(files);
    expect(art.frames).toHaveLength(2);
    expect(art.frames[0]!.get(1)![3]).toBeCloseTo(UX_FRAME1, 12);
    expect(art.frames[1]!.get(1)![3]).toBeCloseTo(UX_FRAME2, 12);
    expect(art.stageLabels).toEqual(["MESH", "DENT_A", "DENT_B", "DENT_C"]);
    expect(art.results).toBeUndefined();
    expect(art.emap).toBeUndefined();
    expect(art.epart?.byElem.get(1)).toBe(100);
    expect(art.epart?.byElem.get(3)).toBe(200);
  });

  it("没有帧文件 → 抛错", async () => {
    await expect(loadFromFiles([new File(["MESH, 0\n"], "progress.csv")])).rejects.toThrow(
      "没有 frame_N.csv 工件",
    );
  });
});
