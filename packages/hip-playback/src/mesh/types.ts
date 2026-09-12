/** 构网(MeshData)契约 — 帧节点云 → 可渲染网格。

 * MeshData 与参考实现(cube.template.html 的 DATA dict)字段一一对应:
 *   vertBase/vertNorm/framesU/faces/stages/meta —— JS 移植与 Python 端
 *   (build_viewer.py)golden 对拍即钉死字段口径:顶点坐标 round 4、位移 round 5。
 * mode 为组件化新增:lattice(规则格反推,带参考轮廓/压头语义)vs emap(通用)。
 */

/** 单个外壳面的三角形索引与网格线索引(共享顶点缓冲,引用全局顶点序号)。 */
export interface FaceGeom {
  tris: number[];
  wire: number[];
}

export interface MeshMeta {
  side: number;                          // 包围盒最大边长(lattice = 立方边长)
  nseg: number;                          // 位移帧数
  colorMax: number;                      // 全帧全局 max|u|(云图归一)
  results: Record<string, number>;       // results.csv 标量(HUD)
}

export interface StageInfo {
  label: string[];                       // nseg + 1 个(含 MESH/t=0 装料态)
  time: number[];
  depth: number[];                       // 逐帧 |min uy|(压深口径,顶面压头类载荷)
}

export interface MeshData {
  meta: MeshMeta;
  vertBase: number[];                    // 平铺基准坐标 [x,y,z]×N
  vertNorm: number[];                    // 平铺静态面法线(光照用)
  framesU: number[][];                   // 每帧平铺位移 [ux,uy,uz]×N
  faces: FaceGeom[];
  stages: StageInfo;
  mode: "lattice" | "emap";
}

/** 构网输入(frames 已按帧号升序;坐标列以首帧为基准)。 */
export interface MeshInput {
  frames: ReadonlyArray<import("../csv").FrameNodes>;
  emap?: import("../csv").EmapTable;     // 缺席 → 尝试规则格反推
  stageLabels?: string[];
  stageTimes?: number[];
  results?: Record<string, number>;
}
