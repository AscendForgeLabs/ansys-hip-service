/** 按部件渲染与 epart 接线:分组装配 / 层×部件可见性正交 / 部件 chip / 元素端到端。
 *
 * 分层取舍(场景测试受 happy-dom 无 WebGL 限制,真 PlaybackScene 构造即抛):
 *   - groupFacesByPart / VisibilityState = scene 可测逻辑层(纯函数 + 纯状态,
 *     不依赖 THREE),分组索引装配与正交显隐语义在此精确钉测;
 *   - 元素级用例以 FakeScene 替身(scene 模块 mock,其余导出保真)记录
 *     setMesh/setLayerVisible/setPartVisible 调用验证接线;HUD chip、chip
 *     事件委托、sub/note 文案走真实 shadow DOM 断言;
 *   - 单部件(无 epart)路径等价性 = "无 part 标注 → 单组"装配用例 +
 *     既有 lattice/emap/tet 回归用例护航,不新增重复断言。
 */
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { defineHipPlayback, HipPlaybackElement } from "../src/element";
import type { FaceGeom, MeshData } from "../src/mesh/types";
import { groupFacesByPart, PlaybackScene, VisibilityState } from "../src/scene";
import { buildHud, configureHud, resetChipStates } from "../src/ui";
import type { HudHandles } from "../src/ui";

// ---- scene 替身:仅元素级用例消费;其余导出保真(groupFacesByPart 等) ----
vi.mock("../src/scene", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/scene")>();
  class FakeScene {
    static readonly instances: FakeScene[] = [];
    mesh: MeshData | null = null;
    layers: Array<[string, boolean]> = [];
    parts: Array<[number, boolean]> = [];
    shellOn = true;                        // 模拟真场景层状态(VisibilityState 口径)
    wireOn = true;
    partsOn = new Set<number>();           // 模拟真场景部件表(setMesh 清空)
    constructor(_canvas: HTMLCanvasElement) {
      FakeScene.instances.push(this);
    }
    setMesh(data: MeshData): void {
      this.mesh = data;
      this.shellOn = true;                 // 对齐真场景 reset(层 + 部件全复位)
      this.wireOn = true;
      this.partsOn = new Set();
    }
    resize(): void {}
    update(): { uMax: number; depth: number } { return { uMax: 0, depth: 0 }; }
    resetView(): void {}
    consumeDirty(): boolean { return false; }
    render(): void {}
    dispose(): void {}
    setLayerVisible(layer: string, visible: boolean): void {
      this.layers.push([layer, visible]);
      if (layer === "shell") this.shellOn = visible;
      else if (layer === "wire") this.wireOn = visible;
    }
    setPartVisible(part: number, visible: boolean): void {
      this.parts.push([part, visible]);
      if (visible) this.partsOn.add(part);
      else this.partsOn.delete(part);
    }
  }
  return { ...actual, PlaybackScene: FakeScene };
});

const load = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), "utf-8");

// ---- 清理:用例挂到 body 的宿主/元素统一移除(rAF 循环随 disconnect 停) ----
const trash: HTMLElement[] = [];
afterEach(() => {
  for (const node of trash.splice(0)) node.remove();
});
const track = <T extends HTMLElement>(node: T): T => {
  trash.push(node);
  return node;
};

/** FakeScene 的可断言面(调用流水 + 模拟状态),元素级用例取"最近实例"用。 */
interface FakeSceneLike {
  mesh: MeshData | null;
  layers: Array<[string, boolean]>;
  parts: Array<[number, boolean]>;
  shellOn: boolean;
  wireOn: boolean;
  partsOn: Set<number>;
}
const lastFake = (): FakeSceneLike =>
  (PlaybackScene as unknown as { instances: FakeSceneLike[] }).instances.at(-1)!;

// ---- tet 小 fixture:两 tet4 共享面 (1,2,3),part 1/2 各一单元(字面量 emap
// 不带 elemIds → 天然走行序 e+1 回退,emap 5 列 = tet4 档) ----
const TET_TEXTS: Record<string, string> = {
  "frame_1.csv":
    "id,x,y,z,ux,uy,uz\n" +
    "1,0,0,0,0,0,0\n2,1,0,0,0,0,0\n3,0,1,0,0,0,0\n4,0,0,1,0,0,0\n5,0,0,-1,0,0,0\n",
  "emap.csv": "elem,n1,n2,n3,n4\n1,1,2,3,4\n2,1,2,3,5\n",
  "epart.csv": "elem,part\n1,1\n2,2\n",
};

