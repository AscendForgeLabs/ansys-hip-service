/** 构网共用装配:色标最大值 / 压深序列 / 兜底阶段表 / 数值口径。

 * 全部纯函数;lattice 与 emap 两条构网路径共用,保证两条路产出的
 * MeshData 在同一份帧数据上 meta/stages 逐值一致(交叉验证的前提)。
 */

import type { FrameNodes, Vec6 } from "../csv";
import type { MeshInput, StageInfo } from "./types";

/** 阶段表的标签/时间部分(depth 由调用方按帧序列补,见 depthSeries)。 */
export type StageLabelsTimes = Omit<StageInfo, "depth">;

// 舍入口径 = Python round() 语义(十进制正确舍入,ties away from even 的差异由
// toFixed 承担):与参考实现 build_viewer.py 的 round(v,4)/round(v,5) 逐位一致,
// 是 golden 对拍 bit-exact 的前提(lattice/emap 两路共用,交叉验证才可比)。
export const round4 = (v: number): number => Number(v.toFixed(4));
export const round5 = (v: number): number => Number(v.toFixed(5));

/** 全帧全局 max|u|(云图归一分母;与参考实现同口径)。 */
export function colorMaxOf(framesU: ReadonlyArray<ReadonlyArray<number>>): number {
  let max = 0;
  for (const fu of framesU) {
    for (let i = 0; i + 2 < fu.length; i += 3) {
      const u = Math.hypot(fu[i]!, fu[i + 1]!, fu[i + 2]!);
      if (u > max) max = u;
    }
  }
  return round4(max);
}

/** 压深序列:[0] + 逐帧 |min uy|(顶面压头类载荷语义,HUD 显示口径)。 */
export function depthSeries(frames: ReadonlyArray<FrameNodes>): number[] {
  const depths = [0];
  for (const frame of frames) {
    let minUy = 0;
    for (const v of frame.values()) minUy = Math.min(minUy, v[4]);
    depths.push(round4(-minUy));
  }
  return depths;
}

/** 兜底阶段表:progress.csv 缺席时用 MESH,F1..FN / 0 秒(参考实现同款)。 */
export function fallbackStages(nseg: number): StageLabelsTimes {
  return {
    label: ["MESH", ...Array.from({ length: nseg }, (_, i) => `F${i + 1}`)],
    time: [0, ...Array<number>(nseg).fill(0)],
  };
}

export function buildStages(input: MeshInput, nseg: number): StageLabelsTimes {
  const labels = input.stageLabels ?? [];
  const times = input.stageTimes ?? [];
  if (labels.length === 0 || times.length === 0) return fallbackStages(nseg);
  return { label: labels, time: times };
}

/** 位移幅值(工具,lattice/emap 构网中途取 |u| 用)。 */
export const magnitudeOf = (v: Vec6): number => Math.hypot(v[3], v[4], v[5]);
