# ansys-hip-service 方法说明文档(字段级)

版本 v1.0(2026-09-10)· 面向上游调用方(HIPForm)

> 本文是 **12 个仿真方法的字段级参考**:每个方法接受哪些参数、类型、默认值、结果形态。
> 调用模式、端点总览、错误码见 `docs/api-brief.md`;机器可读真源为
> `GET /sim/methods`(各方法 `params_schema`)与 Swagger(`/docs`)。
> 单位约定:**mm / MPa / s / ℃**。提交走各方法的类型化端点 `POST /sim/{name}`
> (泛化 `POST /sim/{method}` 运行时兼容、不在 Swagger 展示),202 受理 →
> 轮询 `GET /jobs/{id}` → `GET /jobs/{id}/result` 取结果、`/artifacts/{name}` 下工件。

---

## 0. 调用方必读(四条规则)

1. **三级合并**:`请求内联 params` > `零件配置(part)` > `主配置 defaults`。
   参数几乎全部可选 —— 只给 `{"part": "tc4-demo"}` 即可跑通;内联只写想覆盖的键。
   合并结果写入作业目录 `resolved-params.json`,可追溯。
2. **内联只收已知顶层键**,拼错 → 400 `INVALID_PARAMS`(防静默吞字段;
   嵌套 `base`(process-window / sensitivity)同样拒绝,其余嵌套构件忽略未知子键)。
3. **合并粒度**(内联给部分子字段时):
   - `cycle`:`points` 列表**整体替换**,不逐点合并;
   - `mesh` / `materials`(含 `overrides`):**深合并**(内联键覆盖,其余保留);
   - `geometry` / `numerics`:零件配置提供,内联同样深合并;
   - 标量/列表(如 `initial_relative_density`、`probe_points`):整体替换。
4. **保真度**:`fidelity: "smoke"` = 真实几何 + 线弹性占位本构(联调用,量级不可用于
   工程判定);`"real"` = 当前内核即最终形态。受理响应、作业状态、结果 JSON 三处都带,
   调用方必须透传展示。

---

## 1. 公共参数构件(多方法共用,后文直接引用)

### 1.1 `Cycle` — 工艺曲线

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `points` | `CyclePoint[]` | ✓(≥2 点) | 按 `time_s` 升序;相邻点线性过渡 |

`CyclePoint`:`time_s`(s,≥0)、`temperature_c`(℃,0–2500)、`pressure_mpa`(MPa,0–500)。
缺省曲线(不传 `cycle` 时):900 ℃ / 120 MPa / 保温 3 h,五点
(0→3600 升温、3600→14400 保温、14400→18000 降温)。

### 1.2 `GeometryRef` — 几何引用

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `capsule_step` | string | 视方法 | 包套 STEP 绝对路径(可先 `POST /uploads` 上传取得) |
| `cavity_step` | string | 视方法 | 目标型腔 STEP 绝对路径 |

axisym 自动剖面假设几何**轴沿 Z 且 X 居中**;其他朝向请改用 `profile` 显式给尺寸。

### 1.3 `AxisymProfile` — 显式 2D 剖面(优先于 geometry 自动推导)

| 字段 | 类型 | 说明 |
|---|---|---|
| `inner_radius_mm` | number | 内半径;实心粉末省略/None |
| `outer_radius_mm` | number | 外半径(与 `height_mm` 一起给才生效) |
| `height_mm` | number | 轴向高度 |

### 1.4 `MaterialSelection` — 材料选定

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `powder` | string | `"tc4"` | 粉末材料名(material_lib 键) |
| `capsule` | string | `"20steel"` | 包套材料名 |
| `overrides` | object | `{}` | 按材料名逐参数覆盖,例 `{"tc4": {"yield_stress_mpa": 800}}`。**阶段 1 FEM 方法忽略此键**(直接用材料库点列),快速计算方法同样不用 |

### 1.5 `MeshSettings` / `Numerics`

| 构件 | 字段 | 默认 | 说明 |
|---|---|---|---|
| `MeshSettings` | `mesh_size_mm` | `6.0` | 特征单元尺寸(mm) |
| `Numerics` | `time_step_s` | `null` | 时间步长(s);null = 内核默认(曲线总长/120) |

