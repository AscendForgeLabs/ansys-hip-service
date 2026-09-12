/** 共享纯几何工具 — emap(hex)/tet 两条构网路径共用的三维向量算术与定向核。
 *
 * 自 emap.ts 原样搬移(零语义变化,emap 的 golden 对拍钉死行为):
 *   sub3/cross3/dot3/mean3 向量算术、posOf/uOf 节点行取坐标/位移
 *   (数值口径 round4/round5)、DEGENERATE_EPS 退化阈值、bboxMaxSide 包围盒;
 * orientNormal 为原 emap 的 outwardNormalOf 定向核泛化:参照外向向量
 * 由调用方给定(emap = 角点均值 − 单元中心,tet 同式),几何法线与参照
 * 点积 < 0 则翻转。emap(hex)分支与离线参考实现 build_viewer.py 逐条
 * 对拍,见其模块 docstring;tet 分支见 src/mesh/tet.ts。
 */
import type { FrameNodes, Vec6 } from "../csv";
import { round4, round5 } from "./common";

export type Vec3 = readonly [number, number, number];

/** 法线长度低于此值视为退化面(角点共线/重合,无法定向)。 */
export const DEGENERATE_EPS = 1e-12;

export const sub3 = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const cross3 = (a: Vec3, b: Vec3): Vec3 => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
export const dot3 = (a: Vec3, b: Vec3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const mean3 = (vs: ReadonlyArray<Vec3>): Vec3 => [
  vs.reduce((s, v) => s + v[0], 0) / vs.length,
  vs.reduce((s, v) => s + v[1], 0) / vs.length,
  vs.reduce((s, v) => s + v[2], 0) / vs.length,
];
export const posOf = (v: Vec6): Vec3 => [round4(v[0]), round4(v[1]), round4(v[2])];
export const uOf = (v: Vec6): Vec3 => [round5(v[3]), round5(v[4]), round5(v[5])];

/** 面单位几何法线与绕向翻转标志:(p1−p0)×(p2−p0) 单位化后与参照外向向量
 * 点积 < 0 则取反并翻三角绕向;退化面(|n| < DEGENERATE_EPS)抛中文错误,
 * what 注明面来源与角点号(emap/tet 各自的错误前缀)。 */
export function orientNormal(
  cornerPos: ReadonlyArray<Vec3>,
  refOutward: Vec3,
  what: string,
): { normal: Vec3; flip: boolean } {
  const geometric = cross3(
    sub3(cornerPos[1]!, cornerPos[0]!),
    sub3(cornerPos[2]!, cornerPos[0]!),
  );
  const len = Math.hypot(geometric[0], geometric[1], geometric[2]);
  if (len < DEGENERATE_EPS) {
    throw new Error(`${what}退化(角点共线或重合),无法定向`);
  }
  const unit: Vec3 = [geometric[0] / len, geometric[1] / len, geometric[2] / len];
  if (dot3(unit, refOutward) >= 0) {
    return { normal: unit, flip: false };
  }
  return { normal: [-unit[0], -unit[1], -unit[2]], flip: true };
}

/** 三轴包围盒最大边长(meta.side 口径)。 */
export function bboxMaxSide(first: FrameNodes): number {
  let lo: Vec3 = [Infinity, Infinity, Infinity];
  let hi: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (const v of first.values()) {
    lo = [Math.min(lo[0], v[0]), Math.min(lo[1], v[1]), Math.min(lo[2], v[2])];
    hi = [Math.max(hi[0], v[0]), Math.max(hi[1], v[1]), Math.max(hi[2], v[2])];
  }
  return round4(Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]));
}
