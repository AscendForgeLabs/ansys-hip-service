# ansys-hip-service 简要 API 文档

版本 v1.0(2026-09-09)

> HIP 仿真**方法级** HTTP 服务:HIPForm 按需调用单个计算方法(`/sim/*`),作业异步执行,
> 轮询取结果。本文是快速上手指引;**字段级权威文档以 Swagger(`/docs`)与 `/redoc` 为准**
>(pydantic 模型的 description/examples 即文档)。
> 单位约定:mm / MPa / s / ℃。材料/工艺规格见 `docs/materials-process.md`。

## 1. 快速开始

```bash
uv sync
uv run uvicorn ansys_hip.main:app --host 0.0.0.0 --port 8010
# Swagger:  http://<host>:8010/docs      ReDoc: http://<host>:8010/redoc
# 健康检查: curl http://<host>:8010/health
```

依赖环境:ANSYS 2022 R1(`ansys221` 批处理,单许可 → 队列并发 1)、Python 3.12 /
FastAPI / pydantic v2 / numpy / scipy / gmsh / meshio。

## 2. 端点总览

| 端点 | 方法 | 用途与行为 |
|---|---|---|
| `/health` | GET | 服务健康:MAPDL 可执行/许可环境/队列深度/版本 |
| `/sim/methods` | GET | 12 方法自描述(分组/状态/保真度/参数 JSON Schema);**返回裸数组** |
| `/sim/{method}` | POST | 提交计算。**一律 202 受理**(校验不通过则 4xx,顺序见 §6);排队状态经 `GET /jobs/{id}` 的 `status=pending` 体现,**无 Retry-After 头**(并发 1,`service.yaml` `queue.max_concurrent`) |
| `/jobs/{id}` | GET | 作业状态(pending/running/succeeded/failed/cancelled)+ log/result/artifacts 链接 |
| `/jobs/{id}/result` | GET | 成功作业的结果 JSON;未完成:pending/running/cancelled → **409 RESULT_NOT_READY**,failed → **409 JOB_FAILED**(body 带原 error 的 code/message) |
| `/jobs/{id}/artifacts` | GET | **工件集合端点**:返回文件名列表(与 result.json 的 `artifacts` 数组一致);`JobState.artifacts_url` 即指向本端点 |
| `/jobs/{id}/artifacts/{name}` | GET | 下载单个工件(mesh.cdb / deformed.stl / 补偿后 STEP 等);名称不在列表 → 404 ARTIFACT_NOT_FOUND |
| `/jobs/{id}` | DELETE | **取消并清理**:pending → 移出队列;running → 防御性 kill MAPDL 进程组;均删除作业记录与目录,返回 **204**;此后 `GET /jobs/{id}` → 404。cancelled/finished 作业同样可 DELETE 清理 |
| `/parts` | GET | 可用零件配置列表(config/parts/*.yaml) |
| `/uploads` | POST | 上传几何(multipart)→ 返回服务端绝对路径供 `geometry` 引用。扩展名白名单 `.step/.stp/.stl`;文件名消毒取 `Path.name`;落盘 `var/uploads/<token>_<原名>`(暂无大小上限,见 §8) |

## 3. 配置体系(三级覆盖)

```
请求内联 params  >  config/parts/<零件>.yaml  >  config/service.yaml (defaults)
```

- 请求只给 `part`(全继承零件配置)或只给 `params`(内联)或两者都给(内联覆盖零件配置);
- 内联 `params` 只接受该方法参数模型的**已知顶层键**,未知键 → 400 INVALID_PARAMS
 (防止拼写错误被静默忽略);
- 合并结果写入作业目录 `resolved-params.json`,任何一次计算都可追溯实际参数;
- 默认工艺曲线 900℃/120 MPa/保温 3 h(`defaults.cycle`,五点,见 materials-process.md §2.2)。

## 4. 方法一览(12 个;权威来源:`registry.py`,与 `GET /sim/methods` 一致)

| 方法 | 分组 | 状态 | 保真度 | 典型耗时 | MAPDL | 需几何 | 一句话用途 |
|---|---|---|---|---|---|---|---|
| `densification` | 快速计算 | available | real | <1 s | — | — | 致密化曲线 D(t):Arrhenius 动力学沿工艺曲线积分,回答"该工艺下密度能否到 0.97" |
| `material-query` | 快速计算 | available | real | <1 s | — | — | 材料性能查询:TC4/20 钢温度相关参数(模量/屈服/蠕变/Gurson,文献初值) |
| `mesh` | 快速计算 | available | real | 10–60 s | — | ✓ | STEP→粉末域推导(cad_workflow 移植)→Gmsh(physical groups 分包套/粉末)→.cdb/msh/stl |
| `process-window` | 快速计算 | available | real | 5–30 s | — | — | 工艺窗口扫参:温度×压力×保温时长网格 → 终态密度,输出达标(≥0.97)窗口 |
| `shrinkage-estimate` | 快速计算 | available | real | <1 s | — | — | 均匀收缩估算:(D0/Df)^(1/3) 体积守恒,给各特征尺寸收缩量与收缩率 |
| `axisym-hip` | 2D FEM | experimental | **smoke** | 2–10 min | ✓ | profile/geometry 二选一 | 2D 轴对称 HIP 全过程(阶段 1 冒烟=真实几何+线弹性占位;阶段 2 换 Gurson+蠕变+接触,API 不变) |
| `axisym-mechanical` | 2D FEM | experimental | **smoke** | 1–5 min | ✓ | 二选一 | 保温段力学:外压下包套/粉末应力分布与位移(阶段 2 换 Gurson 等温力学) |
| `axisym-thermal` | 2D FEM | available | real | 1–5 min | ✓ | 二选一 | 升温段纯热瞬态(PLANE77):芯部-表面温差滞后曲线,校核均匀温度假设 |
| `full3d-hip` | 3D FEM | experimental | **smoke** | 0.5–4 h | ✓ | ✓ | 3D 全模型 HIP(真实 STEP 网格+线性占位本构;阶段 2 换 SOLID187+接触+NLGEOM) |
| `calibrate` | 反演/优化 | available | real | 5–60 s | — | — | 实验 D-t 数据 → Arrhenius 动力学参数(scipy least_squares;阶段 2 升级 Gurson 反演) |
| `compensate` | 反演/优化 | available | real | 5–30 s | — | ✓(cavity) | 预变形补偿:目标型腔按 (Df/D0)^(1/3) 放大,输出补偿后 STEP(阶段 2 换 FEM 迭代) |
| `sensitivity` | 反演/优化 | available | real | 5–60 s | — | — | 参数敏感性:逐参数批量跑致密化核,输出各参数对终态密度的敏感度排序 |

分组标签:快速计算(解析/数值秒级)/ 2D 轴对称 FEM(MAPDL)/ 3D 全模型 FEM(MAPDL)/ 反演优化。
**各方法参数字段级说明(类型/默认值/合并粒度/结果形态)见 `docs/methods-reference.md`**;
参数模型定义于 `src/ansys_hip/schemas.py`(冻结契约)。

## 5. 保真度契约(real / smoke)

- `real`:当前内核即最终形态(Arrhenius·scipy·gmsh·PLANE77 热瞬态);
- `smoke`:**真实几何 + 占位(线弹性)本构**,用于打通求解管线与联调,结果量级不可用于
  工程判定;**阶段 2 换成 Gurson+蠕变+接触+NLGEOM 后 API 不变**,HIPForm 侧无需改动;
- 三处一致可见:`GET /sim/methods` 的 `status: experimental`、受理响应与作业状态的
  `fidelity: "smoke"`、结果 JSON 内的 `fidelity` 键。调用方必须透传展示,防误用。

## 6. 错误码

### 6.1 请求级(HTTP 响应体 `{code, message}`)

| code | 场景 | HTTP |
|---|---|---|
| `METHOD_NOT_FOUND` | `/sim/{method}` 方法名不在注册表 | 404 |
| `METHOD_DISABLED` | 方法被 config `methods.disabled` 显式停用 | 503 |
| `METHOD_NOT_IMPLEMENTED` | 方法在注册表但内核 `run_*` 尚未实现(懒加载缺失) | 501 |
| `PART_NOT_FOUND` | `part` 无对应 config/parts/*.yaml | 404 |
| `GEOMETRY_NOT_FOUND` | geometry STEP 路径不存在或不可读 | 404 |
| `INVALID_PARAMS` | 参数校验失败(含内联 params 未知顶层键、缺几何、范围越界、曲线点数不足) | 400 |
| `JOB_NOT_FOUND` | 作业 id 不存在(含已被 DELETE 清理、或服务重启后丢失,见 §8) | 404 |
| `RESULT_NOT_READY` | `result` 端点对 pending/running/cancelled 作业 | 409 |
| `JOB_FAILED` | `result` 端点对 failed 作业(body 带原 error 的 code/message) | 409 |
| `ARTIFACT_NOT_FOUND` | `artifacts/{name}` 文件名不在该作业工件列表 | 404 |

### 6.2 作业级(不出现在 HTTP 请求级;经 `GET /jobs/{id}` 的 `error{code,message}` 返回)

| code | 场景 |
|---|---|
| `MAPDL_NOT_FOUND` | ansys 可执行缺失(提交前可经 `/health` 预检:mapdl_found=false) |
| `LICENSE_UNAVAILABLE` | 许可不可用/签出超时 |
| `CONVERGENCE_FAILED` | MAPDL 非线性不收敛(message 附 job.out 关键行) |
| `TIMEOUT` | 超过 job_timeout_s(默认 14400 s)被终止 |
| `INTERNAL` | 未分类内核异常(日志留栈);含服务重启时磁盘遗留 pending/running 作业的改标(见 §8) |

### 6.3 行为注记

1. **提交校验顺序**(POST /sim/{method},全部通过才 202 受理):
   404 方法 → 503 下线 → 501 未实现 → 404 零件 → 400 参数;
2. 内联 `params` 出现未知顶层键 → 400 INVALID_PARAMS(防拼写错误被静默忽略);
3. 请求体校验失败统一 **400**(FastAPI 默认 422 已被覆盖);
4. `GET /sim/methods` 返回**裸数组**(无 `{items: [...]}` 包装);
5. 作业注册表阶段 1 在**内存**:服务重启后历史作业 `GET /jobs/{id}` → 404;磁盘遗留的
   pending/running 作业启动时改标 failed(INTERNAL)。

## 7. curl 示例

### 7.1 提交 → 轮询 → 取结果(densification,全默认曲线)

```bash
# 提交(只给 part,参数全继承三级配置链)
curl -s -X POST http://localhost:8010/sim/densification \
  -H 'Content-Type: application/json' \
  -d '{"part": "tc4-demo"}'
# → 202 {"id": "9d2f...", "method": "densification", "status": "pending",
#        "fidelity": "real", "status_url": "/jobs/9d2f..."}

# 轮询状态(排队中 status=pending;单并发,无 Retry-After)
curl -s http://localhost:8010/jobs/9d2f...
# → {"status": "succeeded", "result_url": "/jobs/9d2f.../result",
#    "log_url": "...", "artifacts_url": "/jobs/9d2f.../artifacts"}

# 取结果(未完成时提前取 → 409 RESULT_NOT_READY;failed → 409 JOB_FAILED)
curl -s http://localhost:8010/jobs/9d2f.../result
# → {"times_s": [...], "densities": [...], "final_density": 0.973,
#    "reached_097": true, "fidelity": "real"}
```

### 7.2 内联参数覆盖(axisym-hip,显式 2D 剖面)

```bash
curl -s -X POST http://localhost:8010/sim/axisym-hip \
  -H 'Content-Type: application/json' \
  -d '{
    "part": "tc4-demo",
    "params": {
      "profile": {"outer_radius_mm": 50, "height_mm": 150},
      "nlgeom": false
    }
  }'
# → 202;结果含 displacement_max_mm / von_mises_max_mpa / time_history
#   / artifacts[](冒烟:线弹性,仅验证管线)
```

### 7.3 工艺窗口扫参(process-window)

```bash
curl -s -X POST http://localhost:8010/sim/process-window \
  -H 'Content-Type: application/json' \
  -d '{"params": {
        "temperatures_c": [880, 900, 920, 940],
        "pressures_mpa": [100, 110, 120, 130, 140],
        "hold_times_s":  [7200, 10800, 14400]}}'
# → 结果:final_densities 三重嵌套 + window_ok[][][](≥0.97 判定)
```

### 7.4 工件与取消

```bash
# 工件列表(集合端点,与 result.json 的 artifacts 数组一致)
curl -s http://localhost:8010/jobs/9d2f.../artifacts
# → ["mesh.cdb", "capsule.stl"]

# 单个工件下载
curl -s -O http://localhost:8010/jobs/9d2f.../artifacts/mesh.cdb

# 取消并清理:pending → 移出队列;running → 防御性 kill MAPDL 进程组;
# 均删除作业记录与目录 → 204;此后 GET /jobs/9d2f... → 404 JOB_NOT_FOUND
curl -s -X DELETE http://localhost:8010/jobs/9d2f... -o /dev/null -w '%{http_code}\n'
# → 204
```

## 8. 已知限制(阶段 1)

| # | 限制 | 影响与对策 |
|---|---|---|
| 1 | 作业注册表在内存 | 服务重启后历史作业 GET 404;磁盘遗留 pending/running 启动时改标 failed(INTERNAL)。作业结果/工件落盘保留(retention 3d),重要结论请及时取走 |
| 2 | `/uploads` 无大小上限 | 内网部署的已知取舍;对接公网前须补上限 |
| 3 | FEM 方法忽略 `MaterialSelection.overrides` | 直接用材料库温度点列;逐参数覆盖留阶段 2 |
| 4 | `full3d-hip` 的 `deformed_stl` 返回 `null` | UPGEOM 变形网格导出留阶段 2;阶段 1 以位移/应力数值为准 |
| 5 | axisym 自动剖面假设几何轴沿 Z 且 X 居中 | 非此朝向的 STEP 请显式给 `profile`(inner/outer_radius + height) |
| 6 | 3D 冒烟单元为一阶 SOLID45 | 阶段 2 升 SOLID187(二阶四面体) |
| 7 | 冒烟方法量级不可用于工程判定 | 见 §5 保真度契约;`fidelity: "smoke"` 必须透传展示 |