### 1.6 `DensificationParams` — 致密化基准(被 process-window / sensitivity 嵌套)

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `initial_relative_density` | number | `0.65` | 初始相对密度(0<D<1) |
| `limiting_relative_density` | number | `0.995` | 极限相对密度(0<D≤1) |
| `cycle` | Cycle | `null` | null = 继承配置链默认曲线 |
| `material` | MaterialSelection | `null` | null = 默认 TC4 |
| `kinetics` | object | `{}` | Arrhenius 覆盖:`k_ref`[1/s]、`q_j_per_mol`、`pressure_exponent`、`t_ref_c`、`p_ref_mpa` |

---

## 2. 快速计算组(不跑 MAPDL)

### 2.1 `densification` — 致密化曲线 D(t) · real · <1 s

回答"该工艺下密度能否到 0.97":Arrhenius 动力学沿工艺曲线积分。

**参数**:顶层字段 = §1.6 `DensificationParams` 全部五个字段直接平铺
(例 `{"params": {"initial_relative_density": 0.68}}`)。

**结果**:`{times_s[], densities[], final_density, reached_097: bool, fidelity}`

```bash
curl -X POST http://<host>:8010/sim/densification -H 'Content-Type: application/json' \
  -d '{"part": "tc4-demo", "params": {"initial_relative_density": 0.68}}'
```

### 2.2 `process-window` — 工艺窗口扫参 · real · 5–30 s

温度×压力×保温时长网格,各组合终态密度 + 达标(≥0.97)窗口判定。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base` | DensificationParams | §1.6 默认 | 基准参数(扫参外的其余输入) |
| `temperatures_c` | number[] | `[880, 900, 920, 940]` | 温度扫参点(℃) |
| `pressures_mpa` | number[] | `[100, 110, 120, 130, 140]` | 压力扫参点(MPa) |
| `hold_times_s` | number[] | `[7200, 10800, 14400]` | 保温时长扫参点(s) |

**结果**:`{grid{temperatures_c[], pressures_mpa[], hold_times_s[]}, final_densities[][][], window_ok[][][], fidelity}`

### 2.3 `shrinkage-estimate` — 均匀收缩估算 · real · <1 s

D0→Df 体积守恒 `(D0/Df)^(1/3)`,给各特征尺寸收缩量与收缩率。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `initial_relative_density` | number | | `0.65` | 初始相对密度 |
| `final_relative_density` | number | | `0.97` | 终态(或目标)密度 |
| `characteristic_lengths_mm` | object | ✓(≥1 项) | | 命名特征尺寸(mm),例 `{"height": 150, "outer_diameter": 100}` |

**结果**:`{items[{name, length_mm, final_mm, shrink_mm, shrink_ratio}], linear_strain, fidelity}`

### 2.4 `material-query` — 材料性能查询 · real · <1 s

TC4 粉末 / 20 钢包套温度相关参数(模量/屈服/蠕变/Gurson,文献初值)。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `material` | string | `"tc4"` | 材料名:`tc4` \| `20steel` |
| `temperatures_c` | number[] | `[20, 400, 600, 800, 900]` | 查询温度点(℃) |
| `properties` | string[] | `null` | 限定属性(如 `young_modulus_gpa`, `yield_stress_mpa`);null = 全部 |

**结果**:`{material, rows[{temperature_c, properties{}}], source_notes[], fidelity}`

### 2.5 `mesh` — 网格转换 · real · 10–60 s · 需几何

STEP → 粉末域推导(cad_workflow 逻辑)→ Gmsh(physical groups 分包套/粉末)→ .cdb 等。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` | GeometryRef | ✓ | | `capsule_step` 必填 |
| `mesh` | MeshSettings | | §1.5 | |
| `derive_powder_domain` | boolean | | `true` | 是否由 cavity 推导粉末域;false = 仅包套(降级网格) |
| `output_formats` | string[] | | `["cdb"]` | 取值 `cdb` / `msh` / `stl` |

