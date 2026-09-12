/** CSV 解析层 — passthrough 工件文本 → 结构化数据。

 * 格式口径(docs/passthrough-guide.md / playback-handbook.md §1):
 *   frame_N.csv:首行表头,行 = 节点号,x,y,z,ux,uy,uz(E16.8 科学计数带前导空格);
 *   progress.csv / results.csv:无表头,"标签(≤8 字符,右补空格),数值";
 *   emap.csv(手册 §3 档②):首行表头 elem,n1..n8[,n9..n20],行 = 单元号 + 节点号。
 *
 * 容错策略:帧文件是完整工件 → 坏数值行抛 CsvError(带行号,不静默);
 * progress/results/emap 可能在作业运行中被读取 → 撕裂半行跳过并 warn,
 * 但全部行都坏时仍抛错(空数据是错误,不是正常态)。
 */

export type Vec6 = readonly [number, number, number, number, number, number];
/** 节点号 → [x, y, z, ux, uy, uz](坐标 = 未变形初始构型,各帧恒同)。 */
export type FrameNodes = Map<number, Vec6>;

/** 单元连接表:每单元 8 角点节点号(+ hex20 时 12 棱中点,共 20 列)。 */
export interface EmapTable {
  elements: number[][];       // [单元索引] → 节点号数组(8 或 20 个)
  hasMidnodes: boolean;
}

export interface StageRow {
  label: string;
  time: number;               // 累计秒
}

export class CsvError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CsvError";
  }
}

function toFloat(token: string, where: string): number {
  const value = Number(token);            // Number 自带前后空白容忍(E16.8 前导空格)
  if (!Number.isFinite(value)) {
    throw new CsvError(`${where}:数值无法解析 '${token.trim()}'`);
  }
  return value;
}

export function parseFrameCsv(text: string): FrameNodes {
  const nodes: FrameNodes = new Map();
  const lines = text.split("\n");
  for (let i = 1; i < lines.length; i++) {  // 首行表头跳过(与参考实现口径一致)
    const line = lines[i]!;
    if (!line.trim()) continue;
    const cols = line.split(",");
    if (cols.length !== 7) {
      throw new CsvError(`frame 第 ${i + 1} 行应为 7 列,实际 ${cols.length} 列`);
    }
    const id = Number(cols[0]);
    if (!Number.isInteger(id) || id <= 0) {
      throw new CsvError(`frame 第 ${i + 1} 行节点号无效 '${cols[0]!.trim()}'`);
    }
    nodes.set(id, [
      toFloat(cols[1]!, `frame 第 ${i + 1} 行 x`),
      toFloat(cols[2]!, `frame 第 ${i + 1} 行 y`),
      toFloat(cols[3]!, `frame 第 ${i + 1} 行 z`),
      toFloat(cols[4]!, `frame 第 ${i + 1} 行 ux`),
      toFloat(cols[5]!, `frame 第 ${i + 1} 行 uy`),
      toFloat(cols[6]!, `frame 第 ${i + 1} 行 uz`),
    ]);
  }
  if (nodes.size === 0) {
    throw new CsvError("frame 文件没有数据行(仅表头/空)");
  }
  return nodes;
}

function parseLabelValueRows(text: string, what: string): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const comma = line.indexOf(",");
    if (comma < 0) {                        // 撕裂半行:跳过
      console.warn(`[hip-playback] ${what} 跳过无法解析的行:'${line.trim()}'`);
      continue;
    }
    rows.push([line.slice(0, comma), line.slice(comma + 1)]);
  }
  return rows;
}

export function parseProgressCsv(text: string): StageRow[] {
  return parseLabelValueRows(text, "progress").flatMap(([label, value]) => {
    const time = Number(value);
    if (!Number.isFinite(time)) {
      console.warn(`[hip-playback] progress 跳过时间无法解析的行:'${label},${value}'`);
      return [];
    }
    return [{ label: label.trim(), time }];
  });
}

export function parseResultsCsv(text: string): Record<string, number> {
  const values: Record<string, number> = {};
  for (const [label, value] of parseLabelValueRows(text, "results")) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
      console.warn(`[hip-playback] results 跳过数值无法解析的行:'${label},${value}'`);
      continue;
    }
    values[label.trim()] = num;
  }
  return values;
}

export function parseEmapCsv(text: string): EmapTable {
  const lines = text.split("\n").filter((l) => l.trim());
  if (lines.length === 0 || lines[0]!.replace(/[\s,]/g, "").startsWith("elem") === false) {
    // 无表头也容忍(直接从数据行开始),但首行必须能当数据解析
    if (lines.length === 0) throw new CsvError("emap 文件为空");
  }
  const dataLines = lines[0]!.trimStart().startsWith("elem") ? lines.slice(1) : lines;
  const elements: number[][] = [];
  let width = 0;
  for (let i = 0; i < dataLines.length; i++) {
    const cols = dataLines[i]!.split(",").map((c) => Number(c));
    if (cols.length !== 9 && cols.length !== 21 || cols.some((c) => !Number.isFinite(c))) {
      console.warn(`[hip-playback] emap 跳过无法解析的行(第 ${i + 1} 数据行)`);
      continue;
    }
    if (width === 0) width = cols.length;
    else if (cols.length !== width) {
      throw new CsvError(`emap 第 ${i + 1} 数据行列数 ${cols.length} 与首行 ${width} 不一致`);
    }
    elements.push(cols.slice(1).map(Math.round));   // 节点号取整(0.1E+01 → 1)
  }
  if (elements.length === 0) {
    throw new CsvError("emap 文件没有可用数据行");
  }
  return { elements, hasMidnodes: width === 21 };
}
