# passthrough 直通转发通道 — 上游(HIPForm)对接开发文档

版本 v1.1(2026-09-11)· 面向上游工作流作者(.inp 编写者)与引擎开发团队

> **本文档契约为规划口径,终稿以服务 `openapi()` 与实测响应为准。**
> 字段级权威文档始终是 Swagger(`/docs` / `/redoc`,pydantic 模型 description 即文档);
> 本文是唯一的"怎么对接"入口,覆盖定位、官方资料、HTTP 全流程、.inp 编写契约、
> 完整示例与上游 10-stage 工作流的逐项映射。
>
> 单位约定:**mm / MPa / s / ℃**。**passthrough 是唯一提交通道**:历史的类型化
> 方法 API(full3d-hip 等 12 个)已整体移除、不再兼容(上游确认完全跟随本服务);
> 类型化方法的历史文档已随方法库一并移除(需要时查 git 历史),对接以本文为准。

---

## 目录

1. [定位与边界](#1-定位与边界)
2. [官方链接与资料](#2-官方链接与资料)
3. [HTTP 交付全流程](#3-http-交付全流程)
4. [.inp 编写指南](#4-inp-编写指南)
5. [完整例子:示范工程](#5-完整例子示范工程)
6. [与上游 10-stage 工作流的对接映射](#6-与上游-10-stage-工作流的对接映射)
7. [安全前提](#7-安全前提)

---

## 1. 定位与边界

### 1.1 忠实转发器模型

passthrough 是**唯一提交通道**(`POST /sim/passthrough`,结果
`fidelity: "passthrough"`)。历史的类型化方法 API(full3d-hip 等 12 个端点)已
整体移除、不再兼容 —— 上游确认完全跟随本服务后,服务收敛为「**忠实 MAPDL 转发器**」:

**服务负责(运输层,全部保留)**

| 能力 | 说明 |
|---|---|
| 文件接收 | `POST /uploads/apdl` 收 .inp/.cdb/.mac/.csv/.txt,落到服务端存储 |
| 异步作业 | 提交 202 受理 → 单并发队列(`max_concurrent=1`)→ 状态机 |
| 超时与取消 | `timeout_s` 上限保护;`DELETE /jobs/{id}` 取消并清理(kill 进程组);`POST /jobs/{id}/cancel` 强制中断但保留现场 |
| 阶段进度 | 读 `progress.csv` 侧车,`GET /jobs/{id}` 的 `stages` 实时投影(§4.4) |
| 工件交付 | 声明输出 + `job.out` 自动发布进 `artifacts/`,流式下载 |
| 诊断阶梯 | 许可 → job.out 错误行 → 内部错误,统一错误码(§3.7) |

**服务不负责(仿真逻辑,全部归上游 .inp 作者)**

- 几何建模(体素自建或 `CDREAD` 引 .cdb)、网格、材料、单位制自洽;
- 载荷步/求解策略/收敛控制;
- 后处理与帧导出(坐标、位移、密度场……均由 .inp 内 `/POST1` 命令写出);
- 任何物理判断 —— `fidelity: "passthrough"` 的含义就是"**服务不背书物理内容**,
  结果的正确性由 .inp 作者负责"。

一句话:**ANSYS 的"workflow 机制文件"就是 MAPDL 批量输入文件 .inp 本身**;
本服务用官方机制(`mapdl -b -i entry.inp`,cwd=作业目录)执行它,零新增 ANSYS 侧概念。

### 1.2 类型化方法 API 已整体移除(单通道终态)

**终态口径**:上游(HIPForm)确认**完全跟随本服务** —— 原有类型化功能 API
(densification / axisym-hip / full3d-hip 等 12 个端点)**已整体删除、不再兼容**;
`POST /sim/passthrough` 是唯一的作业提交通道,服务收敛为**纯 MAPDL 转发器**。

为什么收敛(历史对照,仅帮助理解设计):

| | 类型化方法(已移除) | passthrough(唯一通道) |
|---|---|---|
| 仿真逻辑 | 服务持模板(method 即 workflow,params 即 inputs) | 上游持 .inp,服务零逻辑 |
| 参数 | 结构化 params(三级合并,Swagger 字段级) | `entry_file` + 文件清单 + 声明输出 |
| 适用 | 服务已封装好的标准计算 | 任意新物理/新工作流,改 .inp 即上线 |
| 结果保真度 | `real` / `smoke` | `passthrough`(服务不背书物理内容) |

上游《ansys 下游》文档的立场——"业务编排层不碰 MAPDL 命令"——在单通道下依然成立:
命令流归上游**工作流作者**(引擎之外的专职角色),业务编排层同样不碰。
历史方法文档已随方法库移除(查 git 历史);
历史网格资产(.cdb,按 MAT 1=powder/2=capsule 约定)仍可直接复用(§4.1)。

### 1.3 为何不直接暴露 RSM / PyMAPDL(以及它们的正交位置)

| 方案 | 形态 | 不直接暴露的原因 |
|---|---|---|
| **RSM**(Remote Solve Manager) | Workbench 生态的负载调度器 | 无第三方作业 REST 契约(面向 Workbench 客户端);作业单元是应用生成的求解包而非裸输入文件;随 ANSYS 版本耦合 |
| **PyMAPDL** | gRPC 客户端库(官方开源) | 是**库**不是 HTTP 作业服务:长持许可、进程生命周期归调用方,不适合跨团队"提交-轮询-取件"的松耦合模型;引入 gRPC 依赖即改部署面 |

**正交关系**:将来扩集群时,RSM/SLURM 可以作为本服务 `runner` 之下的执行后端
(runner 换执行器,对外 HTTP 契约不变)。届时上游对接代码零改动。

同构先例(安全与作业模型的参照):PyMAPDL 忠实转发、**默认无鉴权** —— 与本服务
"网络层收口"是同一个安全模型;Inductiva(已停运)的 提交输入文件 → 作业队列 →
工件下载 与本服务作业模型同形。

---

## 2. 官方链接与资料

**命令流即流程**:.inp 是 MAPDL 的官方批量输入格式(批处理模式执行一串命令,
含载荷步、求解、后处理与文件导出)。以下链接均已抽查可达(HTTP 200,2026-09-10):

### 2.1 ANSYS 官方(ansyshelp,`/public/` 前缀免登录直链;版本 v252 与部署一致)

| 主题 | 链接 |
|---|---|
| 批处理模式(`-b -i`,**对接核心**) | <https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/ans_ope/Hlp_G_OPE3_4.html>(Operations Guide §4.4 Batch Mode,主题 ID `Hlp_G_OPE3_4`) |
| 运行章总览(命令行/交互/批处理) | <https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/ans_ope/Hlp_G_OPE3.html>(Chapter 4: Running the Mechanical APDL Program,`Hlp_G_OPE3`) |
| Product Launcher 的 Input File 字段 | <https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/ans_ope/launcherhelp.html>(§4.2,"Input File" 即提交批执行的命令文件) |
| 把命令日志文件当输入(理解 .inp 从哪来) | <https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/ans_ope/Hlp_G_OPE8_4.html>(§8.3 Using a Command Log File as Input) |

> ansyshelp 直链失效或要求登录时,把 `/public/Views/Secured/…` 换成
> `https://ansyshelp.ansys.com/account/secured?returnurl=/Views/Secured/…`
> (免费 ANSYS 账号),或在站内搜索主题 ID(如 `Hlp_G_OPE3_4`)。

### 2.2 命令参考免登录镜像(mm.bme.hu,BME 大学维护的 v18.2 帮助镜像)

目录页(Mechanical APDL Command Reference):
<https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_CmdTOC.html>

查法:命令名去掉前导 `/` 或 `*` 后接 `Hlp_C_<NAME>.html`。示范工程用到、
且已逐页验证可达的命令页:

| 命令 | 页面 | 用途 |
|---|---|---|
| `/INPUT` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_INPUT.html> | 嵌套读入另一输入文件 |
| `CDREAD` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_CDREAD.html> | 读入 .cdb(实体/网格数据库) |
| `TIME` / `KBC` | [Hlp_C_TIME](https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_TIME.html) / [Hlp_C_KBC](https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_KBC.html) | 载荷步终点时间 / 斜坡-阶跃 |
| `OUTRES` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_OUTRES.html> | 结果文件写出控制 |
| `SET` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_SET.html> | /POST1 读载荷步结果 |
| `*GET` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_GET.html> | 取标量(极值/计数/节点号) |
| `*VGET` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_VGET.html> | 批量取节点坐标/位移进数组 |
| `*VWRITE` / `*CFOPEN` | [Hlp_C_VWRITE](https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_VWRITE.html) / [Hlp_C_CFOPEN](https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_CFOPEN.html) | 格式化写 csv(侧车与帧文件) |
| `SOLVE` | <https://www.mm.bme.hu/~gyebro/files/ans_help_v182/ans_cmd/Hlp_C_SOLVE.html> | 逐载荷步求解 |

> 镜像是 v18.2:经典命令(TIME/KBC/OUTRES/SET/*GET/*VWRITE 等)语义与 v252 一致;
> 新命令以 ansyshelp v252 为准。

### 2.3 实操参考(社区/机构)

- **PADT** — "10 Things Every ANSYS Mechanical APDL (MAPDL) User Should Know":
  <https://www.padtinc.com/2010/10/20/10-things-every-ansys-mechanical-apdl-mapdl-user-should-know/>
  (批处理/log/输入文件的工程实操习惯;`-b`/`-i` 命令行参数的实践语境)
- **HKHLR(HPC in Hessen)** — "How To run ANSYS Mechanical on an HPC-Cluster"(PDF):
  <https://www.hkhlr.de/sites/default/files/field_download_file/HKHLR-HowTo-Ansys_MAPDL.pdf>
  (Lichtenberg 集群上 mapdl 批处理脚本提交,作业数组/并行参数)
- **PyMAPDL**(官方开源,定位参照):<https://mapdl.docs.pyansys.com/>
- Workbench/Mechanical 生成 .inp:Mechanical 界面 File → Export → .inp,
  或求解前 Write Input File —— 生成的命令流可作手写 .inp 的起点(注意会带
  Workbench 化的命名与路径,需清理后再提交本服务)。

---

## 3. HTTP 交付全流程

端点一览(`/health` 等通用端点以服务 `openapi()` 为准;`/parts` 属历史零件
机制已移除;`/sim/methods` 仍在,返回单方法自描述清单):

| 端点 | 方法 | 用途 |
|---|---|---|
| `/uploads/apdl` | POST | multipart 上传 .inp/.cdb/.mac/.csv/.txt,返回服务端路径 |
| `/sim/passthrough` | POST | 提交 passthrough 作业(202) |
| `/jobs` | GET | 作业列表(队列 + 盘上历史,受理时间倒序;运维向) |
| `/jobs/{id}` | GET | 轮询状态 + `stages` 阶段进度(服务重启后盘上历史作业仍可查) |
| `/jobs/{id}/result` | GET | 结果 JSON(仅 succeeded) |
| `/jobs/{id}/log` | GET | 作业日志纯文本;`?source=job.log\|job.out` 选源,`?tail=N` 取尾;缺省全文但**超 2MB 自动截尾 2000 行**(响应带 `X-Log-Truncated: true`) |
| `/jobs/{id}/artifacts` | GET | 工件文件名数组 |
| `/jobs/{id}/artifacts/{name}` | GET | 流式下载单个工件 |
| `/jobs/{id}/cancel` | POST | 强制中断 pending/running 作业:终止进程组(SIGTERM→SIGKILL)并置 cancelled,**保留作业目录/job.out/日志供排障**;终态作业幂等返回当前状态 |
| `/jobs/{id}` | DELETE | 取消并清理(终止进程组;对历史终态作业为纯目录清理) |
| `/service/log` | GET | 服务请求日志尾部(运维向,`?tail=N`) |

运维面板:`GET /panel`(自托管单页:作业列表/详情/双日志/服务日志,
内网免鉴权);请求访问日志落盘 `var/logs/access.log`(按天轮转,默认保 14 天)。

### 3.1 第一步:上传文件(逐文件)

> 以下全部示例假设已设置 shell 变量 `HIP_KEY`(服务开启鉴权后必带):
> `HIP_KEY=$(openssl rand -hex 32)` 换成真实部署签发的 key,或导出
> `HIP_KEY` 为 `HIP_SERVICE_API_KEYS` 中任一值。


```bash
curl -s -H "X-API-Key: $HIP_KEY" -F 'file=@capsule_shrink.inp' http://localhost:8010/uploads/apdl
# → 200 {"path": "/var/uploads/Ab3x..._capsule_shrink.inp", "size_bytes": 4831}
```

- 扩展名白名单:`.inp .cdb .mac .csv .txt`;文件名消毒取 `Path.name`,落盘 `<token>_<原名>`;
- 本端点与提交端点**同受 passthrough 开关门控**(关闭时 403 `PASSTHROUGH_DISABLED`,
  不留旁路上传面);单文件上限 1 GiB(超限 413 `PAYLOAD_TOO_LARGE`,半写文件即清);
  上传文件按作业保留期清扫;
- 历史 `/uploads`(.step 几何通道)已随类型化方法一并移除 —— APDL 侧文件一律走本端点;
- **响应结构同既有上传**:`{path, size_bytes}`;`path` 是服务端绝对路径,
  后续填进 `entry_file` / `extra_files`;
- 附属 .cdb 同法逐个上传,把各自的 `path` 收集为 `extra_files`。

### 3.2 第二步:提交作业

```bash
curl -s -X POST http://localhost:8010/sim/passthrough \
  -H "X-API-Key: $HIP_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"params": {
        "entry_file": "/var/uploads/Ab3x..._capsule_shrink.inp",
        "extra_files": [],
        "declared_outputs": ["frame_1.csv","frame_2.csv","frame_3.csv","frame_4.csv","deform.csv"],
        "workflow": "HIP_DEMO_V1",
        "timeout_s": 1800}}'
# → 202 {"id": "9d2f...", "method": "passthrough", "status": "pending",
#         "fidelity": "passthrough", "status_url": "/jobs/9d2f..."}
```

参数字段(请求体固定为 `{"params": {...}}` 包装;历史 `part` 字段随类型化方法
一并移除,passthrough 不使用):

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `entry_file` | string | ✓ | 经 `/uploads/apdl` 取得的服务端路径;MAPDL 以 `-i` 执行它 |
| `extra_files` | string[] | 否(默认 `[]`) | 附属文件(如 .cdb)的服务端路径;按原名复制进作业目录根,供 `CDREAD` 相对引用 |
| `declared_outputs` | string[] | ✓(非空,≤64,自动去重) | 作业结束应产出的**裸文件名**清单;缺一即作业失败 `ARTIFACT_NOT_FOUND`(§4.3) |
| `timeout_s` | int | 否 | 取 `min(用户值, 全局上限)`(默认全局上限 14400 s);超时作业失败 `TIMEOUT` |
| `workflow` | string | 否 | 上游工作流名/版本(纯溯源:落 `resolved-params.json` 并回显,**服务不据此分支**;"Workflow 选择"需求 = method 名 + 该字段共同满足) |

**保留名校验**:`entry_file` / `extra_files` / `declared_outputs` 的**裸文件名**
不得撞作业目录保留名(否则 400 `INVALID_PARAMS`):
`state.json` / `resolved-params.json` / `result.json` / `job.log` / `job.out` /
`launcher.log` / `progress.csv` / `artifacts`。

### 3.3 第三步:轮询状态(含 stages)

```bash
curl -s -H "X-API-Key: $HIP_KEY" http://localhost:8010/jobs/9d2f...
```

```json
{
  "id": "9d2f...",
  "method": "passthrough",
  "part": null,
  "status": "running",
  "fidelity": "passthrough",
  "created_at": "2026-09-10T08:00:00+00:00",
  "started_at": "2026-09-10T08:00:01+00:00",
  "finished_at": null,
  "error": null,
  "log_url": "/jobs/9d2f.../log",
  "result_url": "/jobs/9d2f.../result",
  "artifacts_url": "/jobs/9d2f.../artifacts",
  "stages": [
    {"label": "MESH",    "time_s": 0.0},
    {"label": "SEG_P10", "time_s": 600.0},
    {"label": "SEG_P40", "time_s": 1200.0}
  ]
}
```

- `status` 状态机:`pending → running → succeeded / failed / cancelled`(契约不变);
- `stages` = **已完成阶段序列**,来自 .inp 写的 `progress.csv` 侧车(§4.4):
  pending → `null`;running → 实时快照;终态 → 末帧快照;不写侧车则恒为 `null`;
- `stages` 是只增字段 —— 不写侧车的既有调用方响应逐字段不变。

### 3.4 第四步:取结果与工件

```bash
# 结果 JSON(仅 succeeded;未完成 409 RESULT_NOT_READY,失败 409 JOB_FAILED)
curl -s -H "X-API-Key: $HIP_KEY" http://localhost:8010/jobs/9d2f.../result
# → {"fidelity": "passthrough",
#     "artifacts": ["capsule_shrink.inp","deform.csv","frame_1.csv","job.out",
#                   "progress.csv","results.csv"],
#     "returncode": 0, "elapsed_s": 42.7,
#     "values": {"ux_max": -1.8362E+00, "uy_max": 3.1154E-01,
#                 "shrink_r": 2.4107E-02, "p_final": 1.2000E+02}}

# 工件列表(裸文件名数组,与 result.artifacts 一致)
curl -s -H "X-API-Key: $HIP_KEY" http://localhost:8010/jobs/9d2f.../artifacts

# 单个工件流式下载(大文件边下边写,不整读内存)
curl -s -H "X-API-Key: $HIP_KEY" -O http://localhost:8010/jobs/9d2f.../artifacts/deform.csv

# 作业日志(纯文本):job.log=服务簿记事件,job.out=MAPDL 求解输出(运行中也可看)
curl -s -H "X-API-Key: $HIP_KEY" "http://localhost:8010/jobs/9d2f.../log?source=job.out&tail=50"
```

passthrough 结果 JSON 字段:

| 字段 | 说明 |
|---|---|
| `fidelity` | 固定 `"passthrough"`(服务不背书物理内容) |
| `artifacts` | 已发布工件裸文件名(= 入口 .inp + `job.out` 无条件 + 声明输出存在者 + `progress.csv` + `results.csv`(若写)) |
| `returncode` | MAPDL 进程退出码 |
| `elapsed_s` | 求解耗时(秒) |
| `values` | 可选:.inp 写的 `results.csv`(短标签→数值)被解析成 dict(§4.5);未写则无该字段 |

### 3.5 Python httpx 示例

```python
"""passthrough 最小对接(httpx):上传 → 提交 → 轮询 → 取件。"""
import time
import httpx

BASE = "http://localhost:8010"

with httpx.Client(timeout=60) as client:
    # 1) 上传入口 .inp(multipart;附属文件同法逐个上传)
    with open("capsule_shrink.inp", "rb") as stream:
        uploaded = client.post(f"{BASE}/uploads/apdl",
                               files={"file": ("capsule_shrink.inp", stream)}).json()
    # 2) 提交(202;立即持久化 job_id,POST 不重试)
    accepted = client.post(f"{BASE}/sim/passthrough", json={"params": {
        "entry_file": uploaded["path"],
        "declared_outputs": ["frame_1.csv", "deform.csv"],
        "workflow": "HIP_DEMO_V1",
    }})
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["id"]

    # 3) 轮询(status + stages)
    while True:
        state = client.get(f"{BASE}/jobs/{job_id}").json()
        print(state["status"], state.get("stages"))
        if state["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(3)

    # 4) 取结果与工件(流式下载)
    if state["status"] == "succeeded":
        print(client.get(f"{BASE}/jobs/{job_id}/result").json())
        for name in client.get(f"{BASE}/jobs/{job_id}/artifacts").json():
            with client.stream("GET", f"{BASE}/jobs/{job_id}/artifacts/{name}") as resp:
                with open(name, "wb") as out:
                    for chunk in resp.iter_bytes():
                        out.write(chunk)
    else:
        raise SystemExit(f"作业失败: {state['error']}")
```

(完整可运行版见 `docs/examples/passthrough-demo/submit_demo.py`。)

### 3.6 HIPForm `AnsysClient` 方法对应表

类型化通道移除后,HIPForm 侧 `src/hipform/ansys_client.py` 的适配口径:
**作业侧五个方法形状不变、继续可用;提交侧两个方法需适配改走 passthrough**。

| AnsysClient 方法 | HTTP | 单通道终态下的适配 |
|---|---|---|
| `upload_step` | POST `/uploads` | **随类型化方法一并移除**(.step 几何通道不再存在);替代 = 新增 `upload_apdl`:同一 multipart 请求形态,POST `/uploads/apdl`,去掉 STEP 内容校验(~10 行) |
| `submit(method, params)` | POST `/sim/{method}` | **改走唯一通道**:提交固定为 `POST /sim/passthrough`(body `{"params": {entry_file, declared_outputs, …}}`);202 + `{id, method, status_url}` 响应形状不变,现有校验可沿用 |
| `get_job` | GET `/jobs/{id}` | **形状不变,继续可用**:status 枚举不含新值;`stages` 是新增只增字段,`get_job` 不做白名单校验,自然透传 |
| `get_result` / `get_log` | GET `…/result` / `…/log` | **形状不变,继续可用** |
| `list_artifacts` / `download_artifact` | GET `…/artifacts[/{name}]` | **形状不变,继续可用**(下载本就是流式) |

### 3.7 全量错误码表

统一错误体 `{"code": "...", "message": "..."}`;**调用方按 code 匹配,勿匹配 message 文本**。

请求级(HTTP 响应体直接返回):

| code | 场景 | HTTP |
|---|---|---|
| `PASSTHROUGH_DISABLED` | **新增**。passthrough 开关关闭时提交(`POST /sim/passthrough` 或泛化 `POST /sim/{method}`)或上传(`POST /uploads/apdl`,与提交同受门控);选 403 便于区分"端点不存在"与"被策略关闭" | 403 |
| `INVALID_PARAMS` | 参数校验失败:字段类型/缺失、`declared_outputs` 空/超 64、文件名含路径分隔符或不可打印字符、撞保留名(§3.2) | 400 |
| `PART_NOT_FOUND` | 历史零件配置引用(`part` 字段已随类型化方法移除;passthrough 不使用) | 404 |
| `GEOMETRY_NOT_FOUND` | geometry STEP 路径不存在或不可读(历史类型化方法;passthrough 不涉及) | 404 |
| `METHOD_NOT_FOUND` | 泛化 `/sim/{method}` 未知方法名(单通道下即非 `passthrough` 的提交路径) | 404 |
| `METHOD_DISABLED` | 方法被 config `methods.disabled` 显式停用 | 503 |
| `METHOD_NOT_IMPLEMENTED` | 方法在注册表但内核未实现 | 501 |
| `JOB_NOT_FOUND` | 作业 id 不存在(已删除或超出保留期被清扫;服务重启后盘上历史作业仍可经 `/jobs` 与 `/jobs/{id}` 查询,不会因此报此错) | 404 |
| `ARTIFACT_NOT_FOUND` | `artifacts/{name}` 文件名不在该作业工件列表 | 404 |
| `RESULT_NOT_READY` | result 端点对 pending/running/cancelled 作业 | 409 |
| `JOB_FAILED` | result 端点对 failed 作业(body 带原 error 的 code/message) | 409 |

作业级(`GET /jobs/{id}` 的 `error{code,message}`;经 result 端点以 409 JOB_FAILED 透传):

| code | 场景 |
|---|---|
| `MAPDL_NOT_FOUND` | ansys 可执行缺失(/health 可预检 `mapdl_found`) |
| `LICENSE_UNAVAILABLE` | 许可不可用/签出超时 |
| `CONVERGENCE_FAILED` | MAPDL 非零退出或 job.out 出 ERROR 行(message 附关键行) |
| `TIMEOUT` | 超过 `timeout_s` 上限被终止 |
| `ARTIFACT_NOT_FOUND` | **passthrough 作业级新语义**:声明的输出在作业结束时缺失(§4.3) |
| `INTERNAL` | 未分类内核异常(含服务重启遗留作业改标) |

---

## 4. .inp 编写指南

### 4.0 执行环境事实(写文件前必须知道)

- MAPDL 以 **批处理模式**执行:`mapdl -b -i <entry 文件原名>`,作业名固定
  `-j hipjob`;**进程工作目录 = 作业目录**(`var/jobs/<id>/`)——你写的所有
  相对路径文件都落在作业目录根;
- 入口 .inp 与 `extra_files` 被**按原名复制**进作业目录根,所以 .inp 内引用
  附属文件**一律裸文件名相对引用**:

```apdl
CDREAD,DB,,capsule_mesh,cdb    ! 读同目录 capsule_mesh.cdb(经 extra_files 带入)
```

- 作业目录根的簿记文件(`state.json`/`resolved-params.json`/`result.json`/
  `job.log`/`job.out`/`launcher.log`/`progress.csv`/`artifacts/`)**不得覆盖**
  (提交时已按保留名拦截);
- `.inp` 写出的文件落在作业目录根,服务把**已发布**的复制进 `artifacts/` 供下载;
  未声明也未自动处理的文件留在根目录,不可下载(避免误发布中间大文件);
- 单并发(`max_concurrent=1`):所有作业共享单许可串行执行。

**失控输出与退出码(实测教训,写大模型前必读)**:

- `/NERR` 的 **NMABT 默认 10000**:累计警告+错误超万条即
  `The number of ERROR and WARNING messages exceeds 10000 ... The ANSYS run is
  terminated by this error`,且海量输出会把 job.out 撑到数百 MB。错误源应尽早修正;
  确需放宽显示上限时在 .inp 头部加 `/NERR,,99999999`(上限 99,999,999);
- 批处理对错误敏感:首错即进入终止路径(`/INPUT` 中遇错即止可用 `IFKEY=1` 控制);
- 官方退出码(Mechanical APDL Operations Guide §4.1 Table 4.1):
  `0`=正常退出,`1`=指示错误(含崩溃信号),`5`=命令行参数错误,`7`=许可失败,
  `8`=运行结束异常。作业失败时服务在错误消息中附官方语义;
- 中断逃生门:`POST /jobs/{id}/cancel` 强制中断(终止进程组,**保留作业目录/日志
  供排障**);超时上限内未完成则 TIMEOUT 自动终止。

### 4.1 单位制与 MAT 编号:责任归上游

服务对 passthrough **不做任何单位换算与材料注入**:

- 单位制自洽是 .inp 作者的责任(建议沿用本仓库约定 **mm / MPa / s / ℃**,
  热参数换算 KXX=mW/(mm·K)、C=mJ/(tonne·K)、DENS=tonne/mm³,与仓库历史惯例
  一致,便于对照);
- **MAT 编号全局约定(强烈建议遵守)**:`1 = powder`、`2 = capsule` ——
  这是仓库历史类型化方法遗留的一致约定,既有网格资产(.cdb)均按此编号;
  上游自编 .inp 沿用同一约定,历史 .cdb 工件即可直接 `CDREAD` 复用;
- `results.csv`/`progress.csv` 的短标签不带单位,单位语义由上游工作流层补。

### 4.2 帧导出(动画数据,Channel C)

每段载荷步结束后在 `/POST1` 用 `*VGET` 取节点坐标+位移,写一个文本帧文件
(一帧一文件;`*CFOPEN` 无可靠 APPEND,见 §4.4):

```apdl
/POST1
SET,LAST
*VGET,XG(1),NODE,1,LOC,X          ! 原始坐标(未 UPGEOM 即未变形坐标)
*VGET,YG(1),NODE,1,LOC,Y
*VGET,UXA(1),NODE,1,U,X           ! 位移
*VGET,UYA(1),NODE,1,U,Y
*CFOPEN,frame_%I%,csv             ! %I% 参数代入 → frame_1.csv ...
*VWRITE,'node','x_mm','y_mm','ux_mm','uy_mm'
(A4,',',A4,',',A4,',',A5,',',A5)
*VWRITE,NLIST(1),XG(1),YG(1),UXA(1),UYA(1)
(E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8)
*CFCLOSE
```

前端渲染动画 = 逐帧读 csv 数据 + 客户端插值(数据帧,不传视频/图片)。
**消费侧怎么把帧变成 3D 回放(数据语义/渲染配方/各栈生态选项/实测坑)见
`playback-handbook.md`;cube 型 three.js 参考实现见
`examples/passthrough-demo/playback/`。**
**坑:`*VWRITE` 字符字面量标签 ≤8 字符**,超长被 MAPDL 静默截断
(写读两侧撞键、数据错位)。逐标签自查,别依赖"看起来写进去了"。

### 4.3 declared_outputs 声明规则

- 裸文件名(**不含路径**),如 `frame_1.csv`;数量 1–64,自动去重;
- **不得与入口/附属文件同名**(输入会被复制进作业目录根部,同名会让缺件检查
  被"自我满足"—— MAPDL 零产出也算成功;提交时 400 `INVALID_PARAMS` 拒绝);
- 作业成功结束时**逐个存在性检查:缺一即作业 failed,`error.code = ARTIFACT_NOT_FOUND`**
  (这是"服务无逻辑"的关键契约 —— 服务不懂你的物理输出,但保证"说好交付的一定在");
  认定口径 = **作业目录根部**写出(推荐,服务负责发布进 `artifacts/`)**或** .inp
  直写 `artifacts/` 子目录(兼容既有模板惯例,文件本就可下载;注意直写可能与
  服务无条件发布的 `job.out`/入口 .inp 同名互覆,别起这些名字);
- 拼写即契约:`*CFOPEN,frame_1,csv` 写出的是 `frame_1.csv`,声明写成
  `frame1.csv` 或 `Frame_1.CSV` 都会触发失败(对大小写敏感,按写出侧原样声明);
- **不必声明**(服务自动处理):入口 .inp 本身、`job.out`(无条件发布,
  支撑错误分析)、`progress.csv`(保留名,侧车专用)、`results.csv`(存在即
  自动解析进 `values` 并发布 —— 别声明,声明后若没写反而触发 ARTIFACT_NOT_FOUND);
- 大文件(rst、变形 .cdb、大 csv)列入 declared_outputs 即可,下载是流式的。

### 4.4 progress.csv 侧车契约(阶段进度)

`.inp` 约定在作业目录根维护 `progress.csv`:每行 = **阶段短标签(≤8 字符) + 累计耗时秒**。

因 `*CFOPEN` 无可靠 APPEND(既有领域约束),采用**整文件重写式**:
`*DIM` 数组自维护已完成阶段,每完成一阶段重开文件全量重写:

```apdl
! ---- 初始化(网格后):阶段表 + 首行 ----
NSTG = NSEG + 1
*DIM,PGLAB,CHAR,NSTG              ! 阶段标签(≤8 字符)
*DIM,PGTIM,ARRAY,NSTG             ! 累计秒(本例=载荷步分析时间)
PGLAB(1) = 'MESH'
PGTIM(1) = 0
*CFOPEN,progress,csv
JL = PGLAB(1)
JTV = PGTIM(1)
*VWRITE,JL,JTV
(A8,',',E16.8)
*CFCLOSE

! ---- 每完成第 I 段(后处理段内):登记 + 全量重写 ----
PGLAB(I+1) = SEGLAB(I)            ! 如 'SEG_P10'
PGTIM(I+1) = TSEGA(I)             ! 该段载荷步末累计时间
*CFOPEN,progress,csv
*DO,J,1,I+1
  JL = PGLAB(J)
  JTV = PGTIM(J)
  *VWRITE,JL,JTV
(A8,',',E16.8)
*ENDDO
*CFCLOSE
```

读侧契约(服务):GET /jobs/{id} 时读时投影,容忍半行撕裂(坏行跳过并记日志),
文件不存在 → `stages: null`。**时间列语义由作者保证**:载荷步累计分析时间是最
稳妥的选择(壁钟时间在 MAPDL 侧无可靠统一口径,若用 `*GET,par,ACTIVE,,TIME`
取壁钟需自行核实);标签 ≤8 字符同样适用截断坑。

### 4.5 results.csv 可选结构化结果契约

`.inp` 末尾可选地在作业目录根写 `results.csv`:行 = `短标签(≤8 字符),数值`,
格式与仓库历史 `summary.csv` 惯例一致(解析侧沿用):

```apdl
*CFOPEN,results,csv
*VWRITE,'ux_max',UXMAX
(A8,',',E16.8)
*VWRITE,'shrink_r',SHRR
(A8,',',E16.8)
*CFCLOSE
```

服务把它解析进结果的 `values: {短标签: 数值}`(纯 key-value 搬运,**不做物理
解读**);长键名(如 `final_density`)由上游引擎侧映射短标签。缺席即无 `values`
字段。撕裂容忍同 progress.csv。

### 4.6 变形几何数据导出(支撑 final_powder.step 重构)

末段 `/POST1` 后写出 全节点 坐标 + 位移(+可算变形后坐标),供上游重构
终态粉末几何:

```apdl
*VGET,XG(1),NODE,1,LOC,X        ! 初始坐标
*VGET,UXA(1),NODE,1,U,X         ! 位移
*VOPER,XD(1),XG(1),ADD,UXA(1)   ! 变形后 = 初始 + 位移
*CFOPEN,deform,csv
*VWRITE,'node','x0_mm','y0_mm','ux_mm','uy_mm','xd_mm','yd_mm'
(A4,',',A5,',',A5,',',A5,',',A5,',',A5,',',A5)
*VWRITE,NLIST(1),XG(1),YG(1),UXA(1),UYA(1),XD(1),YD(1)
(E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8,',',E16.8)
*CFCLOSE
```

final_powder.step 的几何重构(CAD 重建、STL 包络)是上游职责,本层只供数据。
3D 场景(含 rst 全场)同理:把所需场量逐段/逐节点写成 csv 列入 declared_outputs。

### 4.7 编写检查单(提交前自查)

- [ ] 相对引用只用裸文件名(附属文件已在作业目录根);
- [ ] 不写保留名文件(§3.2 列表);
- [ ] 每个 `*VWRITE` 字符字面量 ≤8 字符(含表头、标签、探针名);
- [ ] `declared_outputs` 与 `*CFOPEN` 实际写出的文件名逐字一致;
- [ ] progress.csv 是整文件重写式、行=标签+累计秒;results.csv 是 标签+数值;
- [ ] 单位自洽、MAT 1=powder/2=capsule(若混用本服务生态);
- [ ] `-j` 作业名是服务指定的,不要在 .inp 里 `/FILNAME` 改名。

---

## 5. 完整例子:示范工程

`docs/examples/passthrough-demo/` 是**自包含**的轴对称包套缩放演示
(不依赖任何外部几何文件,几何用 `RECTNG` 体素自建),五份文件:

| 文件 | 角色 |
|---|---|
| `capsule_shrink.inp` | 入口命令流:PLANE183 轴对称;粉末/包套两区 `RECTNG+AGLUE+AATT`(MAT 1=powder、2=capsule);**线弹性占位本构(演示通道,不背书 HIP 物理)**;`NLGEOM,1`;4 段升压载荷步循环;每段后写一帧 `frame_N.csv` + 重写 `progress.csv`;末尾写 `deform.csv`(变形几何数据)与 `results.csv`(短标签标量) |
| `vm1_axial_bar.inp` | **官方 Verification Manual VM1 已知答案 E2E 夹具**(数值级通道验证,真 MAPDL v252 实测 13 项断言全过):LINK180 两端固定直杆,官方目标反力 900/600 lb 与解析位移 -8.0e-5/-9.0e-5 in 写进 `results.csv`(`r1_lb`/`r2_lb`/`ratio12`/`u2_in`/`u3_in`);单位故意保留官方原制 in/lbf/psi,验证服务不预设单位制;断言表见目录 `README.md` |
| `vm3_thermal_support.inp` | **官方 Verification Manual VM3 已知答案 E2E 夹具**(热-结构耦合,真 MAPDL v252 实测 12 项断言全过):铜/钢三杆并联(LINK180),ΔT=+10°F + 4000 lb,官方目标热应力 19695/10152 psi 写进 `results.csv`(`st_strs`/`cu_strs`/`ratio_st`/`ratio_cu`);单位保留官方原制 in/lbf/psi/°F;断言表见目录 `README.md` |
| `submit_demo.py` | httpx 提交脚本:上传 → 提交(`workflow="HIP_DEMO_V1"`)→ 轮询打印 status+stages → 下载工件;每步附等价 curl 注释 |
| `README.md` | 一分钟跑通说明(含服务端开开关的方法) |

端到端时序(假设服务已在 :8010 且 `passthrough.enabled=true`):

```
客户端                                服务                         MAPDL 子进程
  │ POST /uploads/apdl (inp)            │
  │←─ 200 {path}                        │
  │ POST /sim/passthrough               │ 创建 var/jobs/<id>/,
  │  {entry_file, declared_outputs,     │ 复制 entry+extra 进根,入队
  │   workflow:"HIP_DEMO_V1"}           │
  │←─ 202 {id, method, status_url}      │
  │ GET /jobs/<id>  (轮询,3 s)          │── 取许可,启动: mapdl -b -i capsule_shrink.inp
  │←─ {status:"running", stages:[MESH]} │        (cwd=作业目录根)
  │ GET /jobs/<id>                      │   PREP7→SOLVE×4,每段后:
  │←─ {stages:[MESH,SEG_P10]}           │     frame_N.csv + progress.csv 重写
  │ ...                                 │   末段后: deform.csv + results.csv
  │←─ {status:"succeeded", stages:[…5]} │── 进程退出(rc=0),核对 declared_outputs
  │ GET /jobs/<id>/result               │   发布工件 → artifacts/
  │←─ {fidelity:"passthrough", values…} │
  │ GET /jobs/<id>/artifacts/deform.csv │
  │←─ (流式文件)                        │
```

逐文件讲解、跑通命令与预期产物见该目录 `README.md`;`.inp` 内每段都有中文注释,
可直接当模板改(改段数时记得同步提交侧 `declared_outputs` 的帧文件名)。

---

## 6. 与上游 10-stage 工作流的对接映射

> 本章把本服务放进 HIPForm 规划的 10-stage HIP Simulation Workflow 引擎
> (WF-00 校验 → WF-10 报告,建于上游侧)的坐标系。阶段编号以上游引擎文档为准,
> 本章口径:**本服务 = WF-06 求解层 + 通用运输层**(传输级校验/作业/进度/工件)。

### 6.1 WF 阶段 ↔ 本服务端点映射

| Stage(上游) | 职责层 | 本服务落点 |
|---|---|---|
| WF-00 输入校验 | 上游引擎 | —(引擎校验 package.step/process.json/material.json;本服务另做传输级校验,见 §6.2 分层) |
| WF-01 CAD precheck | 上游(cad_workflow 既有资产) | — |
| WF-02 body 识别(包套/粉末/抽气管) | 上游 | — |
| WF-03 网格划分 | 上游(自建管线产出 .cdb) | —(.cdb 经 `extra_files` 带入;历史 mesh 方法已随类型化通道移除) |
| WF-04–05 求解准备(.inp 生成/材料映射) | 上游 .inp 生成器 | `POST /uploads/apdl` 收产物 |
| **WF-06 求解** | **本服务** | **`POST /sim/passthrough` → `GET /jobs/{id}`(stages)→ result/log/artifacts** |
| WF-07 变形几何重构(final_powder.step) | 上游 | 数据源 = WF-06 的 deform.csv(§4.6)/declared 场文件 |
| WF-08 STEP diff(目标对比) | 上游(comparison 资产) | — |
| WF-09 验收判定(PASS/FAIL) | 上游 | —(本服务不判物理合格性) |
| WF-10 报告 | 上游(report 资产) | —(HTML report 属上游;本服务供 result/log/工件) |

WF-06 栈选型 = **经典 MAPDL .inp**(上游已评估 Sintering Wizard 不覆盖 HIP 物理
需手搭,与经典 MAPDL 命令集等价;Sintering Add-on/ExportAnimation/HTML report
属 Mechanical/Workbench 层,不在本通道路线)。

### 6.2 错误码分层(两层不必同形)

| 层 | 错误码族 | 职责 |
|---|---|---|
| 上游引擎 | `GEO_*` / `MESH_*` / `SOLVER_*` … | **语义分类**:哪个环节、什么工程原因失败 |
| 本服务 | `MAPDL_NOT_FOUND` / `LICENSE_UNAVAILABLE` / `CONVERGENCE_FAILED` / `TIMEOUT` / `ARTIFACT_NOT_FOUND` / `INTERNAL` + 请求级码 | **通用运输诊断**:环境/许可/进程/超时/交付缺失;`job.out` 工件无条件提供,供上游二次归因 |

映射建议:服务 `CONVERGENCE_FAILED`/`TIMEOUT` → 引擎 `SOLVER_*`;
`ARTIFACT_NOT_FOUND`(作业级)→ 引擎侧归类"输出契约破裂";其余按环境/内部类
归并。引擎不必透传服务码给最终用户,但应把 code+message+job.out 留痕。

### 6.3 输入契约翻译(引擎层 → 传输层)

两层输入契约不必同形,由上游 .inp 生成器翻译:

| 引擎层输入 | 传输层(passthrough) |
|---|---|
| `package.step`(装配几何) | STEP→gmsh→.cdb(上游管线,产物经 `extra_files` 带入,.inp 内 `CDREAD` 引用);规则几何可省,直接 .inp 内 `RECTNG`/`CYLIND`+`AMESH` 自建 |
| `process.json`(温度/压力/时间曲线) | .inp 内的 `TIME`/`SFL`/`SOLVE` 分段载荷步序列 |
| `material.json`(材料参数) | .inp 内 `MP`/`TB` 定义(MAT 1=powder、2=capsule 约定) |
| (引擎的动画/重构需求) | 帧文件 + deform.csv 列入 `declared_outputs` |

### 6.4 上游能力清单 13 项逐条对照

| # | 上游能力 | 本服务落点 | 责任归属 |
|---|---|---|---|
| 1 | Job 创建 | `POST /sim/passthrough` → 202 `{id,method,status_url}` | 本服务 |
| 2 | 文件上传 | `POST /uploads/apdl`(multipart,白名单 .inp/.cdb/.mac/.csv/.txt) | 本服务 |
| 3 | Workflow 选择 | method 名固定 `passthrough` + `params.workflow`(溯源标签,服务不据此分支) | 共同(端点归调用方,语义归上游) |
| 4 | 参数输入 | `entry_file`/`extra_files`/`declared_outputs`/`timeout_s`(唯一提交形态) | 共同(契约由服务定,内容归上游) |
| 5 | Job 启动 | 202 受理即入队,单许可串行执行 | 本服务 |
| 6 | 状态查询 | `GET /jobs/{id}`(status 状态机 + `stages` 阶段序列) | 本服务 |
| 7 | 日志 | `GET /jobs/{id}/log`(?tail=N)+ `job.out` 工件 | 本服务 |
| 8 | 结果返回 | `GET /jobs/{id}/result`(fidelity/artifacts/returncode/elapsed_s/values) | 本服务运输,内容归上游 .inp |
| 9 | 报告 | — | 上游(report 资产;数据源=8) |
| 10 | 动画 | 帧文件(Channel C:数据帧+前端渲染) | 上游渲染;本服务运输帧文件 |
| 11 | 最终几何(final_powder.step) | —(供 deform.csv 变形坐标数据) | 上游重构;本服务供数据 |
| 12 | 错误信息 | `error{code,message}` + job.out(§6.2 分层) | 共同 |
| 13 | 文件下载 | `GET /jobs/{id}/artifacts/{name}` 流式 | 本服务 |

### 6.5 状态枚举命名映射(服务枚举不动)

| 上游(引擎) | 本服务(`status`) | 备注 |
|---|---|---|
| `QUEUED` | `pending` | 已受理未开跑(排队) |
| `RUNNING` | `running` | 执行中(看 `stages`) |
| `SUCCESS` | `succeeded` | 终态,可取 result |
| `FAILED` | `failed` | 终态,`error{code,message}` |
| `CANCELLED` | `cancelled` | 终态(`DELETE /jobs/{id}` 触发) |

引擎侧在适配层做一次枚举翻译即可;服务枚举是冻结契约不改。

### 6.6 提交形态映射

上游文档示例 `POST /api/v1/jobs {workflow, inputs, files}` ↔ 本服务两段式:

| 上游字段 | 本服务等价 |
|---|---|
| `files` | 先逐文件 `POST /uploads/apdl`(multipart)拿服务端路径 |
| `workflow` | `POST /sim/passthrough`(唯一通道)+ `params.workflow` 溯源标签 |
| `inputs` | `entry_file`/`extra_files`/`declared_outputs`/`timeout_s` |

即:**上传与提交分离、作业与轮询端点统一**,无单请求混传二进制的形态。
(历史上的「method=workflow」映射 —— 类型化 `POST /sim/{name}` 每方法一个端点 ——
已随类型化通道删除,仅作历史对照;单通道下 workflow 语义由 `params.workflow` 承载。)

### 6.7 stage/progress 口径

- `stages` = **已完成阶段序列**(标签+该阶段完成时的累计秒);
- **当前阶段与百分比由引擎/前端按序列派生**(如已知总段数,`len(stages)/总段数`
  即进度),服务**不猜百分比**;
- 时间列语义 = .inp 作者写入的累计口径(推荐载荷步分析时间,§4.4)。

### 6.8 optiSLang 立场

上游引擎内部若采用 optiSLang Web Service 做参数研究/优化,属其**内部分层**
(引擎之下的求解调度),与本 API 不冲突 —— 即上游文档的方案 A:下游自建
Adapter、只约定 API。本服务同样可作 optiSLang 节点里的求解执行器(每次迭代
= 一次 passthrough 提交)。

### 6.9 大文件说明

rst 全场文件、变形 .cdb、大 csv 都可列入 `declared_outputs`(≤64 个);
下载走 `GET /jobs/{id}/artifacts/{name}` 流式响应,客户端应流式落盘
(`AnsysClient.download_artifact` 已是此形态)。注意作业目录有保留期清扫
(默认 3 天),重要结论及时取走。

---

## 7. 安全前提

passthrough = **部署面上的任意 APDL 执行**(`/SYS` 可执行系统命令、任意路径写)。
服务已启用 **API Key 应用层鉴权**(见下节),但鉴权不缩小执行面本身:
拿到 key 的调用方仍可提交任意 APDL,网络层收口原则不变。因此:

- 开关 `config/service.yaml` 的 `passthrough.enabled`,**默认 `false`**
  (环境变量 `HIP_SERVICE_PASSTHROUGH_ENABLED` 可覆盖);
- **仅在纯内网开启**(服务与上游同内网/同机);`GET /health` 的
  `passthrough_enabled` 字段可观测当前开关;
- **公网隧道期间保持关闭**:当前若有临时公网暴露(如 frp 隧道),
  passthrough 必须关(提交会得到 403 `PASSTHROUGH_DISABLED`,这是设计行为);
- 开关关闭时,上传端点 `/uploads/apdl` 与提交端点一并拒绝;
- 开启时服务启动日志会显著标注(warning 级)"任意 APDL 执行面已开启";
- passthrough 是唯一提交通道:开关关闭即整个服务不可提交仿真作业(403),
  不存在"其他方法仍可用"的旁路;历史类型化方法执行的是服务持有模板,
  已随单通道收敛整体移除。

### 7.1 API Key 鉴权(全端点)

除 `GET /health` 与 `OPTIONS` 请求(CORS 预检不带自定义头;含 CORS 未挂载时的
裸 OPTIONS,穿过鉴权即 405)外,**全部端点要求 `X-API-Key` 请求头**
(含 `/docs`、`/openapi.json`、`/uploads/*`、`/sim/*`、`/jobs/*`、`/service/*`;
`/panel` 静态壳见下方豁免面):
缺失或错误一律 `401`,响应体为统一错误信封 `{"code": "UNAUTHORIZED", "message": "..."}`,
三种情形(服务未配置 key / 请求缺头 / key 无效)message 各不相同,便于排障。

- **fail-closed**:`auth.api_keys` 为空 = 除豁免外全部 401(不是关闭鉴权)——
  忘配置的部署不会裸奔,启动日志有显著 warning;
- 配置:`config/service.yaml` 的 `auth.api_keys`(列表,多 key 并存支持无痕轮换)
  或环境变量 `HIP_SERVICE_API_KEYS`(逗号分隔);**yaml 已入 git,真实 key 走环境变量**,
  例:`HIP_SERVICE_API_KEYS=$(openssl rand -hex 32)`;
- 豁免面:`/health`、根跳转 `/` 与 `/panel` 静态壳(无敏感数据);面板 JS 内嵌
  key 输入——首次 401 弹窗输入一次,存 sessionStorage(关标签页即清),全部
  数据请求(含取消作业)自动携带;`/docs` 在浏览器直接打开仍 401,需 curl 或
  浏览器头注入插件;跨域回放组件须配置 CORS `allow_headers`(服务端已放行
  `X-API-Key`)并自行携带 key。

```bash
# 全部业务请求带 key(示例:上传 + 提交)
curl -s -H 'X-API-Key: <key>' -F 'file=@capsule_shrink.inp' http://localhost:8010/uploads/apdl
curl -s -X POST -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"params": {...}}' http://localhost:8010/sim/passthrough
```

---

## 附:常见问题

- **提交 403 PASSTHROUGH_DISABLED?** 开关没开(§7);确认部署环境允许开启再开。
- **作业 failed,`error.code=ARTIFACT_NOT_FOUND`?** 声明的输出没在作业目录根
  找到:核对 `*CFOPEN` 实际文件名与 `declared_outputs` 逐字一致(大小写、
  下划线、扩展名);下载 `job.out` 工件看 MAPDL 侧有没有报错。
- **`stages` 一直是 null?** .inp 没写 progress.csv 侧车(§4.4),或还没跑到
  第一次重写;先确认 GET /jobs/{id} 是 running。
- **帧文件名怎么带序号?** `*CFOPEN,frame_%I%,csv` 用参数代入(§4.2);
  段数改了记得同步 declared_outputs。
- **想复用历史网格资产?** 类型化方法时期产出的 .cdb 可直接作为 extra_files
  上传,.inp 里 `CDREAD` 引用(MAT 1/2 约定已对齐);新网格由上游管线生成。
