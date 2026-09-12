# hip-playback — 前端即插即用 3D 回放组件

ansys-hip passthrough 帧工件(`frame_N.csv` 节点云)→ 浏览器内构网 + 渲染的
完整交互式三维回放,打包为**一个自包含 js 文件**(内嵌 three.js r128 与全部
样式,零构建、零框架绑定、内网可用)。渲染配方与数据契约见仓库
`docs/playback-handbook.md`;本库是参考实现
(`docs/examples/passthrough-demo/playback/`,Python 离线生成页面)的组件化移植。

## 前端接入(全部代码)

```html
<script src="/static/hip-playback.js"></script>

<hip-playback base-url="http://hip内网地址:8010" job-id="20260912-xxxx"></hip-playback>
```

自带:时间轴 / 播放 / 变形倍率 / 回放速度 / viridis |u| 云图 / 阶段与压深 HUD /
网格线、参考轮廓、压头显隐 / 拖转缩放。任何框架(纯 HTML、Vue、React、JSP)同用。

> 跨域拉取需在服务端开启 CORS(`server.cors_origins`,只放行 GET;
> 见 `config/service.yaml` 与 `docs/passthrough-guide.md`)。

## 属性 / 方法 / 事件

| 接口 | 说明 |
|---|---|
| `base-url` + `job-id` 属性 | 自动 `GET /jobs/{id}/artifacts` 清单 → 拉取帧/progress/results/emap |
| `t` / `scale` 属性 | 初始时间(0..N)与变形倍率(默认 5) |
| `loadData(File[] 或 {文件名:文本})` | 本地通道:目录选择/拖入/已读好的 CSV,离线可用 |
| `play()` / `pause()` / `resetView()` | 程序控制 |
| `t` / `scale` 属性(可写) | 跳转时刻、改倍率 |
| `error` 事件 | 拉取/解析/构网失败(ErrorEvent;界面同步显示中文错误态) |
| `data-debug` 属性 | 顶点级对账 JSON dump(验收/排障;headless 用) |

## 构网(CSV 建模)双路径

帧 CSV 是节点云,连接表按 `playback-handbook.md` §3 两条路自动选择:

1. 工件含 **`emap.csv`**(.inp 末尾 6 行 APDL 自导,手册有现成片段)→
   通用六面体构网:边界面提取 + 外法线定向,二次单元(hex20)按
   -0.25×Σ角点 + 0.5×Σ棱中点虚拟面心保真实曲面观感;
2. 无 emap → **规则 hex20 格反推**(参考实现原路径):每轴唯一坐标均匀、
   角点+棱中点形态自动检测,适合 BLOCK 类结构网格(如 `cube_dent.inp`);
3. 都不成 → 明确报错并指向手册 §3(不静默降级)。

## 开发

```bash
cd packages/hip-playback
npm install          # 首次
npm test             # vitest 单测(CSV/lattice/emap/loader/模型/色带)
npm run build        # → dist/hip-playback.js(单文件,提交入库供前端取用)
npm run demo         # 起本地静态服务打开 demo 页(需先 build)
```

- fixture:合成 2×2×2 hex20 网格 + 真机 cube-dent 切片
  (`tests/fixtures/`,由 `tests/fixtures/gen/make_fixtures.py` 再生成,
  golden 基准由真 `build_viewer.py` 产出对拍);
- demo 页支持 jobId 拉取与本地目录两通道,URL 锚点 `#base=…&job=…&debug` 可直达;
- three.js 钉 `0.128.0`(与参考实现验收图同版,保证渲染逐参数一致)。
