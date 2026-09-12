/** <hip-playback> Web Component — 生命周期/属性/双通道取数/HUD 编排。

 * 即插即用:<script src="hip-playback.js"> + 标签即得完整交互回放。
 * 属性:base-url + job-id(自动拉取)、t(初始时间)、scale(初始倍率)、
 *       data-debug(顶点级对账 dump,排查/验收用)。
 * 方法:loadData(File[] | Record<文件名, 文本>)、play()、pause()、resetView()。
 * 事件:error(ErrorEvent,拉取/解析/构网失败;界面同时给中文错误态,不静默)。
 *
 * 生命周期要点(评审修复):
 *   - shadowRoot 复用(`shadowRoot ?? attachShadow`):断连重连不二次 attachShadow;
 *   - disconnect 只停循环/观察器/场景,**HUD DOM 保留**,重连轻量恢复;
 *   - 装载数据建场景后立即 resize(RO 首帧通知先于场景存在,jobId 通道必踩);
 *   - 显隐 chip 用 tags 容器事件委托(chip 在 configureHud 才创建,逐 chip 挂会漏);
 *   - 渲染走脏标记(空闲不整帧渲染,长驻面板省 GPU)。
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
  private needsRender = true;                    // 脏标记:有更新才整帧渲染

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
    const root = this.shadowRoot ?? this.attachShadow({ mode: "open" });
    if (this.hud) {                              // 重连:HUD 已在,轻量恢复
      this.restartObservers();
      this.startLoop();
      if (this.mesh) this.rebuildScene();        // 旧场景已 dispose,按既有网格重建
      return;
    }
    const style = document.createElement("style");
    style.textContent = STYLES;
    root.append(style);
    this.hud = buildHud(this);
    this.wireEvents();
    this.restartObservers();
    this.startLoop();
    if (this.baseUrl && this.jobId) void this.autoLoad();
    else showEmpty(this.hud);
  }

  disconnectedCallback(): void {
    cancelAnimationFrame(this.rafId);
    this.resizeObserver?.disconnect();
    this.scene?.dispose();
    this.scene = null;
    this.loadSeq++;          // 在途装载回调全部失效(旧数据不得回写已卸载的元素)
    // HUD DOM 保留在 shadow root,重连免重建
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

  resetView(): void { this.scene?.resetView(); this.needsRender = true; }

  // ---- 内部 ----

  private restartObservers(): void {
    this.resizeObserver?.disconnect();
    this.resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) {
        this.scene?.resize(rect.width, rect.height);
        this.needsRender = true;
      }
    });
    this.resizeObserver.observe(this);
  }

  /** 断连重连后按既有网格重建场景(canvas WebGL 上下文随 dispose 释放)。 */
  private rebuildScene(): void {
    if (!this.hud) return;
    try {
      this.scene = new PlaybackScene(this.hud.canvas);
      this.scene.setMesh(this.mesh!);
      this.scene.resize(this.clientWidth, this.clientHeight);
      this.refreshFrame();
    } catch {
      this.fail(new Error("当前浏览器环境不支持 WebGL,无法渲染 3D 回放"));
    }
  }

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
    const hud = this.hud;
    if (!hud) return;        // 已断连,静默丢弃迟到数据
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
    // RO 首帧通知先于场景存在(jobId 通道):装载后必须显式定尺寸,
    // 否则画布停在 canvas 默认 300×150 缓冲被 CSS 拉伸(模糊+失真)
    this.scene.resize(this.clientWidth, this.clientHeight);
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
    const hud = this.hud;
    this.pause();
    if (!hud) return;                           // 已断连:事件照发,界面无处可写
    const message = err instanceof Error ? err.message : String(err);
    showError(hud, `${message}(数据契约见 passthrough-guide.md / playback-handbook.md)`);
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
    // 显隐 chip 事件委托:ghost/punch 在 configureHud 才创建(且每次重载会重建),
    // 逐 chip 挂监听必漏 → 在容器上按 data-layer 分发
    hud.tagsRoot.addEventListener("click", (e) => {
      const chip = (e.target as HTMLElement).closest(".hip-tag") as HTMLSpanElement | null;
      const layer = chip?.dataset.layer as "shell" | "wire" | "ghost" | "punch" | undefined;
      if (!chip || !layer) return;
      const on = !chip.classList.contains("on");
      chip.classList.toggle("on", on);
      this.scene?.setLayerVisible(layer, on);
      this.needsRender = true;
    });
  }

  private refreshFrame(): void {
    if (!this.scene || !this.mesh || !this.hud) return;
    const { uMax, depth } = this.scene.update(this.tNow, this.scale);
    const k = Math.min(Math.round(this.tNow), this.mesh.stages.label.length - 1);
    this.hud.pv.textContent = depth.toFixed(2);
    this.hud.seg.textContent =
      `${this.mesh.stages.label[k]} · t=${this.mesh.stages.time[k]} s`;
    this.hud.umax.textContent = `max|u| = ${uMax.toFixed(3)} mm`;
    this.needsRender = true;
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
      // 脏标记渲染:播放中每帧,空闲时仅在有更新/相机拖转(scene 内部置脏)时渲染
      if (this.scene && (this.playing || this.needsRender || this.scene.consumeDirty())) {
        this.scene.render();
        this.needsRender = false;
      }
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
