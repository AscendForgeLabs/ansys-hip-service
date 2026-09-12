/** HUD DOM 构造 — Shadow Root 内的全部界面元素(参考实现同布局)。

 * 只负责建 DOM 与文案,不挂事件、不做渲染;事件与更新在 element.ts 编排。
 * 文案全部中文;通用(emap)模式下没有参考轮廓/压头(cube 型语义)。
 */

export interface HudHandles {
  root: ShadowRoot;
  canvas: HTMLCanvasElement;
  state: HTMLDivElement;                  // 空态/错误态覆盖层
  panel: HTMLDivElement;
  title: HTMLHeadingElement;
  sub: HTMLDivElement;
  timeline: HTMLInputElement;
  scale: HTMLInputElement;
  scalev: HTMLSpanElement;
  speed: HTMLInputElement;
  speedv: HTMLSpanElement;
  play: HTMLButtonElement;
  reset: HTMLButtonElement;
  tagsRoot: HTMLDivElement;               // 显隐 chip 容器(事件委托挂这里)
  tags: { shell: HTMLSpanElement; wire: HTMLSpanElement;
          ghost: HTMLSpanElement | null; punch: HTMLSpanElement | null };
  partChips: HTMLSpanElement[];           // 部件 chip(tet + epart;configureHud 每轮重建)
  stage: HTMLDivElement;
  pv: HTMLSpanElement;                    // 压深数值
  seg: HTMLDivElement;                    // 阶段标签 · t=秒
  umax: HTMLDivElement;                   // max|u|
  cmax: HTMLSpanElement;                  // 色带右端标注
  note: HTMLDivElement;                   // 底部算例说明
}

const el = <K extends keyof HTMLElementTagNameMap>(
  tag: K, attrs: Record<string, string> = {},
  ...children: Array<Node | string>
): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
};

/** 部件 chip 的 data-layer 前缀(生成端 configureHud 与消费端 element 委托共用)。 */
export const PART_LAYER_PREFIX = "part:";

/** 把 tagsRoot 下全部显隐 chip 恢复默认 on(换装/重连 = 显隐全默认;画面同态)。
 * configureHud(重载)与 element.rebuildScene(重连)两处复用。 */
export function resetChipStates(tagsRoot: HTMLElement): void {
  for (const chip of tagsRoot.querySelectorAll<HTMLElement>(".hip-tag")) {
    chip.classList.add("on");
  }
}

function tagChip(label: string, layer: string, on = true): HTMLSpanElement {
  return el("span", { class: `hip-tag${on ? " on" : ""}`, "data-layer": layer }, label);
}

