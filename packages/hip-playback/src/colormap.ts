/** viridis 色带 — 与参考实现(cube.template.html 的 cmap)逐参数一致。
 *
 * 5 个 0-255 锚点、4 段线性插值:v 先截断到 [0,1] 再放大到 [0,4];
 * i = min(floor(v), 段数-2) 防越段,f = v - i 为段内比例;输出 0-1 浮点三元组。
 */

export const VIRIDIS_STOPS: ReadonlyArray<readonly [number, number, number]> = [
  [0x44, 0x01, 0x54],
  [0x3b, 0x52, 0x8b],
  [0x21, 0x91, 0x8c],
  [0x5e, 0xc9, 0x62],
  [0xfd, 0xe7, 0x25],
];

export function viridis(v: number): [number, number, number] {
  const scaled = Math.max(0, Math.min(1, v)) * (VIRIDIS_STOPS.length - 1);
  const i = Math.min(Math.floor(scaled), VIRIDIS_STOPS.length - 2);
  const f = scaled - i;
  const a = VIRIDIS_STOPS[i]!;
  const b = VIRIDIS_STOPS[i + 1]!;
  return [
    (a[0] + (b[0] - a[0]) * f) / 255,
    (a[1] + (b[1] - a[1]) * f) / 255,
    (a[2] + (b[2] - a[2]) * f) / 255,
  ];
}
