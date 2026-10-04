# passthrough 示范工程:轴对称包套缩放演示

自包含的 passthrough 通道端到端演示(不依赖任何外部几何/网格文件),也是上游
.inp 编写者的种子模板。完整契约见 `../../passthrough-guide.md`(本文只讲跑通)。

## 文件

| 文件 | 角色 |
|---|---|
| `capsule_shrink.inp` | 入口命令流:PLANE183 轴对称;`RECTNG` 自建粉末/包套两区 + `AGLUE` + `AATT`(MAT 1=powder、2=capsule 约定);线弹性占位本构(演示通道,**不背书 HIP 物理**);`NLGEOM,1`;4 段升压载荷步循环;每段写一帧 `frame_N.csv` + 整文件重写 `progress.csv`;末尾写 `deform.csv`(变形几何数据)与 `results.csv`(短标签标量) |
| `cube_dent.inp` | 入口命令流:**全 3D 版帧导出演示**(capsule 是 2D 轴对称,本案例是真三维实体网格)。SOLID186 六面体(`BLOCK` 20 mm 方块 + `ESIZE,2` + `VMESH`,4961 节点);顶面中央 4x4 mm 方形压头节点集 6 段逐步下压 0.3→1.8 mm(`NLGEOM`);帧列含 z/uz 三分量(node,x,y,z,ux,uy,uz);MAT 1=powder 占位本构。**回放参考实现见 `playback/` 子目录**(帧→3D 回放页,消费手册见 `../../playback-handbook.md`) |
| `vm1_axial_bar.inp` | **官方 Verification Manual VM1 已知答案 E2E 夹具**:两端固定直杆轴向加载(LINK180,官方命令流形态);官方目标反力 900/600 lb + 解析位移 -8.0e-5 / -9.0e-5 in(载荷向下,位移为负)写进 `results.csv`;写 `progress.csv`(MODEL/SOLVE/POST)与 `disp.csv`(declared)。单位故意保留官方原制 in/lbf/psi —— 验证服务不预设单位制 |
| `vm3_thermal_support.inp` | **官方 Verification Manual VM3 已知答案 E2E 夹具(热-结构耦合)**:铜/钢三杆并联(LINK180,底部 UY 耦合成刚性梁),ΔT=+10°F + 4000 lb,官方目标热应力 钢 19695 / 铜 10152 psi 写进 `results.csv`(`st_strs`/`cu_strs`/`ratio_st`/`ratio_cu`);写 `progress.csv`(MODEL/SOLVE/POST)与 `disp.csv`(declared)。单位保留官方原制 in/lbf/psi/°F |
| `submit_demo.py` | httpx 提交脚本:上传 → 提交 → 轮询打印 status+stages → 流式下载工件;注释里有逐阶段等价 curl |
| `README.md` | 本文件 |

## 一分钟跑通

前置:部署机已装 v252 与许可,`config/service.yaml` 的 `passthrough.enabled: true`
(**默认 false,仅纯内网允许开启**;公网隧道期间必须关)。服务在 `:8010`:

```bash
# 0) 确认开关已开(否则提交得 403 PASSTHROUGH_DISABLED);服务开启鉴权时
#    先导出 key(submit_demo.py 与下方 curl 都会用到,与服务端环境变量同名):
#    export HIP_SERVICE_API_KEYS=<部署签发的 key>
curl -s http://localhost:8010/health

# 1) 跑提交脚本(上传 → 提交 → 轮询 → 下载,全自动)
pip install httpx
python submit_demo.py http://localhost:8010
```

想手动逐步跑(等价 curl 全集在 `submit_demo.py` 顶部注释):

```bash
curl -s -H "X-API-Key: $HIP_SERVICE_API_KEYS" -F 'file=@capsule_shrink.inp' http://localhost:8010/uploads/apdl
# → {"path": "/var/uploads/xxx_capsule_shrink.inp", "size_bytes": ...}

curl -s -X POST http://localhost:8010/sim/passthrough \
  -H "X-API-Key: $HIP_SERVICE_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"params": {"entry_file": "/var/uploads/xxx_capsule_shrink.inp",
        "declared_outputs": ["frame_1.csv","frame_2.csv","frame_3.csv",
                              "frame_4.csv","deform.csv"],
        "workflow": "HIP_DEMO_V1", "timeout_s": 1800}}'
# → 202 {"id": "...", "method": "passthrough", "status_url": "/jobs/..."}

watch -n3 'curl -s -H "X-API-Key: $HIP_SERVICE_API_KEYS" http://localhost:8010/jobs/<id>'   # 盯 status 与 stages
```

