/** Shadow DOM 内样式 — 参考实现(cube.template.html)同款暗色 HUD 观感。

 * 全部选择器都作用于 :host 内部,不污染宿主页面;宿主只暴露
 * --hip-accent 一个自定义属性(可选题色,默认 viridis 绿)。
 */

export const STYLES = `
:host {
  display: block;
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  background: #0e1116;
  color: #cfd8e3;
  font: 13px/1.5 "Noto Sans CJK SC", "WenQuanYi Zen Hei", system-ui, sans-serif;
  overflow: hidden;
  --hip-accent: #5ec962;
  --hip-panel-bg: rgba(14, 17, 22, .82);
  --hip-panel-border: #2a3340;
}
:host([hidden]) { display: none; }
canvas.hip-view { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }

.hip-hud {
  position: absolute;
  background: var(--hip-panel-bg);
  border: 1px solid var(--hip-panel-border);
  border-radius: 8px;
  padding: 10px 14px;
  backdrop-filter: blur(4px);
}
.hip-panel { top: 12px; left: 12px; width: 256px; }
.hip-panel h1 { font-size: 14px; margin: 0 0 2px; color: #e8eef5; font-weight: 600; }
.hip-panel .sub { font-size: 11px; color: #7d8a99; margin-bottom: 8px; }
.hip-row { display: flex; align-items: center; gap: 8px; margin: 7px 0; }
.hip-row label { flex: 0 0 62px; font-size: 11px; color: #9aa7b5; }
input[type="range"] { flex: 1; accent-color: var(--hip-accent); }
button {
  background: #1b2330; color: #cfd8e3; border: 1px solid #2f3a49;
  border-radius: 5px; padding: 3px 12px; cursor: pointer; font-size: 12px;
  font-family: inherit;
}
button:hover { background: #26303f; }

.hip-stage { top: 12px; right: 12px; text-align: right; }
.hip-stage .lbl { font-size: 11px; color: #7d8a99; }
.hip-stage .big { font-size: 22px; font-weight: 600; color: #fde725; }
.hip-stage .seg { font-size: 13px; color: var(--hip-accent); margin-top: 2px; }

.hip-bar { bottom: 12px; left: 12px; right: 12px; display: flex; gap: 14px; align-items: center; }
.hip-bar .note { font-size: 10px; color: #5d6a78; max-width: 44%; text-align: right; margin-left: auto; }

.hip-cbar { width: 120px; height: 10px; border-radius: 5px;
  background: linear-gradient(90deg, #440154, #3b528b, #21918c, #5ec962, #fde725); }
.hip-cbar-wrap { display: flex; flex-direction: column; font-size: 9px; color: #7d8a99; }

.hip-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.hip-tag {
  font-size: 10px; padding: 1px 7px; border-radius: 9px; background: #1b2330;
  border: 1px solid #2f3a49; color: #9aa7b5; cursor: pointer; user-select: none;
}
.hip-tag.on { color: #fde725; border-color: var(--hip-accent); }

/* 空态 / 错误态(数据加载前、加载失败时整幅居中) */
.hip-state {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px; text-align: center;
  padding: 0 10%; font-size: 13px; color: #9aa7b5;
}
/* hidden 属性必须压过上面的 display:flex,否则空态浮层盖在已渲染画面上 */
.hip-state[hidden] { display: none; }
.hip-state.hip-error { color: #e08c8c; }
.hip-state h2 { font-size: 15px; margin: 0; color: #e8eef5; font-weight: 600; }
.hip-state .hint { font-size: 11px; color: #5d6a78; line-height: 1.7; }

.hip-debug {
  position: absolute; inset: 0; overflow: auto; margin: 0; padding: 12px;
  font: 11px/1.5 monospace; color: #cfd8e3; background: #0e1116; white-space: pre-wrap;
}
`;
