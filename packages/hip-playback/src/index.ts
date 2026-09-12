/** hip-playback — ansys-hip passthrough 帧工件的即插即用 3D 回放组件。

 * 前端接入(全部代码):
 *   <script src="hip-playback.js"></script>
 *   <hip-playback base-url="http://内网:8010" job-id="…"></hip-playback>
 *
 * 引入即注册自定义元素;同时命名导出数据层(解析/构网/装载)供进阶使用。
 * 数据契约与渲染配方见仓库 docs/playback-handbook.md;本库 = 参考实现
 * (docs/examples/passthrough-demo/playback/)的组件化移植:
 * 构网(CSV 建模)与渲染全部在浏览器侧完成,Python 端不再预生成页面。
 */

import { defineHipPlayback } from "./element";

export { HipPlaybackElement, defineHipPlayback } from "./element";
export type { LoadedArtifacts } from "./loader";
export { loadFromFiles, loadFromService, loadFromTexts } from "./loader";
export { buildMeshData } from "./mesh";
export type { MeshData, MeshInput } from "./mesh/types";
export {
  parseEmapCsv,
  parseFrameCsv,
  parseProgressCsv,
  parseResultsCsv,
} from "./csv";
export type { EmapTable, FrameNodes, StageRow, Vec6 } from "./csv";
export { segOf, depthAt, interpolateFrame } from "./playback-model";
export { viridis, VIRIDIS_STOPS } from "./colormap";

export const VERSION = "0.1.0";

// 脚本标签引入(<script src>)即注册;ESM 引入方亦可自行 defineHipPlayback()
defineHipPlayback();
