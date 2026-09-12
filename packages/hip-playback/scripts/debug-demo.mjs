// 一次性诊断:demo 页内直接 fetch 示例 CSV + 手动 loadData,看哪一步断。
import { chromium } from "playwright";

const URL = process.argv[2] ?? "http://127.0.0.1:8090/demo/";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.on("console", (m) => console.log("[page]", m.type(), m.text().slice(0, 200)));
page.on("requestfailed", (r) => console.log("[reqfail]", r.url(), r.failure()?.errorText));
page.on("response", (r) => {
  if (r.url().includes("cube-dent") || r.url().endsWith(".js")) {
    console.log("[resp]", r.status(), r.url().slice(-60));
  }
});
await page.goto(URL, { waitUntil: "domcontentloaded" });
const result = await page.evaluate(async () => {
  const out = {};
  try {
    const r = await fetch("./data/cube-dent/frame_1.csv");
    out.fetchStatus = r.status;
    out.bodyHead = (await r.text()).slice(0, 60);
  } catch (err) {
    out.fetchError = String(err);
  }
  const el = document.querySelector("hip-playback");
  out.hasLoadData = typeof el?.loadData === "function";
  try {
    const texts = {};
    for (let i = 1; i <= 6; i++) {
      const r = await fetch(`./data/cube-dent/frame_${i}.csv`);
      texts[`frame_${i}.csv`] = await r.text();
    }
    await el.loadData(texts);
    const root = el.shadowRoot;
    out.afterSub = root?.querySelector(".sub")?.textContent ?? null;
    out.timelineMax = root?.querySelector('input[type="range"]')?.max ?? null;
    out.stateHidden = root?.querySelector(".hip-state")?.hidden ?? null;
    out.pv = root?.querySelector(".big span")?.textContent ?? null;
  } catch (err) {
    out.loadDataError = String(err).slice(0, 300);
  }
  return out;
});
console.log(JSON.stringify(result, null, 1));
await browser.close();
