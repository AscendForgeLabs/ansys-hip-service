# 三维回放数据消费手册(面向上游前端)

> passthrough 通道产出的帧工件如何变成"逐步变形"的 3D 回放。本手册技术栈中立:
> 数据语义与渲染配方与框架无关,生态选项按栈列出,选型归上游。
> 传输契约本体见 `passthrough-guide.md`;参考实现见
> `examples/passthrough-demo/playback/`(cube 型,three.js,已真机验收)。

## §0 你会拿到什么

提交作业(见 guide §2/§3)后,渲染所需的全部数据从这几个端点拿:

| 数据 | 端点 | 渲染用途 | 必需性 |
|---|---|---|---|
| **帧文件 frame_1..N.csv** | `GET /jobs/{id}/artifacts/frame_N.csv` | 回放本体(逐帧节点坐标+位移) | **必需** |
| progress.csv | `GET /jobs/{id}/artifacts/progress.csv` | 时间轴刻度(阶段标签+累计秒) | 可选(HUD) |
| result.values | `GET /jobs/{id}/result` | 标量标注(如压深/极值) | 可选(HUD) |
| deform.csv | `GET /jobs/{id}/artifacts/deform.csv` | 末态九列一步到位(重构用) | 可选 |
| 工件名清单 | `GET /jobs/{id}/artifacts` | 发现帧文件(帧数 N 未知时) | 方便 |

帧文件在 `.inp` 的 `declared_outputs` 里逐个声明(缺一即作业 failed
`ARTIFACT_NOT_FOUND`);`progress.csv` / `results.csv` 由服务自动处理,勿声明。
帧数 N 由 .inp 段数决定 —— 上游既写 .inp 就知道 N,不知道时用工件清单发现。

## §1 帧数据精确语义

每个 `frame_N.csv` = 一个载荷段末的全节点快照,UTF-8 文本 CSV:

```csv
node,x_mm,y_mm,z_mm,ux_mm,uy_mm,uz_mm
  0.10000000E+01,  0.20000000E+01, ...,  0.00000000E+00, ...
```

(数值列为 `E16.8` 科学计数,带前导空格,解析按 float 处理即可;首行表头。)

三条不变量,渲染侧可放心依赖:

1. **坐标列 = 未变形初始构型**(`*VGET,...,LOC` 取的是原始坐标),各帧恒同 ——
   首帧坐标即基准网格,不必逐帧重复解析;
2. **位移列 = 该载荷段末值**(`*VGET,...,U`),帧间线性插值即近似连续过程
   (段内子步为斜坡加载,KBC,0);
3. **节点号各帧一致且全模型覆盖**(MAPDL NUMCMP 紧编号,1..NNOD 连续)。

量级参考:SOLID186、20 mm 块、esize 2 mm → 4961 节点 ≈ 0.5 MB/帧(见
`examples/passthrough-demo/cube_dent.inp` 实测,真机 v252 E2E 14 项断言)。

**注意:帧里没有单元连接表** —— 它是节点云,不是网格文件。三条应对路线见 §3。

## §2 渲染配方(框架无关,六步)

1. **解析**:读全部帧 → 节点表(首帧坐标)+ 每帧位移数组;
2. **基准构型**:以首帧坐标建渲染网格(连接来源见 §3);
3. **逐帧插值**:时间参数 t∈[0,N],t=0 为零位移(装料态),t∈(i,i+1] 在
   帧 i 与 i+1 间线性插值;**变形放大**:`pos = 基准 + scale × u`
   (scale 独立可调 —— 真实凹陷常为边长的百分之几,×5~×10 才肉眼可辨);
4. **着色**:`|u| / max|u|`(全帧全局最大)映射色标(viridis 等),
   位移场一眼可读;也可按单分量(如 |uy|)着色;
5. **面片更新**:每帧只更新 position/color 顶点缓冲(共享 BufferAttribute,
   一次 needsUpdate),索引(连接)静态不动 —— 上万节点也流畅;
6. **HUD**:阶段标签/累计秒(progress.csv)+ 标量(result.values)随 t 联动。

参考实现把以上六步全部落地(three.js,零构建),见 §5。

## §3 连接表:三档策略

