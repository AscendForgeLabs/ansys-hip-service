/** 回放纯函数模型测试:segOf/depthAt/interpolateFrame 逐公式精确断言。
 *
 * 语义移植自参考实现(cube.template.html):
 *   segOf:fi = min(floor(tf-1e-9), nseg-1);fi<=0 → [0,tf] 否则 [fi-1, tf-fi]。
 *   注:t=nseg 按公式得 [nseg-2, 1](数值上 = 末帧,且基帧+1 不越界)。
 */
import { describe, expect, it } from "vitest";

import { depthAt, interpolateFrame, segOf } from "../src/playback-model";

describe("segOf", () => {
  it("t<=0 → null(零位移装料态)", () => {
    expect(segOf(0, 6)).toBeNull();
    expect(segOf(-1, 6)).toBeNull();
  });

  it("t∈(0,1] → [0, t](从零位移插到帧 1)", () => {
    expect(segOf(0.25, 6)).toEqual([0, 0.25]);
    expect(segOf(1, 6)).toEqual([0, 1]);
  });

  it("t=1.5(nseg≥2)→ 按公式 [0, 0.5]", () => {
    expect(segOf(1.5, 6)).toEqual([0, 0.5]);
    expect(segOf(1.5, 2)).toEqual([0, 0.5]);
  });

  it("中段 t=k+φ → [k-1, φ]", () => {
    expect(segOf(2.25, 6)).toEqual([1, 0.25]);
    expect(segOf(4.75, 6)).toEqual([3, 0.75]);
  });

  it("整数 t 的浮点防护:t=2 → [0, 1](帧 1 末,不跳段)", () => {
    expect(segOf(2, 6)).toEqual([0, 1]);
  });

  it("t=nseg → 末帧 [nseg-2, 1]", () => {
    expect(segOf(6, 6)).toEqual([4, 1]);
    expect(segOf(3, 3)).toEqual([1, 1]);
  });

  it("t>nseg → 钳制到末帧", () => {
    expect(segOf(9.5, 6)).toEqual([4, 1]);
  });
});

describe("depthAt", () => {
  const depth = [0, 0.5, 1.2, 1.8];                 // 4 采样点,nseg=3

  it("端点:t=0 → d[0];t=nseg → 末值", () => {
    expect(depthAt(0, 3, depth)).toBeCloseTo(0, 12);
    expect(depthAt(3, 3, depth)).toBeCloseTo(1.8, 12);
  });

  it("采样点处精确命中:t=1 → d[1],t=2 → d[2]", () => {
    expect(depthAt(1, 3, depth)).toBeCloseTo(0.5, 12);
    expect(depthAt(2, 3, depth)).toBeCloseTo(1.2, 12);
  });

  it("中点线性插值", () => {
    expect(depthAt(0.5, 3, depth)).toBeCloseTo(0.25, 12);
    expect(depthAt(1.5, 3, depth)).toBeCloseTo(0.85, 12);
  });

  it("t 越界钳制:t<0 → d[0],t>nseg → 末值", () => {
    expect(depthAt(-5, 3, depth)).toBeCloseTo(0, 12);
    expect(depthAt(99, 3, depth)).toBeCloseTo(1.8, 12);
  });

  it("depth 少于 2 个采样点 → 抛错(而非静默 NaN)", () => {
    expect(() => depthAt(1, 1, [0.5])).toThrow(/至少需要 2/);
  });
});

describe("interpolateFrame", () => {
  it("端点 frac=0/1 → 原帧数值", () => {
    const a = [1, -2, 3, 0];
    const b = [5, 2, -1, 0.5];
    expect(interpolateFrame(a, b, 0)).toEqual(a);
    expect(interpolateFrame(a, b, 1)).toEqual(b);
  });

  it("中段逐分量 a+(b-a)*f", () => {
    expect(interpolateFrame([0, 10, -4], [4, 0, 4], 0.5)).toEqual([2, 5, 0]);
    expect(interpolateFrame([1, 1], [3, 5], 0.25)).toEqual([1.5, 2]);
  });

  it("返回新数组,不改入参(不可变)", () => {
    const a = [1, 2];
    const b = [3, 4];
    const out = interpolateFrame(a, b, 0.5);
    expect(out).not.toBe(a);
    expect(out).not.toBe(b);
    expect(a).toEqual([1, 2]);
    expect(b).toEqual([3, 4]);
  });

  it("两帧长度不一致 → 抛错", () => {
    expect(() => interpolateFrame([1, 2], [1, 2, 3], 0.5)).toThrow(/长度不一致/);
  });
});