## 预期产物

作业 succeeded 后,`demo-artifacts/` 下应得到(脚本自动下载):

| 工件 | 来源 | 用途 |
|---|---|---|
| `progress.csv` | 侧车(服务自动发布) | 阶段进度(MESH + 4 个求解段,标签+累计秒) |
| `frame_1.csv` … `frame_4.csv` | 每载荷段一帧(declared) | 动画数据(Channel C:数据帧 + 前端渲染) |
| `deform.csv` | 末段后处理(declared) | 全节点 初始坐标+位移+变形后坐标 → 上游 final_powder.step 重构数据源 |
| `results.csv` | 末段后处理(自动) | 短标签标量,服务解析进 result 的 `values`(ux_max / uy_max / shrink_r / p_final) |
| `capsule_shrink.inp` | 入口回声(自动) | 溯源 |
| `job.out` | MAPDL 输出(无条件) | 错误分析 |

量级参考(线弹性占位本构,仅验证通道):`shrink_r` ≈ 2% 量级的径向收缩。

## VM1 官方算例 E2E(已知答案)

`capsule_shrink.inp` 只验证"通道打得通";`vm1_axial_bar.inp` 在此之上提供
**数值级**端到端验证 —— 算例与目标值均出自官方(Ansys Mechanical APDL
Verification Manual VM1,参考解 Timoshenko p.26 prob.10),跑通后
`result.values` 逐项对上即证明"忠实转发 + 结果解析"全链路正确。

```bash
curl -s -F 'file=@vm1_axial_bar.inp' http://localhost:8010/uploads/apdl
# → {"path": "/var/uploads/xxx_vm1_axial_bar.inp", ...}

curl -s -X POST http://localhost:8010/sim/passthrough \
  -H "X-API-Key: $HIP_SERVICE_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"params": {"entry_file": "/var/uploads/xxx_vm1_axial_bar.inp",
        "declared_outputs": ["disp.csv"], "workflow": "VM1_E2E"}}'
# → 202 {"id": "...", "status_url": "/jobs/..."}

# succeeded 后取结果,values 对照下表:
curl -s http://localhost:8010/jobs/<id>/result
```

`values` 断言表(相对误差 < 1e-3 即通过):

| 短标签 | 目标值 | 出处 |
|---|---|---|
| `r1_lb` | 900 | y=10 端反力(官方) |
| `r2_lb` | 600 | y=0 端反力(官方) |
| `ratio12` | 1.5 | R1/R2 |
| `u2_in` | -8.0e-5 | 节点 2 位移(解析,A=1 in²、EA=30e6 lb;载荷向下 → 位移为负) |
| `u3_in` | -9.0e-5 | 节点 3 位移(解析,同上) |

同时核对:`stages` 末帧 = `[MODEL, SOLVE, POST]`;工件含
`disp.csv`(declared)+ `progress.csv` / `results.csv` / 入口回声(保留上传
token 前缀,形如 `ab12…_vm1_axial_bar.inp`)/ `job.out`(自动)。
(2026-09-11 真 MAPDL v252 实测:13 项断言全过,values 相对误差 0,端到端 ~5 s。)

## VM3 官方算例 E2E(热-结构耦合,已知答案)

VM1 验证纯力学校核;`vm3_thermal_support.inp` 在此之上覆盖**热-结构耦合**
(双材料热膨胀差 + 机械载荷,更贴近 HIP 场景)。算例与目标值均出自官方
(Ansys Mechanical APDL Verification Manual VM3,参考解 Timoshenko p.30 prob.9)。

```bash
curl -s -F 'file=@vm3_thermal_support.inp' http://localhost:8010/uploads/apdl
# → {"path": "/var/uploads/xxx_vm3_thermal_support.inp", ...}

curl -s -X POST http://localhost:8010/sim/passthrough \
  -H "X-API-Key: $HIP_SERVICE_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"params": {"entry_file": "/var/uploads/xxx_vm3_thermal_support.inp",
        "declared_outputs": ["disp.csv"], "workflow": "VM3_E2E"}}'
# → 202 {"id": "...", "status_url": "/jobs/..."}

# succeeded 后取结果,values 对照下表:
curl -s http://localhost:8010/jobs/<id>/result
```

