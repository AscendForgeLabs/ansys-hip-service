/** <hip-playback> Web Component — 生命周期/属性/双通道取数/HUD 编排。

 * 即插即用:<script src="hip-playback.js"> + 标签即得完整交互回放。
 * 属性:base-url + job-id(自动拉取)、t(初始时间)、scale(初始倍率)、
 *       data-debug(顶点级对账 dump,排查/验收用)。
 * 方法:loadData(File[] | Record<文件名, 文本>)、play()、pause()、resetView()。
 * 事件:error(ErrorEvent,拉取/解析/构网失败;界面同时给中文错误态,不静默)。
 */

import type { FrameNodes } from "./csv";
import type { LoadedArtifacts } from "./loader";
import { loadFromFiles, loadFromService, loadFromTexts } from "./loader";
import { buildMeshData } from "./mesh";
import type { MeshData } from "./mesh/types";
import { PlaybackScene } from "./scene";
import { STYLES } from "./styles";
import { buildHud, configureHud, showError, showEmpty } from "./ui";
import type { HudHandles } from "./ui";

const ELEMENT_TAG = "hip-playback";

export class HipPlaybackElement extends HTMLElement {
  static get observedAttributes(): string[] {
    return ["base-url", "job-id", "t", "scale"];
  }

  private hud: HudHandles | null = null;
  private scene: PlaybackScene | null = null;    // 懒建:首次成功装载数据时(无 WebGL 环境给错误态)
  private mesh: MeshData | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private rafId = 0;
  private lastTs: number | null = null;
  private playing = false;
  private tNow = 0;
  private loadSeq = 0;                           // 装载序号:旧请求返回时丢弃,防竞态

  // ---- 属性(反映到 attribute,可声明式使用) ----
  get baseUrl(): string { return this.getAttribute("base-url") ?? ""; }
  get jobId(): string { return this.getAttribute("job-id") ?? ""; }
  get t(): number { return this.tNow; }
  set t(value: number) {
    this.tNow = this.mesh ? Math.max(0, Math.min(value, this.mesh.meta.nseg)) : value;
    this.setAttribute("t", String(this.tNow));
  }
  get scale(): number { return Number(this.hud?.scale.value ?? 5); }
  set scale(value: number) {
    if (!this.hud) { this.setAttribute("scale", String(value)); return; }
    this.hud.scale.value = String(value);
    this.hud.scalev.textContent = `×${value}`;
    this.refreshFrame();
  }

