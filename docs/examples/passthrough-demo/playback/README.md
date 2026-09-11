# 三维回放参考实现(cube 型)

passthrough 帧工件 → 自包含交互式 3D 回放页的参考实现(渲染配方与各栈生态
选项见 `../../../playback-handbook.md`)。

## 用法

```bash
# frames_dir = 已下载的 passthrough 工件目录(含 frame_N.csv,
#              可选 progress.csv / results.csv;下载方式见 ../../README.md)
python build_viewer.py <frames_dir> [--output-dir <目录>]

# 例:对本仓库示范案例的真机工件(在 e2e 回传目录时)
python build_viewer.py ~/ansys-hip-e2e-20260911/cube-dent/artifacts
# → 生成 <frames_dir>/index.html,浏览器直接打开
#    锚点直达:#t=6&scale=10(第 6 帧、×10 倍率);#nopunch 隐藏压头
```

产物 `index.html` 自包含(数据 + `three.min.js` 本地引用,零网络零构建),
交互:时间轴/播放/变形倍率、viridis |u| 云图、压头/网格线/参考轮廓显隐、
拖转/滚轮。HUD 压深口径 = 逐帧 `|min uy|`(顶面压头类载荷语义)。

## 边界

- 适用**规则结构网格六面体域**(hex20:每轴唯一坐标 = 角点等距格 + 棱中点,
  自动检测,不符即报错)——`cube_dent.inp` 即此形态;
- 非规则网格/其他单元形态:按手册 §3 档②(自导 emap.csv 连接表)或
  档③(点云)在各自前端栈实现,`cube.template.html` 可当 three.js 起点;
- 模板内嵌 `#debug` 调试锚点(dump 顶点级 pos/col/拾取链,排查渲染问题用)。