**结果**:`{node_count, element_count, groups{capsule, powder}, degraded: bool, artifacts[], fidelity}`
(.cdb 为 MAPDL 可读命令流;MAT 1=powder、2=capsule)

---

## 3. 2D 轴对称 FEM 组(MAPDL)

三个方法共同点:`geometry` 与 `profile` **二选一**;`materials` null = TC4 + 20 钢;
`mesh`/`numerics` 见 §1.5。作业级错误(MAPDL 缺失/许可/不收敛/超时)经
`GET /jobs/{id}` 的 `error{code,message}` 返回,见 api-brief §6.2。

### 3.1 `axisym-thermal` — 升温段热瞬态 · **real** · 1–5 min · PLANE77

包套-粉末芯部温差滞后曲线,校核均匀温度假设。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` / `profile` | GeometryRef / AxisymProfile | 二选一 | `null` | 剖面来源 |
| `cycle` | Cycle | | `null` | **只取温度 ramp**,压力忽略 |
| `materials` | MaterialSelection | | `null` | |
| `mesh` / `numerics` | MeshSettings / Numerics | | §1.5 | |
| `probe_points` | `[r, z][]` | | `[[0, 0.5], [0, 0]]` | 探针**归一化坐标**(r∈[0,1] 映射内外半径,z∈[0,1] 映射高度);缺省 = 芯部 + 底面;**≤99 个**(APDL 写出宽度限制) |

**结果**:`{probes[{name, times_s[], temperatures_c[]}], max_lag_c, artifacts[], fidelity}`
(2 个探针命名 `core`/`surface`,其余 `probe_N`)

### 3.2 `axisym-mechanical` — 保温段力学 · **smoke** · 1–5 min · PLANE183

外压下包套/粉末应力分布与位移。阶段 1 冒烟 = 线弹性;阶段 2 换 Gurson 等温力学,API 不变。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` / `profile` | GeometryRef / AxisymProfile | 二选一 | `null` | |
| `hold_temperature_c` | number | | `900` | 保温温度(℃) |
| `hold_pressure_mpa` | number | | `120` | 保温压力(MPa) |
| `materials` | MaterialSelection | | `null` | |
| `mesh` | MeshSettings | | §1.5 | |

**结果**:`{displacement_max_mm, von_mises_max_mpa, section_stress[{radius_mm, sxx_mpa}], artifacts[], fidelity}`
(`section_stress` = 中截面径向应力剖面,11 个采样点)

### 3.3 `axisym-hip` — 2D 轴对称 HIP 全过程 · **smoke** · 2–10 min · PLANE183

真实几何剖面 + 线弹性占位本构,验证求解管线;阶段 2 换 Gurson+蠕变+接触,API 不变。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` / `profile` | GeometryRef / AxisymProfile | 二选一 | `null` | |
| `cycle` | Cycle | | `null` | 全曲线(温度+压力) |
| `materials` | MaterialSelection | | `null` | |
| `mesh` / `numerics` | MeshSettings / Numerics | | §1.5 | |
| `nlgeom` | boolean | | `false` | 大变形开关(冒烟阶段建议 false) |

**结果**:`{displacement_max_mm, von_mises_max_mpa, time_history{t[], disp[]}, artifacts[], fidelity}`

---

## 4. 3D 全模型 FEM 组(MAPDL)

### 4.1 `full3d-hip` — 3D 全模型 HIP · **smoke** · 0.5–4 h · SOLID45

真实 STEP 网格 + 线性占位本构 + 外表面均压;阶段 2 换 SOLID187+接触+NLGEOM,API 不变。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` | GeometryRef | ✓ | | `capsule_step` 必填;给 `cavity_step` 得双域网格 |
| `cycle` | Cycle | | `null` | 取峰值温度/压力作保温单工况 |
| `materials` | MaterialSelection | | `null` | |
| `mesh` | MeshSettings | | §1.5 | 3D 网格量大,建议 ≥8.0 起步 |
| `nlgeom` | boolean | | `false` | |