| 档 | 适用 | 做法 | 成本 |
|---|---|---|---|
| ① 规则网格反推 | BLOCK 类结构网格(坐标轴对齐、均匀步长) | 按坐标平面切面 → 角点格+棱中点+虚拟面心扇形三角化(参考实现即此,含 hex20 自动检测) | 零数据成本,仅限规则域 |
| **② 自导 emap.csv(推荐)** | 任意网格 | .inp 末尾加 6 行 APDL 把单元→节点连接写进 declared_outputs,前端零推导 | 每作业 +一份小文件 |
| ③ 点云渲染/重建 | 不改 .inp 的兜底 | 直接渲染节点点集按 \|u\| 着色(无面但过程可读);或前端 Delaunay/泊松重建 | 最低,观感降级 |

档② APDL 片段(SOLID186 角点序 1..8;线性四面体同理取全部节点。
取单元节点号的**内联函数是 `NELEM(E,位置)`** —— 真机实测 `NM()` 在 *VWRITE
表达式中报 "No dimensions set for parameter= NM" 不可用):

```apdl
! ---- emap.csv:单元→角点连接(列入 declared_outputs) ----
*GET,NEL,ELEM,,COUNT
*CFOPEN,emap,csv
*VWRITE,'elem','n1','n2','n3','n4','n5','n6','n7','n8'
(A5,',',A2,',',A2,',',A2,',',A2,',',A2,',',A2,',',A2,',',A2)
*DO,E,1,NEL
  *VWRITE,E,NELEM(E,1),NELEM(E,2),NELEM(E,3),NELEM(E,4),NELEM(E,5),NELEM(E,6),NELEM(E,7),NELEM(E,8)
  (E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8)
*ENDDO
*CFCLOSE
```

(标签 ≤8 字符自查:`'elem'` `'n1'`..`'n8'` ✓。**列数上限实测 ≈10 列**:*VWRITE
格式行超长会被截断,21 列 hex20 全节点宽表真机实测丢列/截标签,故角点 8 列
为验证口径;二次单元棱中点(n9..n20)如需导出应改长格式(每行 elem,位置,
节点),或省略 —— 前端对线性连接表自动用 2 三角/面渲染,观感稍平但正确。)

## §4 各技术栈生态选项

选型归上游;下表按栈给入口与适配点(2026-09 调研核对过版本与维护状态)。
**调研结论:没有任何活跃小库直接做"FEM 位移帧 CSV → 3D 动画"** —— §5 参考实现
(three.js 模板生成)即当前最短路径;现成库各取所长如下。

### 前端/JS

| 选项 | 入口 | 适配点 | 备注 |
|---|---|---|---|
| **three.js**(参考实现) | BufferGeometry + 共享 position/color attribute | §2 六步直接映射;`#t=&scale=` 锚点可做分享链接 | 零构建可 inline;内网自包含分发友好 |
| **k3d** 2.18(2025-07) | `obj.positions={t: 数组}` 时序字典 + `start_auto_play()` | **唯一自带前端时间轴控件的现成库**;`get_snapshot()` 可导独立 HTML 且面板仍可用 | 定制受限(配色/交互),attribute 着色时序需实测;适合作对照实现 |
| Babylon.js / 原生 WebGL | 等价能力 | 同 §2 | 团队既有栈优先 |
| plotly scatter3d+frames | `frame` 列表 + slider | 自包含 HTML 可行 | ≤2~5 万点可用,10 万节点卡顿 |
| pythreejs | — | — | **休眠**(2022 后无 release),不作基础 |

### Python(离线生成/桌面/notebook)

| 选项 | 入口 | 适配点 | 备注 |
|---|---|---|---|
| **PyVista** 0.49(2026-09,活跃) | `UnstructuredGrid`(节点+emap 连接)+ `warp_by_vector` 逐帧;离线视频管线 `open_movie`+`write_frame` | 帧→`point_data['u']` | **出视频/出图的标准答案**;但 `export_html` 已弃用改走 trame,trame 导出的 HTML 脚本走 CDN 且点云导出有已知 bug —— 内网自包含交付不适用 |
| **vedo** 2026.6(活跃) | `Mesh(points, faces)`;three.js 后端可导 HTML(`--backend threejs`/x3d) | 高层封装 | 导出的 HTML **无时间轴控件**;动画录制走 `Video` 类(需 ffmpeg) |
| **meshio → XDMF 时序** | `TimeSeriesWriter`(XDMF3+HDF5) | **ParaView 直接打开即得时间轴回放**,零前端开发;PyVista `XdmfReader` 亦可编程遍历 | 需单元连接表(节点云只能 vertex cells)→ 建议作为上游采用 §3 档②(emap.csv)时的增强工件 |
| ansys 系(dpf / mapdl-core / visualization-interface) | 全部绑定 rst | — | CSV 绕 rst 不值得;直接要 rst 见 §7 FAQ |

