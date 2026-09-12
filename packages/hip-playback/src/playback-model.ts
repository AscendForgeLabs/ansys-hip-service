/** 回放纯函数模型 — 逐语义移植参考实现(cube.template.html 的 segOf/depthAt/帧插值)。
 *
 * 时间轴约定:t=0 为零位移装料态,t∈(0,nseg] 覆盖 nseg 个位移帧。
 * 帧数组约定:frames[k] 为第 k 帧平铺位移(frames[0] = 初始基准),
 * segOf 返回 [基帧索引, 帧内比例],渲染按 frames[基]→frames[基+1] 线性插值。
 */

export function segOf(t: number, nseg: number): [number, number] | null {
  if (t <= 0) return null;                                // 零位移装料态
  const tf = Math.min(t, nseg);
  const fi = Math.min(Math.floor(tf - 1e-9), nseg - 1);   // 当前到达帧 0..nseg-1
  return fi <= 0 ? [0, tf] : [fi - 1, tf - fi];           // [基帧, 帧内比例]
}

export function depthAt(t: number, nseg: number, depth: readonly number[]): number {
  if (depth.length < 2) {
    throw new Error("depthAt:depth 至少需要 2 个采样点");
  }
  const tf = Math.max(0, Math.min(t, nseg));
  const i = Math.min(Math.floor(tf), depth.length - 2);
  return depth[i]! + (depth[i + 1]! - depth[i]!) * (tf - i);
}

export function interpolateFrame(
  frameA: readonly number[],
  frameB: readonly number[],
  frac: number,
): number[] {
  if (frameA.length !== frameB.length) {
    throw new Error(
      `interpolateFrame:两帧长度不一致(${frameA.length} vs ${frameB.length})`,
    );
  }
  return frameA.map((a, i) => a + (frameB[i]! - a) * frac);
}
