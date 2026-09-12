/** 构网分发:emap 按形状档分 hex/tet 两条构网路径,否则规则 hex20 格反推。
 *
 * epart 前置校验:必须与 tet emap 同供(缺席 emap 或非 tet 档都显式中文报错,
 * 不静默降级 —— epart 单独存在会让 HUD 工件清单与部件能力双双失实)。
 * 三条路径产出的 MeshData 同构(共享 mesh/common.ts 装配口径),
 * 同一份规则格数据上可交叉验证(见 tests/cross-mesh.test.ts)。
 */

import { buildEmapMesh } from "./emap";
import { buildLatticeMesh } from "./lattice";
import { buildTetMesh } from "./tet";
import type { MeshData, MeshInput } from "./types";

export function buildMeshData(input: MeshInput): MeshData {
  const { emap, epart } = input;
  if (epart) {
    if (!emap) {
      throw new Error("epart 需与 emap.csv 同供:缺单元连接表,部件归属无从标注");
    }
    if (emap.cell !== "tet") {
      throw new Error("epart 部件分组当前仅支持 tet 皮肤:hex emap 请勿携带 epart.csv 侧车");
    }
  }
  if (emap) {
    return emap.cell === "tet"
      ? buildTetMesh({ ...input, emap, epart: input.epart })
      : buildEmapMesh({ ...input, emap });
  }
  return buildLatticeMesh(input);
}

export type { MeshData, MeshInput } from "./types";
