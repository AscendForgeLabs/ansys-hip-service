# Changelog

版本变更摘要，面向用户与开发者；版本号由 git tag（v*）驱动，未打 tag 的变更归入「未发布」。逐行代码变更见 commit 历史。

## v2.0.0 - 2026-10-05

### 新增

- API Key 鉴权：全部端点要求 `X-API-Key` 请求头，fail-closed——未配置 key 时除豁免外一律 401，忘配置不会裸奔；`auth.api_keys` 多 key 并存支持无痕轮换。豁免面：`/health`、根跳转、CORS 预检 OPTIONS 与 `/panel` 静态壳（面板 JS 首次 401 弹窗输入 key 一次，存 sessionStorage，失效后点"立即刷新"重输）；`/docs` 浏览器直开仍 401。**不兼容变更**：存量未带 key 的调用方（含编排服务）需升级至同日发布的 simulation-service v0.2.0 并配置 `SIMULATION_ANSYS_API_KEY`。
- 建立 CHANGELOG 机制（本文件）。当前为 v1.0.0 之后、三端联调前的基线，无功能性变更。

### 修复

- 面板工件下载改走鉴权 fetch（裸 `<a href>` 导航无法携带自定义头，鉴权下下载全 401）；401 弹窗抑制闩与失效密钥清理（取消/输错不再连环弹窗，key 轮换后可重输）；`/panel` 豁免改按段边界匹配；X-API-Key 仅发同源 URL。
- 配置健壮性：环境变量空串视为未设置（防 `HIP_SERVICE_API_KEYS=` 残留空赋值静默清空 yaml 已配置的 key）；`auth.api_keys` 拒绝非字符串元素（YAML 裸 `0123`/`true` 会被静默强转致比对永远不中）。
- 文档：README / CLAUDE.md / OpenAPI description / passthrough-guide §7.1 豁免面口径统一；guide 主流程 7 处 curl 与示范脚本补 `X-API-Key`。
