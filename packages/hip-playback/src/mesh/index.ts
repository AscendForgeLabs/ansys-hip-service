/** 构网分发:有 emap.csv 走通用六面体构网,否则规则 hex20 格反推。

 * 两条路径产出的 MeshData 同构(共享 mesh/common.ts 装配口径),
 * 同一份规则格数据上可交叉验证(见 tests/cross-mesh.test.ts)。
 */

import type { EmapTable } from "../csv";
import { buildEmapMesh } from "./emap";
import { buildLatticeMesh } from "./lattice";
import type { MeshData, MeshInput } from "./types";

export function buildMeshData(input: MeshInput): MeshData {
  return input.emap
    ? buildEmapMesh({ ...input, emap: input.emap as EmapTable })
    : buildLatticeMesh(input);
}

export type { MeshData, MeshInput } from "./types";
