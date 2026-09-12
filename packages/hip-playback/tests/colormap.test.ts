/** 色带测试:锚点逐字节一致、端点/段边界、越界截断、绿色通道单调不回退。 */
import { describe, expect, it } from "vitest";

import { VIRIDIS_STOPS, viridis } from "../src/colormap";

const EXPECTED_STOPS: ReadonlyArray<readonly [number, number, number]> = [
  [0x44, 0x01, 0x54],
  [0x3b, 0x52, 0x8b],
  [0x21, 0x91, 0x8c],
  [0x5e, 0xc9, 0x62],
  [0xfd, 0xe7, 0x25],
];

describe("VIRIDIS_STOPS", () => {
  it("5 个锚点与参考实现逐字节一致", () => {
    expect(VIRIDIS_STOPS).toEqual(EXPECTED_STOPS);
  });
});

describe("viridis", () => {
  it("v=0 → 首锚点(0-255 归一到 0-1)", () => {
    const [r, g, b] = viridis(0);
    expect(r).toBeCloseTo(0x44 / 255, 6);
    expect(g).toBeCloseTo(0x01 / 255, 6);
    expect(b).toBeCloseTo(0x54 / 255, 6);
  });

  it("v=1 → 末锚点(f=1 精确落在 stop[4])", () => {
    const [r, g, b] = viridis(1);
    expect(r).toBeCloseTo(0xfd / 255, 6);
    expect(g).toBeCloseTo(0xe7 / 255, 6);
    expect(b).toBeCloseTo(0x25 / 255, 6);
  });

  it("v=0.25/0.5/0.75 → 段边界精确命中中间锚点", () => {
    expect(viridis(0.25)).toEqual([0x3b / 255, 0x52 / 255, 0x8b / 255]);
    expect(viridis(0.5)).toEqual([0x21 / 255, 0x91 / 255, 0x8c / 255]);
    expect(viridis(0.75)).toEqual([0x5e / 255, 0xc9 / 255, 0x62 / 255]);
  });

  it("越界截断:v<0 同 v=0,v>1 同 v=1", () => {
    expect(viridis(-0.42)).toEqual(viridis(0));
    expect(viridis(3.7)).toEqual(viridis(1));
  });

  it("中段采样:绿色通道严格递增(不回退),输出恒在 [0,1]", () => {
    let lastG = -1;
    for (let v = 0; v <= 1.0001; v += 0.05) {
      const [r, g, b] = viridis(v);
      expect(g).toBeGreaterThan(lastG);
      lastG = g;
      for (const c of [r, g, b]) {
        expect(c).toBeGreaterThanOrEqual(0);
        expect(c).toBeLessThanOrEqual(1);
      }
    }
  });
});
