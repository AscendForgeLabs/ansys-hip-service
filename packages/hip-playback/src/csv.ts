/** CSV 解析层 — passthrough 工件文本 → 结构化数据。

 * 格式口径(docs/passthrough-guide.md / playback-handbook.md §1):
 *   frame_N.csv:首行表头,行 = 节点号,x,y,z,ux,uy,uz(E16.8 科学计数带前导空格);
 *   progress.csv / results.csv:无表头,"标签(≤8 字符,右补空格),数值";
 *   emap.csv(手册 §3 档②):首行表头 elem,n1..nN,行 = 单元号 + 节点号;
 *     四档列宽:9/21 = hex8/hex20,5/11 = tet4/tet10(单文件单一宽度,混类型分文件导出);
 *   epart.csv(侧车):无/有表头均可,行 = "elem,part" 两个正整数,单元号 → part 号。
 *
 * 容错策略:帧文件是完整工件 → 坏数值行抛 CsvError(带行号,不静默);
 * progress/results/emap 可能在作业运行中被读取 → 撕裂半行跳过并 warn,
 * 但全部行都坏时仍抛错(空数据是错误,不是正常态)。
 */

export type Vec6 = readonly [number, number, number, number, number, number];
/** 节点号 → [x, y, z, ux, uy, uz](坐标 = 未变形初始构型,各帧恒同)。 */
export type FrameNodes = Map<number, Vec6>;

/** 单元形状档:由 emap 列宽唯一判定(9/21 列 = hex,5/11 列 = tet)。 */
export type EmapCell = "hex" | "tet";

/** 单元连接表:每单元节点号(hex 8 角点 / hex20 另加 12 棱中点;tet 4 角点 / tet10 另加 6 棱中点)。 */
export interface EmapTable {
  cell: EmapCell;             // 单元形状档(与列宽档一一对应)
  elements: number[][];       // [单元索引] → 节点号数组(8/20/4/10 个)
  hasMidnodes: boolean;       // 有无棱中点列(21/11 列档)
  elemIds?: number[];         // [单元索引] → 单元号(数据行首列);parseEmapCsv 恒填,
                              // 手造字面量可省,消费侧缺席按行序 e+1
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

/** emap 合法列宽 → 形状档:9/21 = hex8/hex20,5/11 = tet4/tet10。 */
const EMAP_WIDTH_CELL: ReadonlyMap<number, EmapCell> = new Map([
  [9, "hex"],
  [21, "hex"],
  [5, "tet"],
  [11, "tet"],
]);

export function parseEmapCsv(text: string): EmapTable {
  const lines = text.split("\n").filter((l) => l.trim());
  if (lines.length === 0 || lines[0]!.replace(/[\s,]/g, "").startsWith("elem") === false) {
    // 无表头也容忍(直接从数据行开始),但首行必须能当数据解析
    if (lines.length === 0) throw new CsvError("emap 文件为空");
  }
  const dataLines = lines[0]!.trimStart().startsWith("elem") ? lines.slice(1) : lines;
  const elements: number[][] = [];
  const elemIds: number[] = [];
  let width = 0;
  let cell: EmapCell = "hex";
  for (let i = 0; i < dataLines.length; i++) {
    const cols = dataLines[i]!.split(",").map((c) => Number(c));
    const rowCell = EMAP_WIDTH_CELL.get(cols.length);
    if (rowCell === undefined || cols.some((c) => !Number.isFinite(c))) {
      console.warn(`[hip-playback] emap 跳过无法解析的行(第 ${i + 1} 数据行)`);
      continue;
    }
    if (width === 0) {
      width = cols.length;
      cell = rowCell;
    } else if (cols.length !== width) {
      throw new CsvError(
        `emap 第 ${i + 1} 数据行列数 ${cols.length} 与首行 ${width} 不一致:` +
          "emap 需单一单元宽度(9/21/5/11 = hex8/hex20/tet4/tet10),混合单元类型请分文件导出",
      );
    }
    elemIds.push(Math.round(cols[0]!));
    elements.push(cols.slice(1).map(Math.round));   // 节点号取整(0.1E+01 → 1)
  }
  if (elements.length === 0) {
    throw new CsvError("emap 文件没有可用数据行");
  }
  return { cell, elements, hasMidnodes: width === 21 || width === 11, elemIds };
}

/** epart.csv 侧车:单元号 → part 号(单元物理归属,供过滤/着色等进阶消费)。 */
export interface EpartTable {
  byElem: ReadonlyMap<number, number>;
}

/** 解析 epart.csv:行 = "elem,part" 两个正整数;表头/坏行容忍与 emap 同策略。
 *
 * 重复单元号后写覆盖先写:上游 *VWRITE 追加导出可能有重复行,以最后写入的归属为准
 * (Map.set 天然后写覆盖)。
 */
export function parseEpartCsv(text: string): EpartTable {
  const byElem = new Map<number, number>();
  const lines = text.split("\n").filter((l) => l.trim());
  if (lines.length === 0) {
    throw new CsvError("epart 文件为空");
  }
  // 首行以 elem 开头(去空白/逗号后)视为表头跳过;无表头容忍,与 emap 同策略
  const dataLines = lines[0]!.replace(/[\s,]/g, "").startsWith("elem") ? lines.slice(1) : lines;
  for (let i = 0; i < dataLines.length; i++) {
    const cols = dataLines[i]!.split(",").map((c) => Number(c));
    const isValid =
      cols.length === 2 && cols.every((c) => Number.isInteger(c) && c > 0);
    if (!isValid) {
      console.warn(`[hip-playback] epart 跳过无法解析的行(第 ${i + 1} 数据行)`);
      continue;
    }
    byElem.set(cols[0]!, cols[1]!);   // 重复单元号:后写覆盖先写
  }
  if (byElem.size === 0) {
    throw new CsvError("epart 文件没有可用数据行");
  }
  return { byElem };
}
