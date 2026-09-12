/** 工件加载层 — 服务端 / 本地 File / 纯文本三入口,共用同一套文件名口径。
 *
 * 工件四类(docs/passthrough-guide.md / playback-handbook.md §1):
 *   frame_N.csv(位移帧,按 N 数字升序)/ progress.csv(阶段)/
 *   results.csv(结构化结果)/ emap.csv(单元连接表)。
 * progress/results/emap 缺席容忍(字段 undefined);一个帧都没有是错误,不静默。
 */

import {
  parseEmapCsv,
  parseFrameCsv,
  parseProgressCsv,
  parseResultsCsv,
} from "./csv";
import type { EmapTable, FrameNodes } from "./csv";

export interface LoadedArtifacts {
  frames: FrameNodes[];            // 按帧号升序
  stageLabels?: string[];
  stageTimes?: number[];
  results?: Record<string, number>;
  emap?: EmapTable;
}

const FRAME_NAME_RE = /^frame_(\d+)\.csv$/;
const SIDELOAD_NAMES = ["progress.csv", "results.csv", "emap.csv"] as const;

/** 从文件名集合发现帧文件,按帧号数字升序(拒绝字典序:frame_10 < frame_2)。 */
function frameNamesSorted(names: readonly string[]): string[] {
  return names
    .flatMap((name) => {
      const m = FRAME_NAME_RE.exec(name);
      return m ? [[name, Number(m[1])] as const] : [];
    })
    .sort((x, y) => x[1] - y[1])
    .map(([name]) => name);
}

/** 纯核心:文件名 → 文本 → 解析。三入口共用,保证同口径;不改入参。 */
function buildFromTexts(texts: Record<string, string>): LoadedArtifacts {
  const frameNames = frameNamesSorted(Object.keys(texts));
  if (frameNames.length === 0) {
    throw new Error("没有 frame_N.csv 工件");
  }
  const loaded: LoadedArtifacts = {
    frames: frameNames.map((name) => parseFrameCsv(texts[name]!)),
  };
  if (texts["progress.csv"] !== undefined) {
    const rows = parseProgressCsv(texts["progress.csv"]);
    loaded.stageLabels = rows.map((r) => r.label);
    loaded.stageTimes = rows.map((r) => r.time);
  }
  if (texts["results.csv"] !== undefined) {
    loaded.results = parseResultsCsv(texts["results.csv"]);
  }
  if (texts["emap.csv"] !== undefined) {
    loaded.emap = parseEmapCsv(texts["emap.csv"]);
  }
  return loaded;
}

export async function loadFromService(
  baseUrl: string,
  jobId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<LoadedArtifacts> {
  const listUrl = `${baseUrl.replace(/\/+$/, "")}/jobs/${encodeURIComponent(jobId)}/artifacts`;
  const names = await fetchNames(listUrl, fetchImpl);
  const texts: Record<string, string> = {};
  for (const name of frameNamesSorted(names)) {
    texts[name] = await fetchText(`${listUrl}/${name}`, fetchImpl);
  }
  for (const name of SIDELOAD_NAMES) {
    if (names.includes(name)) {
      texts[name] = await fetchText(`${listUrl}/${name}`, fetchImpl);
    }
  }
  return buildFromTexts(texts);
}

export async function loadFromFiles(files: readonly File[]): Promise<LoadedArtifacts> {
  const texts: Record<string, string> = {};
  for (const file of files) {
    if (FRAME_NAME_RE.test(file.name) || (SIDELOAD_NAMES as readonly string[]).includes(file.name)) {
      texts[file.name] = await file.text();
    }
  }
  return buildFromTexts(texts);
}

export function loadFromTexts(texts: Record<string, string>): LoadedArtifacts {
  return buildFromTexts(texts);
}

async function fetchNames(listUrl: string, fetchImpl: typeof fetch): Promise<string[]> {
  const res = await fetchImpl(listUrl);
  if (!res.ok) {
    throw new Error(`拉取工件清单失败:HTTP ${res.status} ${listUrl}`);
  }
  const body: unknown = await res.json();
  if (!Array.isArray(body) || body.some((n) => typeof n !== "string")) {
    throw new Error(`工件清单不是字符串数组:${listUrl}`);
  }
  return body;
}

async function fetchText(url: string, fetchImpl: typeof fetch): Promise<string> {
  const res = await fetchImpl(url);
  if (!res.ok) {
    throw new Error(`拉取工件失败:HTTP ${res.status} ${url}`);
  }
  return res.text();
}