`values` 断言表(**容差 1e-2** —— 官方目标为手册圆整值;`ratio_*` ≈ 1):

| 短标签 | 目标值 | 出处 |
|---|---|---|
| `st_strs` | 19695 | 钢杆(中间杆)轴向应力(官方,psi) |
| `cu_strs` | 10152 | 铜杆(两侧杆)轴向应力(官方,psi) |
| `ratio_st` / `ratio_cu` | ≈1.0 | 实测/目标 比值 |

同时核对:`stages` 末帧 = `[MODEL, SOLVE, POST]`;工件含 `disp.csv`(declared)+
自动件 + 入口回声(同 VM1)。
(2026-09-11 真 MAPDL v252 实测:12 项断言全过,应力相对误差 ~2.5e-5,端到端 ~4 s。)


## 正方体三维凹陷帧 E2E(全 3D)

capsule 的帧是 2D 轴对称剖面;`cube_dent.inp` 用同一契约形态产出**全三维**
"逐步凹陷"过程帧 —— 上游按帧读 csv 插值即可在任意前端(three.js 等)回放
三维动画,服务零改动(帧文件就是普通 declared_outputs 工件)。

```bash
curl -s -F 'file=@cube_dent.inp' http://localhost:8010/uploads/apdl
# → {"path": "/var/uploads/xxx_cube_dent.inp", ...}

curl -s -X POST http://localhost:8010/sim/passthrough \
  -H "X-API-Key: $HIP_SERVICE_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"params": {"entry_file": "/var/uploads/xxx_cube_dent.inp",
        "declared_outputs": ["frame_1.csv","frame_2.csv","frame_3.csv",
                              "frame_4.csv","frame_5.csv","frame_6.csv",
                              "deform.csv"],
        "workflow": "CUBE_3D_E2E", "timeout_s": 900}}'

# succeeded 后取结果:
curl -s http://localhost:8010/jobs/<id>/result
```

`values` 断言表(压深为既设位移,精确;应力仅通道级检查):

| 短标签 | 目标值 | 出处 |
|---|---|---|
| `dent_dep` | 1.8 | 末段压深(既设,mm) |
| `uy_min` | ≈ -1.8 | 全场最小 UY = 压坑底(既设位移 ±2%) |
| `seqv_max` | > 0 | 全场最大 von Mises(占位本构,量级不作数) |

同时核对:`stages` 末帧 = `[MESH, DENT_03 … DENT_18]`(时间列 0…3600 s);
工件含 `frame_1..6.csv` + `deform.csv`(declared)+ 自动件 + 入口回声;
帧内逐帧最小 uy = -0.3 → -1.8 单调加深。
(2026-09-11 真 MAPDL v252 实测:14 项断言全过,端到端 ~1 min。)

## 改模板时注意

- **改段数 NSEG**:提交侧 `declared_outputs` 的帧文件名必须同步
  (`*CFOPEN,frame_%I%,csv` 生成 `frame_1.csv .. frame_<NSEG>.csv`);
- 每个 `*VWRITE` 字符字面量标签 ≤8 字符(超长被 MAPDL 静默截断,写读两侧撞键);
- `progress.csv` / `results.csv` / 入口 .inp / `job.out` 由服务自动处理,
  **不要**放进 `declared_outputs`;
- 有附属 .cdb 时:逐个 `POST /uploads/apdl` 上传,路径填 `extra_files`,
  .inp 内裸文件名 `CDREAD` 引用;
- **位移极值提取用 `*VSCFUN,Par,MIN/UZA(1)` 数组式**,不要用
  `NSORT,U,Y,1,1` + `*GET,SORT,,MIN` —— NSORT 默认 `KABS=1` 按**绝对值**
  排序,升序取 MIN 会拿到 0(约束面上 uy=0 的节点);
- **平坦方形压头的压深有上限**:边界剪切集中,实测压深 2.0 mm 收敛、
  2.5 mm 时压头边缘 SOLID186 单元过度畸变发散(`cube_dent.inp` 取 1.8 mm
  留余量);要更深凹陷需分级压深(环形分级)或改接触。
