// demo 无头验证(playwright chromium):console 错误收集 + shadow DOM 状态 + 截图。
// 用法:node scripts/verify-demo.mjs [URL](默认 http://127.0.0.1:8090/demo/)
import { chromium } from "playwright";

const URL = process.argv[2] ?? "http://127.0.0.1:8090/demo/";
const OUT = process.argv[3] ?? "/tmp/hip-pw-demo.png";

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = [];
page.on("console", (msg) => {
  if (msg.type() === "error") errors.push(msg.text());
});
page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
page.on("response", (r) => {
  if (r.url().includes("cube-dent")) console.log("[resp]", r.status(), r.url().slice(-30));
});

await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
// 固定等待自动加载完成(fetch 3MB + 解析构网 + 软件 WebGL 初始化)
await page.waitForTimeout(12000);

const state = await page.evaluate(() => {
  const el = document.querySelector("hip-playback");
  const root = el?.shadowRoot;
  return {
    defined: !!customElements.get("hip-playback"),
    sub: root?.querySelector(".sub")?.textContent ?? null,
    timelineMax: root?.querySelector('input[type="range"]')?.max ?? null,
    note: root?.querySelector(".note")?.textContent ?? null,
    canvasCount: root?.querySelectorAll("canvas").length ?? 0,
    hudStage: root?.querySelector(".seg")?.textContent ?? null,
    stateHidden: root?.querySelector(".hip-state")?.hidden ?? null,
    textTail: (root?.textContent ?? "").replace(/\s+/g, " ").slice(-160),
  };
});

await page.screenshot({ path: OUT });
console.log(JSON.stringify(state, null, 1));
console.log("console errors:", errors.length ? errors.slice(0, 5) : "无");
console.log("screenshot:", OUT);
await browser.close();
