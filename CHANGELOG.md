# Changelog

版本变更摘要，面向用户与开发者；版本号由 git tag（v*）驱动，未打 tag 的变更归入「未发布」。逐行代码变更见 commit 历史。

## 未发布

- 新增 API Key 鉴权：全部端点要求 `X-API-Key` 请求头，fail-closed——未配置 key 时除豁免外一律 401，忘配置不会裸奔；`auth.api_keys` 多 key 并存支持无痕轮换。豁免面：`/health`、根跳转、CORS 预检 OPTIONS 与 `/panel` 静态壳（面板 JS 首次 401 弹窗输入 key 一次，存 sessionStorage）；`/docs` 浏览器直开仍 401。
- 建立 CHANGELOG 机制（本文件）。当前为 v1.0.0 之后、三端联调前的基线，无功能性变更。