  connectedCallback(): void {
    if (this.hud) return;                        // 幂等(重复 connect 不重建)
    const root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLES;
    root.append(style);
    this.hud = buildHud(this);
    this.wireEvents();
    this.resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) this.scene?.resize(rect.width, rect.height);
    });
    this.resizeObserver.observe(this);
    this.startLoop();
    if (this.baseUrl && this.jobId) void this.autoLoad();
    else showEmpty(this.hud);
  }

  disconnectedCallback(): void {
    cancelAnimationFrame(this.rafId);
    this.resizeObserver?.disconnect();
    this.scene?.dispose();
    this.scene = null;
    this.hud = null;
    this.loadSeq++;          // 在途装载回调全部失效(重挂载后旧数据不得回写已卸载的 shadow root)
  }

  attributeChangedCallback(name: string, _old: string, _new: string): void {
    if (!this.hud) return;
    if (name === "base-url" || name === "job-id") {
      if (this.baseUrl && this.jobId) void this.autoLoad();
      return;
    }
    if (name === "t") { this.tNow = Math.max(0, Number(_new) || 0); this.refreshFrame(); }
    if (name === "scale") { this.scale = Number(_new) || 5; }
  }

  // ---- 公开 API ----

  /** 本地数据通道:File[](目录选择/拖入)或 {文件名: 文本}(已读好的 CSV)。 */
  async loadData(source: File[] | Record<string, string>): Promise<void> {
    const seq = ++this.loadSeq;
    try {
      const artifacts = Array.isArray(source)
        ? await loadFromFiles(source)
        : loadFromTexts(source);
      if (seq !== this.loadSeq) return;
      this.applyArtifacts(artifacts);
    } catch (err) {
      if (seq === this.loadSeq) this.fail(err);
      throw err;                                 // 调用方亦可通过 Promise 感知
    }
  }

  play(): void {
    if (!this.mesh) return;
    this.playing = true;
    this.hud!.play.textContent = "⏸ 暂停";
  }

  pause(): void {
    this.playing = false;
    if (this.hud) this.hud.play.textContent = "▶ 播放";
  }

  resetView(): void { this.scene?.resetView(); }

  // ---- 内部 ----

  private async autoLoad(): Promise<void> {
    const seq = ++this.loadSeq;
    try {
      const artifacts = await loadFromService(this.baseUrl, this.jobId);
      if (seq !== this.loadSeq) return;
      this.applyArtifacts(artifacts);
    } catch (err) {
      if (seq === this.loadSeq) this.fail(err);
    }
  }

  private applyArtifacts(art: LoadedArtifacts): void {
    if (!this.hud) return;   // 已断连(disconnectedCallback 置空),静默丢弃迟到数据
    const hud = this.hud;
    try {
      this.mesh = buildMeshData({
        frames: art.frames as FrameNodes[],
        emap: art.emap,
        stageLabels: art.stageLabels,
        stageTimes: art.stageTimes,
        results: art.results,
      });
    } catch (err) {
      this.fail(err);
      return;
    }
    if (!this.scene) {
      try {
        this.scene = new PlaybackScene(hud.canvas);
      } catch {
        this.fail(new Error("当前浏览器环境不支持 WebGL,无法渲染 3D 回放"));
        return;
      }
    }
    this.scene.setMesh(this.mesh);
    const resultsNote = this.mesh.meta.results["dent_dep"] !== undefined
      ? ` · 压深 ${this.mesh.meta.results["dent_dep"]} mm`
      : "";
    configureHud(hud, {
      nseg: this.mesh.meta.nseg,
      mode: this.mesh.mode,
      nverts: this.mesh.vertBase.length / 3,
      title: "HIP 三维回放",
      sub: `${this.mesh.mode === "lattice" ? "规则格反推" : "emap 通用构网"} · `
         + `${this.mesh.meta.nseg} 帧 · ${this.mesh.vertBase.length / 3} 顶点`,
      note: `数据:passthrough 工件 frame_1..${this.mesh.meta.nseg}.csv`
          + `${this.mesh.mode === "emap" ? " + emap.csv" : ""}${resultsNote}`
          + ` · 变形倍率仅为可视化放大。`,
    });
    hud.cmax.textContent = `${this.mesh.meta.colorMax.toFixed(2)} mm`;
    const tAttr = Number(this.getAttribute("t") ?? "0") || 0;
    this.tNow = Math.min(tAttr, this.mesh.meta.nseg);
    hud.timeline.value = String(this.tNow);
    hud.timeline.dispatchEvent(new Event("input"));   // 走统一刷新(含 HUD/时间轴)
    this.maybeRenderDebugDump();
  }

  private fail(err: unknown): void {
    this.pause();
    const message = err instanceof Error ? err.message : String(err);
    showError(this.hud!, `${message}(数据契约见 passthrough-guide.md / playback-handbook.md)`);
    console.error("[hip-playback]", err);
    this.dispatchEvent(new ErrorEvent("error", {
      bubbles: false, composed: true,
      error: err instanceof Error ? err : new Error(message),
    }));
  }

  private wireEvents(): void {
    const hud = this.hud!;
    hud.timeline.addEventListener("input", () => {
      this.tNow = Number(hud.timeline.value);
      this.refreshFrame();
    });
    hud.scale.addEventListener("input", () => {
      hud.scalev.textContent = `×${hud.scale.value}`;
      this.refreshFrame();
    });
    hud.speed.addEventListener("input", () => {
      hud.speedv.textContent = `${hud.speed.value} s/程`;
    });
    hud.play.addEventListener("click", () => {
      this.playing = !this.playing;
      hud.play.textContent = this.playing ? "⏸ 暂停" : "▶ 播放";
    });
    hud.reset.addEventListener("click", () => this.resetView());
    hud.tags.shell.addEventListener("click", (e) => {
      const chip = e.currentTarget as HTMLElement;
      const on = !chip.classList.contains("on");
      chip.classList.toggle("on", on);
      this.scene?.setLayerVisible("shell", on);
    });
    hud.tags.wire.addEventListener("click", (e) => {
      const chip = e.currentTarget as HTMLElement;
      const on = !chip.classList.contains("on");
      chip.classList.toggle("on", on);
      this.scene?.setLayerVisible("wire", on);
    });
    for (const chip of [hud.tags.ghost, hud.tags.punch]) {
      chip?.addEventListener("click", () => {
        const on = !chip.classList.contains("on");
        chip.classList.toggle("on", on);
        const layer = chip === hud.tags.ghost ? "ghost" : "punch";
        this.scene?.setLayerVisible(layer as "ghost" | "punch", on);
      });
    }
  }

  private refreshFrame(): void {
    if (!this.scene || !this.mesh) return;
    const hud = this.hud!;
    const { uMax, depth } = this.scene.update(this.tNow, this.scale);
    hud.pv.textContent = depth.toFixed(2);
    const k = Math.min(Math.round(this.tNow), this.mesh.stages.label.length - 1);
    hud.seg.textContent =
      `${this.mesh.stages.label[k]} · t=${this.mesh.stages.time[k]} s`;
    hud.umax.textContent = `max|u| = ${uMax.toFixed(3)} mm`;
  }

  private startLoop(): void {
    const loop = (ts: number): void => {
      const hud = this.hud;
      if (hud && this.mesh && this.playing) {
        const speedSecs = Number(hud.speed.value) || 8;    // 一个全程 N 秒
        if (this.lastTs !== null) {
          this.tNow += this.mesh.meta.nseg / speedSecs * (ts - this.lastTs) / 1000;
          if (this.tNow > this.mesh.meta.nseg) this.tNow = 0;
          hud.timeline.value = String(this.tNow);
          this.refreshFrame();
        }
      }
      this.lastTs = ts;
      this.scene?.render();
      this.rafId = requestAnimationFrame(loop);
    };
    this.rafId = requestAnimationFrame(loop);
  }

  /** data-debug:渲染后 dump 顶点级对账 JSON(参考实现 #debug 同构;验收/排障)。
   * 结果同时回显到 data-debug-result(light DOM 属性,headless dump-dom 可见)。 */
  private maybeRenderDebugDump(): void {
    if (!this.hasAttribute("data-debug") || !this.scene || !this.mesh) return;
    requestAnimationFrame(() => {
      if (!this.scene || !this.hud) return;
      this.pause();
      const tDump = this.tNow || this.mesh!.meta.nseg;
      const dump = JSON.stringify(this.scene.debugInfo(tDump, this.scale));
      const pre = document.createElement("pre");
      pre.className = "hip-debug";
      pre.textContent = dump;
      this.hud.root.append(pre);
      this.setAttribute("data-debug", "done");
      this.setAttribute("data-debug-result", dump);
    });
  }
}

/** 注册自定义元素(重复调用幂等;单文件引入即生效)。 */
export function defineHipPlayback(tag = ELEMENT_TAG): void {
  if (!customElements.get(tag)) customElements.define(tag, HipPlaybackElement);
}
