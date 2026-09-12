/** 构网分发:emap 按形状档分 hex/tet 两条构网路径,否则规则 hex20 格反推。
 *
 * 三条路径产出的 MeshData 同构(共享 mesh/common.ts 装配口径),
 * 同一份规则格数据上可交叉验证(见 tests/cross-mesh.test.ts)。
 */

import type { EmapTable } from "../csv";
import { buildEmapMesh } from "./emap";
import { buildLatticeMesh } from "./lattice";
import { buildTetMesh } from "./tet";
import type { MeshData, MeshInput } from "./types";

export function buildMeshData(input: MeshInput): MeshData {
  if (input.emap && input.epart && input.emap.cell !== "tet") {
    throw new Error("epart 部件分组当前仅支持 tet 皮肤:hex emap 请勿携带 epart.csv 侧车");
  }
  if (input.emap) {
    const emap = input.emap as EmapTable;
    return emap.cell === "tet"
      ? buildTetMesh({ ...input, emap, epart: input.epart })
      : buildEmapMesh({ ...input, emap });
  }
  return buildLatticeMesh(input);
}

export type { MeshData, MeshInput } from "./types";