// hex fixture:借合成工件的真实 hex emap(hex+epart 分发报错在构网前抛出)
const HEX_TEXTS: Record<string, string> = {
  "frame_1.csv": load("fixtures/synthetic/artifacts/frame_1.csv"),
  "emap.csv": load("fixtures/synthetic/artifacts/emap.csv"),
  "epart.csv": "elem,part\n1,1\n",
};

// lattice fixture:同一份合成工件只喂 frames(2×2×2 hex20 正则格 → 规则格反推;
// lattice 模式才有 ghost/punch chip)
const LATTICE_TEXTS: Record<string, string> = {
  "frame_1.csv": load("fixtures/synthetic/artifacts/frame_1.csv"),
  "frame_2.csv": load("fixtures/synthetic/artifacts/frame_2.csv"),
  "frame_3.csv": load("fixtures/synthetic/artifacts/frame_3.csv"),
};

describe("groupFacesByPart 分组装配", () => {
  const face = (part: number | undefined, tris: number[], wire: number[]): FaceGeom =>
    part === undefined ? { tris, wire } : { tris, wire, part };

  it("两部件 → 两组升序,组内 tris/wire 按面序拼接", () => {
    const groups = groupFacesByPart([
      face(2, [0, 1, 2], [0, 1, 1, 2]),
      face(1, [3, 4, 5], [3, 4]),
      face(2, [6, 7, 8], [5, 6]),
    ]);
    expect(groups.map((g) => g.part)).toEqual([1, 2]);
    expect(groups[0]!.tris).toEqual([3, 4, 5]);
    expect(groups[0]!.wire).toEqual([3, 4]);
    expect(groups[1]!.tris).toEqual([0, 1, 2, 6, 7, 8]);
    expect(groups[1]!.wire).toEqual([0, 1, 1, 2, 5, 6]);
  });

  it("无 part 标注(单部件路径)→ 单组 part 0,索引不重不漏", () => {
    const groups = groupFacesByPart([
      face(undefined, [1, 2, 3], [9, 8]),
      face(undefined, [4, 5, 6], [7, 7]),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.part).toBe(0);
    expect(groups[0]!.tris).toEqual([1, 2, 3, 4, 5, 6]);
    expect(groups[0]!.wire).toEqual([9, 8, 7, 7]);
  });

  it("epart 缺行的 part 0 与其他部件混排 → 0 组参与升序", () => {
    const groups = groupFacesByPart([
      face(3, [1], [1]),
      face(undefined, [2], [2]),
      face(1, [3], [3]),
    ]);
    expect(groups.map((g) => g.part)).toEqual([0, 1, 3]);
  });

  it("空 faces → 空组(防御)", () => {
    expect(groupFacesByPart([])).toEqual([]);
  });
});

describe("VisibilityState 层 × 部件正交", () => {
  it("默认全可见;关部件 1 只影响部件 1(面与网格线同灭)", () => {
    const v = new VisibilityState();
    expect(v.faceVisible(1)).toBe(true);
    expect(v.wireVisible(1)).toBe(true);
    v.setPart(1, false);
    expect(v.faceVisible(1)).toBe(false);
    expect(v.wireVisible(1)).toBe(false);
    expect(v.faceVisible(2)).toBe(true);
    expect(v.wireVisible(2)).toBe(true);
  });

  it("层开关与部件开关正交:层关全灭,层开恢复部件各自状态", () => {
    const v = new VisibilityState();
    v.setPart(1, false);
    v.setLayer("shell", false);
    expect(v.faceVisible(1)).toBe(false);
    expect(v.faceVisible(2)).toBe(false);      // shell 层关 → 全部面灭
    v.setLayer("shell", true);
    expect(v.faceVisible(1)).toBe(false);      // 部件 1 保持隐藏(状态不丢)
    expect(v.faceVisible(2)).toBe(true);
  });

  it("wire 层独立于 shell 层;部件关同时压住该部件 wire", () => {
    const v = new VisibilityState();
    v.setLayer("wire", false);
    expect(v.wireVisible(1)).toBe(false);
    expect(v.faceVisible(1)).toBe(true);       // shell 不受 wire 开关影响
    v.setPart(3, false);
    v.setLayer("wire", true);
    expect(v.wireVisible(3)).toBe(false);      // 部件 3 保持隐藏
    expect(v.wireVisible(1)).toBe(true);
  });

  it("reset 全复位(换装/重连 = 全部默认 on):层与部件都回可见", () => {
    const v = new VisibilityState();
    v.setPart(1, false);
    v.setLayer("wire", false);
    v.reset();
    expect(v.faceVisible(1)).toBe(true);       // 部件表清空
    expect(v.wireVisible(1)).toBe(true);       // 层状态同复位
    expect(v.faceVisible(2)).toBe(true);
  });
});

describe("configureHud 部件 chip", () => {
  const host = (): HTMLElement => track(document.body.appendChild(document.createElement("div")));
  const buildHandles = (): HudHandles => {
    const h = host();
    h.attachShadow({ mode: "open" });
    return buildHud(h);
  };
  const base = { nseg: 3, nverts: 10, title: "HIP 三维回放", sub: "s", note: "n" };
  const partChips = (h: HudHandles): HTMLElement[] =>
    [...h.tagsRoot.querySelectorAll<HTMLElement>('[data-layer^="part:"]')];

  it("parts=[1,2] → 两枚 chip(文案 / data-layer)", () => {
    const h = buildHandles();
    configureHud(h, { ...base, mode: "emap", parts: [1, 2] });
    expect(partChips(h).map((c) => c.textContent)).toEqual(["部件 1", "部件 2"]);
    expect(partChips(h).map((c) => c.dataset.layer)).toEqual(["part:1", "part:2"]);
  });

  it("重载 parts=[5] → 上一轮 chip 清除,只剩新 chip", () => {
    const h = buildHandles();
    configureHud(h, { ...base, mode: "emap", parts: [1, 2] });
    configureHud(h, { ...base, mode: "emap", parts: [5] });
    expect(partChips(h).map((c) => c.textContent)).toEqual(["部件 5"]);
    expect(h.partChips).toHaveLength(1);
    expect(h.partChips[0]!.dataset.layer).toBe("part:5");
  });

  it("parts 缺席/空 → 无 chip;shell/wire 固定 chip 不受重载影响", () => {
    const h = buildHandles();
    configureHud(h, { ...base, mode: "emap", parts: [1, 2] });
    configureHud(h, { ...base, mode: "emap" });
    expect(partChips(h)).toHaveLength(0);
    expect(h.tagsRoot.querySelector('[data-layer="shell"]')).not.toBeNull();
    expect(h.tagsRoot.querySelector('[data-layer="wire"]')).not.toBeNull();
    configureHud(h, { ...base, mode: "emap", parts: [] });
    expect(partChips(h)).toHaveLength(0);
  });

  it("重载(configureHud):被点灭的 shell/wire 静态 chip 补回 on(换装 = 全默认)", () => {
    const h = buildHandles();
    configureHud(h, { ...base, mode: "emap", parts: [1] });
    h.tagsRoot.querySelector<HTMLElement>('[data-layer="shell"]')!.classList.remove("on");
    h.tagsRoot.querySelector<HTMLElement>('[data-layer="wire"]')!.classList.remove("on");
    configureHud(h, { ...base, mode: "emap", parts: [5] });
    expect(h.tagsRoot.querySelector<HTMLElement>('[data-layer="shell"]')!
      .classList.contains("on")).toBe(true);
    expect(h.tagsRoot.querySelector<HTMLElement>('[data-layer="wire"]')!
      .classList.contains("on")).toBe(true);
  });

  it("resetChipStates:tagsRoot 下全部 .hip-tag 统一恢复 on", () => {
    const h = buildHandles();
    configureHud(h, { ...base, mode: "lattice", parts: [1, 2] });   // lattice 带 ghost/punch
    for (const chip of h.tagsRoot.querySelectorAll<HTMLElement>(".hip-tag")) {
      chip.classList.remove("on");
    }
    resetChipStates(h.tagsRoot);
    const all = [...h.tagsRoot.querySelectorAll<HTMLElement>(".hip-tag")];
    expect(all.length).toBeGreaterThanOrEqual(5);                   // shell/wire/ghost/punch/部件
    expect(all.every((c) => c.classList.contains("on"))).toBe(true);
  });
});

describe("<hip-playback> epart 端到端", () => {
  defineHipPlayback();

  const mountEl = (): HipPlaybackElement =>
    track(document.body.appendChild(document.createElement("hip-playback"))) as HipPlaybackElement;
  const meshOf = (el: HipPlaybackElement): MeshData | null =>
    (el as unknown as { mesh: MeshData | null }).mesh;
  const partChipsOf = (el: HipPlaybackElement): HTMLElement[] =>
    [...el.shadowRoot!.querySelectorAll<HTMLElement>('[data-layer^="part:"]')];

  it("tet frame+emap+epart:mesh 分部件正确、sub 文案带部件数、note 追加 epart.csv", async () => {
    const el = mountEl();
    await el.loadData(TET_TEXTS);
    const mesh = meshOf(el);
    expect(mesh).not.toBeNull();
    expect(mesh!.parts).toEqual([1, 2]);
    expect(mesh!.cell).toBe("tet");
    expect(mesh!.faces).toHaveLength(6);       // 共享面剔除后恰 6 边界面
    expect(mesh!.faces.slice(0, 3).map((f) => f.part)).toEqual([1, 1, 1]);
    expect(mesh!.faces.slice(3).map((f) => f.part)).toEqual([2, 2, 2]);
    const shadow = el.shadowRoot!;
    expect(shadow.querySelector(".sub")!.textContent).toContain("tet 皮肤 · 2 部件");
    const note = shadow.querySelector(".note")!.textContent!;
    expect(note).toContain("+ emap.csv");
    expect(note).toContain("+ epart.csv");
    // 替身场景收到同一份 mesh(渲染接线通)
    expect(lastFake().mesh).toBe(mesh);
  });

  it("tet 无 epart:sub 为 `tet 皮肤`(无部件数),无部件 chip", async () => {
    const el = mountEl();
    const { "epart.csv": _drop, ...noEpart } = TET_TEXTS;
    await el.loadData(noEpart);
    expect(meshOf(el)!.parts).toBeUndefined();
    expect(el.shadowRoot!.querySelector(".sub")!.textContent).toContain("tet 皮肤");
    expect(el.shadowRoot!.querySelector(".sub")!.textContent).not.toContain("部件");
    expect(el.shadowRoot!.querySelectorAll('[data-layer^="part:"]')).toHaveLength(0);
    expect(el.shadowRoot!.querySelector(".note")!.textContent).not.toContain("epart.csv");
  });

  it("hex frame+emap+epart → 中文报错(错误态 + error 事件,mesh 不落地)", async () => {
    const el = mountEl();
    const errors: ErrorEvent[] = [];
    el.addEventListener("error", (e) => errors.push(e as ErrorEvent));
    await el.loadData(HEX_TEXTS);
    expect(errors).toHaveLength(1);
    expect((errors[0]!.error as Error).message).toMatch(/epart 部件分组当前仅支持 tet 皮肤/);
    expect(el.shadowRoot!.textContent).toContain("回放数据加载失败");
    expect(meshOf(el)).toBeNull();
  });

  it("点击部件 chip → scene.setPartVisible;shell chip 仍走 setLayerVisible(正交接线)", async () => {
    const el = mountEl();
    await el.loadData(TET_TEXTS);
    const fake = lastFake();
    const chip1 = el.shadowRoot!.querySelector<HTMLElement>('[data-layer="part:1"]')!;
    chip1.click();
    expect(fake.parts).toEqual([[1, false]]);
    chip1.click();                             // 再点恢复
    expect(fake.parts).toEqual([[1, false], [1, true]]);
    const shell = el.shadowRoot!.querySelector<HTMLElement>('[data-layer="shell"]')!;
    shell.click();
    expect(fake.layers).toEqual([["shell", false]]);
    // 全程未动部件状态:层开关与部件开关各走各的入口
    expect(fake.parts).toEqual([[1, false], [1, true]]);
  });

  it("畸形 part chip(data-layer 非整数)→ 忽略,不调 setPartVisible", async () => {
    const el = mountEl();
    await el.loadData(TET_TEXTS);
    const fake = lastFake();
    const bogus = document.createElement("span");
    bogus.className = "hip-tag";               // 无 on → 首次点击语义 = 开;guard 拦截不翻转场景
    bogus.dataset.layer = "part:abc";
    el.shadowRoot!.querySelector<HTMLElement>("#hip-tags")!.append(bogus);
    bogus.click();
    expect(fake.parts).toEqual([]);            // 畸形值忽略(不产生 NaN 部件号)
    bogus.dataset.layer = "part:2";
    bogus.click();
    expect(fake.parts).toEqual([[2, false]]);  // 合法值照常分发
  });

  it("关 shell/part → 重载新工件:chips 与画面一致回到全默认 on(复位口径)", async () => {
    const el = mountEl();
    await el.loadData(TET_TEXTS);
    const scene = lastFake();
    el.shadowRoot!.querySelector<HTMLElement>('[data-layer="shell"]')!.click();
    el.shadowRoot!.querySelector<HTMLElement>('[data-layer="part:1"]')!.click();
    expect(scene.shellOn).toBe(false);
    expect(scene.parts).toEqual([[1, false]]);   // 两 chip 均已点灭

    await el.loadData(TET_TEXTS);              // 重载:同场景换装,显隐全复位
    expect(lastFake()).toBe(scene);            // 场景实例复用
    const all = [...el.shadowRoot!.querySelectorAll<HTMLElement>(".hip-tag")];
    expect(all.every((c) => c.classList.contains("on"))).toBe(true);   // chips 全 on
    expect(scene.shellOn).toBe(true);          // 画面同态:层复位
    expect(scene.partsOn.size).toBe(0);        // 画面同态:部件表清空
  });

  it("切 part chip → 断连重连:chips 全 on,与全新场景(全默认)一致", async () => {
    const el = mountEl();
    await el.loadData(TET_TEXTS);
    el.shadowRoot!.querySelector<HTMLElement>('[data-layer="part:1"]')!.click();
    el.shadowRoot!.querySelector<HTMLElement>('[data-layer="shell"]')!.click();
    const before = lastFake();
    expect(before.shellOn).toBe(false);

    el.remove();                               // 断连(scene dispose)
    document.body.append(el);                  // 重连(rebuildScene)
    const rebuilt = lastFake();
    expect(rebuilt).not.toBe(before);          // 场景已换新(全默认可见)
    const all = [...el.shadowRoot!.querySelectorAll<HTMLElement>(".hip-tag")];
    expect(all.every((c) => c.classList.contains("on"))).toBe(true);   // chips 全 on
    expect(rebuilt.shellOn).toBe(true);        // 与 chips 一致
    expect(rebuilt.partsOn.size).toBe(0);
  });

  it("lattice 关 ghost/punch → 断连重连:全部 chip 类别复位 on 且与画面一致", async () => {
    const el = mountEl();
    await el.loadData(LATTICE_TEXTS);
    expect(meshOf(el)!.mode).toBe("lattice");
    const before = lastFake();
    for (const layer of ["shell", "wire", "ghost", "punch"]) {
      el.shadowRoot!.querySelector<HTMLElement>(`[data-layer="${layer}"]`)!.click();
    }
    expect(before.layers).toEqual([
      ["shell", false], ["wire", false], ["ghost", false], ["punch", false],
    ]);

    el.remove();                               // 断连(scene dispose)
    document.body.append(el);                  // 重连(rebuildScene)
    const rebuilt = lastFake();
    expect(rebuilt).not.toBe(before);
    // 全部 chip 类别复位 on(lattice 无部件 chip,恰四枚)
    const all = [...el.shadowRoot!.querySelectorAll<HTMLElement>(".hip-tag")];
    expect(all.map((c) => c.dataset.layer).sort()).toEqual(["ghost", "punch", "shell", "wire"]);
    expect(all.every((c) => c.classList.contains("on"))).toBe(true);
    // 画面:全新场景无任何显隐调用 = 全默认可见,与 chips 一致
    expect(rebuilt.layers).toEqual([]);
    expect(rebuilt.shellOn).toBe(true);
    expect(rebuilt.wireOn).toBe(true);
  });
});