**结果**:`{displacement_max_mm, von_mises_max_mpa, deformed_stl: null, artifacts[], fidelity}`
(`deformed_stl` 阶段 1 恒为 null —— UPGEOM 变形网格导出留阶段 2)

---

## 5. 反演/优化组(不跑 MAPDL)

### 5.1 `calibrate` — 动力学参数标定 · real · 5–60 s

实验 D-t 数据 → Arrhenius 动力学参数(scipy least_squares)。阶段 2 升级 Gurson 反演,API 不变。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `experimental` | `{time_s, relative_density}[]` | ✓(≥3 点) | | 实验 D-t 数据 |
| `initial_relative_density` | number | | `0.65` | |
| `limiting_relative_density` | number | | `0.995` | |
| `cycle` | Cycle | | `null` | 实验对应曲线;**注意:无零件配置时主配置默认曲线会注入**(合并层显式行为) |
| `fit_params` | string[] | | `["k_ref", "q_j_per_mol"]` | 拟合哪些参数(其余固定);可选 `k_ref` / `q_j_per_mol` / `pressure_exponent` |

**结果**:`{fitted_params{}, covariance_or_bounds, residual_rms, fit_curve{times_s[], densities[]}, fidelity}`

### 5.2 `compensate` — 型腔预变形补偿 · real · 5–30 s · 需几何

目标型腔 STEP 按 `(Df/D0)^(1/3)` 放大,输出补偿后 STEP(供包套设计);阶段 2 换 FEM 迭代补偿。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `geometry` | GeometryRef | ✓ | | `cavity_step` 必填(目标型腔) |
| `initial_relative_density` | number | | `0.65` | |
| `final_relative_density` | number | | `0.97` | 或由 densification 结果取终态密度 |
| `scale_axis` | string | | `"uniform"` | `uniform` \| `per_axis` |

**结果**:`{scale_factors, output_step_path, artifacts[](补偿后 STEP), fidelity}`

### 5.3 `sensitivity` — 参数敏感性 · real · 5–60 s

对扫参字典逐参数(一次一个,OAT)批量跑 densification 核,输出各参数对终态密度的敏感度排序。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `base` | DensificationParams | | §1.6 默认 | 基准参数 |
| `sweep` | `{参数名: number[]}` | ✓(≥1 项) | | 例 `{"initial_relative_density": [0.60, 0.65, 0.70], "temperature_c": [880, 900, 920]}`;`temperature_c` / `hold_pressure_mpa` 覆盖曲线峰值 |

**结果**:`{base_final_density, one_at_a_time[{name, values[], final_densities[], sensitivity}], fidelity}`

---

## 6. 方法速查表

| 方法 | 保真度 | 典型耗时 | 需几何 | 必填字段 | 核心结果键 |
|---|---|---|---|---|---|
| `densification` | real | <1 s | — | — | `final_density`, `reached_097` |
| `process-window` | real | 5–30 s | — | — | `final_densities[][][]`, `window_ok` |
| `shrinkage-estimate` | real | <1 s | — | `characteristic_lengths_mm` | `items[]`, `linear_strain` |
| `material-query` | real | <1 s | — | — | `rows[]`, `source_notes` |
| `mesh` | real | 10–60 s | ✓ | `geometry.capsule_step` | `node_count`, `artifacts` |
| `axisym-thermal` | real | 1–5 min | 二选一 | — | `probes[]`, `max_lag_c` |
| `axisym-mechanical` | **smoke** | 1–5 min | 二选一 | — | `displacement_max_mm`, `section_stress` |
| `axisym-hip` | **smoke** | 2–10 min | 二选一 | — | `time_history`, `von_mises_max_mpa` |
| `full3d-hip` | **smoke** | 0.5–4 h | ✓ | `geometry.capsule_step` | `displacement_max_mm`, `deformed_stl` |
| `calibrate` | real | 5–60 s | — | `experimental`(≥3 点) | `fitted_params`, `residual_rms` |
| `compensate` | real | 5–30 s | ✓(cavity) | `geometry.cavity_step` | `scale_factors`, `artifacts` |
| `sensitivity` | real | 5–60 s | — | `sweep` | `one_at_a_time[]` |