/** 建空态骨架(数据加载完成后再 configure 填 nseg/结果/文案)。 */
export function buildHud(host: HTMLElement): HudHandles {
  const root = host.shadowRoot!;

  const canvas = el("canvas", { class: "hip-view" });
  const title = el("h1", {}, "HIP 三维回放");
  const sub = el("div", { class: "sub" }, "等待数据…");
  const timeline = el("input", { type: "range", min: "0", max: "1", step: "0.002", value: "0" });
  const scale = el("input", { type: "range", min: "1", max: "30", step: "1", value: "5" });
  const scalev = el("span", { style: "font-size:11px;width:34px" }, "×5");
  const speed = el("input", { type: "range", min: "2", max: "20", step: "1", value: "8" });
  const speedv = el("span", { style: "font-size:11px;width:46px" }, "8 s/程");
  const play = el("button", {}, "▶ 播放");
  const reset = el("button", {}, "复位视角");
  const tagShell = tagChip("外壳", "shell");
  const tagWire = tagChip("网格线", "wire");
  const tagsRoot = el("div", { class: "hip-row hip-tags", id: "hip-tags" }, tagShell, tagWire);

  const cbarWrap = el("div", { class: "hip-cbar-wrap" },
    el("div", { class: "hip-cbar" }),
    el("div", { style: "display:flex;justify-content:space-between" },
      el("span", {}, "0"), el("span", {}, "总位移 |u|"), el("span", {}, "")),
  );

  const panel = el("div", { class: "hip-hud hip-panel" },
    title, sub,
    el("div", { class: "hip-row" }, el("label", {}, "压制进程"), timeline),
    el("div", { class: "hip-row" }, el("label", {}, "变形倍率"), scale, scalev),
    el("div", { class: "hip-row" }, el("label", {}, "回放速度"), speed, speedv),
    el("div", { class: "hip-row", style: "gap:6px" }, play, reset),
    tagsRoot,
    el("div", { class: "hip-row", style: "margin-bottom:0" }, cbarWrap),
  );

  const pv = el("span", {}, "0.00");
  const seg = el("div", { class: "seg" }, "MESH · t=0 s");
  const umax = el("div", { class: "lbl" }, "max|u| = 0 mm");
  const stage = el("div", { class: "hip-hud hip-stage" },
    el("div", { class: "lbl" }, "压深"),
    el("div", { class: "big" }, pv, " mm"),
    seg, umax);

  const note = el("div", { class: "note" }, "");
  const bar = el("div", { class: "hip-hud hip-bar" },
    el("span", { style: "font-size:11px;color:#9aa7b5" }, "拖动旋转 · 滚轮缩放"), note);

  const state = el("div", { class: "hip-state" });
  const stateTitle = el("h2", {}, "等待回放数据");
  const stateHint = el("div", { class: "hint" },
    "设置 base-url 与 job-id 属性自动拉取,或调用 loadData(files) 传入本地工件。");
  state.append(stateTitle, stateHint);

  root.append(canvas, panel, stage, bar, state);
  return {
    root, canvas, state, panel, title, sub, timeline, scale, scalev, speed, speedv,
    play, reset, tagsRoot, tags: { shell: tagShell, wire: tagWire, ghost: null, punch: null },
    partChips: [],
    stage, pv, seg, umax, cmax: cbarWrap.lastElementChild!.lastElementChild as HTMLSpanElement,
    note,
  };
}

/** 数据就绪后配置 HUD:nseg/标题/说明/通用或 cube 型标签/部件 chip。 */
export function configureHud(h: HudHandles, opts: {
  nseg: number; mode: "lattice" | "emap";
  nverts: number; title: string; sub: string; note: string;
  parts?: number[];                        // 部件号清单(MeshData.parts;缺席/空 → 无 chip)
}): void {
  h.state.hidden = true;
  h.timeline.max = String(opts.nseg);
  h.title.textContent = opts.title;
  h.sub.textContent = opts.sub;
  h.note.textContent = opts.note;
  // 重载数据时先移除上一轮的动态 chip(cube 型 ghost/punch 与部件 chip;
  // 避免重复累积;监听走容器委托不受影响),再把 shell/wire 静态 chip
  // 补回 on —— 换装 = 显隐全默认,与场景复位后的画面一致
  h.tags.ghost?.remove();
  h.tags.punch?.remove();
  h.tags.ghost = null;
  h.tags.punch = null;
  for (const chip of h.partChips) chip.remove();
  h.partChips = [];
  resetChipStates(h.tagsRoot);
  if (opts.mode === "lattice") {           // cube 型语义:参考轮廓 + 压头
    const ghost = tagChip("参考轮廓", "ghost");
    const punch = tagChip("压头", "punch");
    h.tags.ghost = ghost;
    h.tags.punch = punch;
    h.tags.shell.after(ghost, punch);
  }
  for (const id of opts.parts ?? []) {     // 部件 chip:接 tagsRoot 尾部,委托已覆盖
    const chip = tagChip(`部件 ${id}`, `${PART_LAYER_PREFIX}${id}`);
    h.partChips.push(chip);
    h.tagsRoot.append(chip);
  }
}

/** 错误态:中文消息 + 出路提示(不静默;覆盖整幅)。 */
export function showError(h: HudHandles, message: string): void {
  h.state.className = "hip-state hip-error";
  h.state.hidden = false;
  h.state.innerHTML = "";
  const title = document.createElement("h2");
  title.textContent = "回放数据加载失败";
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = message;
  h.state.append(title, hint);
}

/** 空态(无 base-url/job-id 且未 loadData)。 */
export function showEmpty(h: HudHandles): void {
  h.state.className = "hip-state";
  h.state.hidden = false;
}
