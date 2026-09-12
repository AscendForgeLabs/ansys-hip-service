// 本地 demo 静态服务(零依赖,node:http):serve 包根 → demo/ 与 dist/ 同源可达。
// 用法:npm run demo [-- 端口 [--host]](默认 8090 / 0.0.0.0——VM 场景宿主机浏览器
// 需经 VM IP 访问,绑 127.0.0.1 则只有本机可见;内网 demo 用途,勿暴露公网)。
// 先 npm run build 产出 dist。
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));   // packages/hip-playback
const PORT = Number(process.argv[2]) || 8090;
const HOST = process.argv[3] || "0.0.0.0";
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".csv": "text/csv; charset=utf-8",
};

createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? "/", "http://localhost");
    let path = url.pathname;
    if (path.endsWith("/")) path += "index.html";           // 目录 → index.html
    const file = normalize(join(ROOT, path));               // 防穿越:必须仍在包根下
    if (!file.startsWith(ROOT)) throw new Error("forbidden");
    const body = await readFile(file);
    res.writeHead(200, { "content-type": MIME[extname(file)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("404(先 npm run build 产出 dist/hip-playback.js)");
  }
}).listen(PORT, HOST, () => {
  console.log(`demo:http://${HOST}:${PORT}/demo/  (本地目录通道直接用;jobId 通道先起 hip 服务并配 CORS)`);
});