> 快速判据:要**嵌进自己前端** → three.js 系(或直接拿参考实现模板改);
> 要**现成时间轴控件少写代码** → k3d(接受定制受限);
> 要**给工程师看/出图出视频** → XDMF+ParaView 或 PyVista;
> 两类都不要 → 手册 §2 配方在任何图形栈里都是那六步。

## §5 参考实现

**前端即插即用件(推荐前端直接用)**:`packages/hip-playback/` — 上述配方与
构网逻辑的 Web Component 化(three.js r128 同版,单文件 `dist/hip-playback.js`
自包含零构建),两行接入:

```html
<script src="hip-playback.js"></script>
<hip-playback base-url="http://hip内网:8010" job-id="…"></hip-playback>
```

构网双路径自动选择(工件含 `emap.csv` → §3 档②通用构网;否则档①规则格反推);
本地数据走 `loadData(files)`;跨域拉取需服务端开 `server.cors_origins`(只放行 GET)。
开发与测试见包内 README。

Python 离线参考实现 `examples/passthrough-demo/playback/`:

```bash
python build_viewer.py <工件目录>          # 含 frame_N.csv 的目录
# → index.html(自包含,浏览器直接打开;#t=6&scale=10 直达末帧)
```

- 真机验收:`cube_dent` 工件(6 帧,压深 0.3→1.8 mm)再生页面像素级复现
  验收图(2026-09-11,MAPDL v252);
- 模板 `cube.template.html` 可作任何 three.js 前端的起点(`__DATA__` 注入,
  数据结构见 builder 源码 `data` dict);
- 交互:时间轴/播放/变形倍率/viridis 云图/压头与轮廓显隐;`#debug` 锚点
  dump 顶点级数据与拾取链(排查渲染问题)。

## §6 已知坑(全部实测踩过)

1. **三角绕向与法线**:多面拼域时,按同一公式生成的三角在部分面会内翻
   (几何法线朝内)。判据:`(a轴×b轴)·面外法线 < 0` 则翻绕向 —— 否则该面
   从外侧看是背面,被光照成黑面。**逐像素光照材质**(three.js Phong 等)在
   双面渲染时按朝向翻法线可救一半;顶点光照(Lambert)救不了,直接黑。
2. **深窄凹坑的遮挡**:放大倍率大时(如 ×8 把 1.8 mm 放成 14 mm),浅视角
   看不进坑底 —— 不是渲染 bug,是几何遮挡。倍率与默认视角要联动调
   (参考实现:×5 + 俯视约 55°)。
3. **帧体积与注入**:帧数据内嵌 HTML 时量化到 4~5 位小数(坐标 4 位、位移
   5 位足够),502 KB 页面秒开;不量化会翻倍。更极致:`int16` 量化(u/max×32767)
   + base64,体积再省一半以上,前端解码即可(参考实现暂用文本量化,已够用)。
4. **坐标列是初始构型**:别用第 N 帧坐标当第 N+1 帧基准 —— 每帧都从头算
   `基准+scale×u`,避免误差累积。
5. **E16.8 前导空格**:CSV 数值带空格,严格按 float 解析,别 split 后裸用。

## §7 FAQ

- **想要视频(mp4/gif)?** 数据侧不变;上游用自己工具链离线渲染录制
  (PyVista 循环 warp+截图 → ffmpeg;或 ParaView XDMF 路线的动画导出)。
  服务只出数据,不出媒体 —— 纯转发定位。
- **能直接拿 rst 全场文件吗?** 可以:把 `hipjob.rst` 列进 declared_outputs
  流式下载即可(guide §6.9)。但它是 MAPDL 私有二进制(几十 MB 起),且需要
  ANSYS 系工具链才能读;文本帧 CSV 是通道正途,除非上游本来就有 rst 消费链。
- **2D 轴对称帧?** 同一契约(`capsule_shrink.inp`):x-y 剖面帧回转 360°
  即得三维;e2e 目录有完整 three.js 参考(剖面+回转壳+剖切)。
- **帧数想要更密?** 段数 NSEG 在 .inp 里自定(帧=段末态,段内还有子步);
  更平滑过程 = 更多段,或与 lead 商量导出子步级帧(OUTRES 已存全部子步)。
- **上游不想自己写 .inp?** 那是另一话题:通道只运输,建模模板从
  `examples/passthrough-demo/` 的三个 .inp 起步。
