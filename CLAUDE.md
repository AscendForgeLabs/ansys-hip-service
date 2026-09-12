# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

HIP 仿真**纯 MAPDL 转发器** — 面向 HIPForm 的 ANSYS/MAPDL 计算运输服务(FastAPI,独立进程部署)。`POST /sim/passthrough` 是**唯一**作业提交通道:上游上传 APDL 输入(.inp 及附属文件),本服务忠实执行、不负责任何仿真逻辑(结果 `fidelity: "passthrough"`,服务不背书物理内容);异步作业模式:提交(202)→ 轮询 `GET /jobs/{id}`(含 stages 阶段进度)→ 取结果/工件。仓库注释、文档、提交信息全部用中文。

上游对接契约的唯一入口是 `docs/passthrough-guide.md`(`POST /uploads/apdl` 上传、`declared_outputs` 声明输出、`progress.csv` 阶段侧车、`results.csv` 结构化结果;示范工程在 `docs/examples/passthrough-demo/`);消费侧三维回放手册在 `docs/playback-handbook.md`(帧数据语义/渲染配方/生态选项,含 `playback/` 参考实现)。

**安全门槛**:passthrough = 任意 APDL 执行面(可读写文件、起系统命令),开关默认 `false`(`config/service.yaml`,环境变量 `HIP_SERVICE_PASSTHROUGH_ENABLED` 可覆盖),仅纯内网允许开启,公网隧道期间必须关闭(关闭时提交得 403 `PASSTHROUGH_DISABLED`)。

## 常用命令

```bash
uv sync --extra dev                                 # 安装依赖+测试工具(uv.lock;或已有 .venv)
.venv/bin/python -m pytest -q                       # 全量测试(bare python 不在 PATH,须用 venv 或 uv run)
.venv/bin/python -m pytest tests/test_passthrough.py -q                    # 单文件
.venv/bin/python -m pytest "tests/test_passthrough.py::TestUploadsApdl" -q # 单测试
uv run uvicorn ansys_hip.main:app --port 8010       # 启动服务;Swagger 在 /docs,运维面板在 /panel
cd packages/hip-playback && npm test                # 前端回放库单测(vitest);npm run build 出 dist 单文件
```

- 测试**不跑真 MAPDL**(conftest 默认 ANSYS 路径指向 `/nonexistent`,/health 为 degraded);真求解由 lead 在部署机 v252 上手工 e2e 验证。
- MAPDL 路径/许可来自 `config/service.yaml`,可被 `ANSYS_BIN` / `ANSYSLMD_LICENSE_FILE` / `HIP_SERVICE_CONFIG` 环境变量覆盖。

## 架构总览

请求流水线(跨文件追踪的入口):

```
api.py(decorator 路由,统一错误体 ErrorBody{code,message};
       手写 POST /sim/passthrough 为唯一可见提交路由,
       泛化 POST /sim/{method} 兜底(include_in_schema=False),两路共用 _submit 管线;
       _submit 入口查 passthrough 开关,关闭即 403 早返回)
  → registry.py(REGISTRY:仅 passthrough 一个 MethodSpec + 懒加载内核执行器)
  → api._resolve_params(内联 params 经 PassthroughParams 校验;落盘 resolved-params.json 保证可追溯)
  → queue.py(asyncio 队列;max_concurrent=1 单许可;to_thread 跑同步内核;
             超时/取消/异常映射;状态机 pending→running→succeeded/failed/cancelled;
             state() 组装响应时读时投影 progress.csv → JobState.stages)
  → kernels/passthrough.run_passthrough(params, ctx) -> dict   # 内核契约
       复制 entry+extra 进 job_dir 根(原名)→ runner.py(唯一 MAPDL 子进程入口)
       → 发布工件(entry + job.out 无条件 + declared_outputs)→ results.py 解析 results.csv
```

关键设计:

- **内核契约**(见 `kernels/__init__.py` docstring):同步函数;返回 dict 必含 `fidelity`;用户可下载工件只写 `artifact_dir(ctx)`(= `job_dir/artifacts/`)且 `"artifacts"` 列**裸文件名**;主动失败抛 `KernelError(code, message)`。
- **declared_outputs 契约**("服务无逻辑"的关键):上游声明作业结束应产出的裸文件名清单,缺一即 `KernelError("ARTIFACT_NOT_FOUND")`;认定 = 根部写出(经 `publish_artifacts` 复制进 artifacts/)**或** .inp 直写 artifacts/(上游模板惯例,提交时 artifacts/ 为空故无自我满足漏洞)。
- **可选结构化结果**:.inp 在 job_dir 根写 `results.csv`(标签 ≤8 字符 + 数值)则解析进 result 的 `values: dict[str, float]`;缺席即无字段(纯搬运,不做物理解读)。
- **阶段进度读时投影**:上游 .inp 约定用 `*CFOPEN` 覆盖式整文件重写 `progress.csv`(阶段标签 + 累计秒);`JobQueue.state()` 轮询时经 `results.parse_progress_csv` 投影进 `JobState.stages`(pending → None;running → 实时;终态 → 末帧)。无后台协程、无状态迁移、无竞态。
- **作业列表与历史回退**:`GET /jobs` = `queue.list_jobs()`(内存活跃实时 + 盘上 state.json 只读 `JobSnapshot` 回退,按 created_at 倒序);`require_job` 同经 `queue.snapshot` — 服务重启后历史作业的状态/日志/结果/工件端点仍可用,`POST /jobs/{id}/cancel` 强制中断 pending/running 作业(killpg 同步执行,**保留作业目录供排障**,终态幂等);DELETE 对历史终态作业为纯目录清理;`GET /jobs/{id}/log?source=job.log|job.out` 白名单选源(运行中也可读根部实时 job.out)。
- **运维面**:`GET /panel` 自托管单页面板(`src/ansys_hip/static/`,零构建原生 JS,内网免鉴权);请求访问日志经 `access_log.py` 中间件全量记录(`TimedRotatingFileHandler` 按天轮转,默认保 14 天,落 `access_log.file`(部署机 `/ansys/hip-var/logs/access.log`,sdb)),`GET /service/log` 尾读。
- **前端回放组件**:`packages/hip-playback/` — 帧工件 → `<hip-playback>` 即插即用 3D 回放 Web Component(three r128 单文件,浏览器侧构网:emap 通用 + 规则格反推双路径);跨域拉取经 `server.cors_origins`(默认关,只放行 GET);详见 `docs/playback-handbook.md` §5 与包内 README。
- **作业目录** `<jobs_dir>/<id>/`(路径随 config `storage.jobs_dir`,部署机在 sdb:`/ansys/hip-var/jobs`;日志同经 `access_log.file` 落 `/ansys/hip-var/logs/`,均不占系统盘):`state.json` / `resolved-params.json` / `result.json` / `job.log` / `artifacts/` / MAPDL 的 `job.out`/`launcher.log`(外部读取面:工件下载仅限 `artifacts/`;`job.log`/`job.out` 经日志端点 `?source=` 白名单可读纯文本,`launcher.log` 与其余根部簿记文件不对外;`RESERVED_JOB_DIR_NAMES` 钉测防上传文件撞名)。重启时遗留 pending/running → failed;超保留期目录自动清扫。
- **runner.py 诊断阶梯**:许可错误(LICENSE_UNAVAILABLE)→ job.out `*** ERROR ***`/`*** FATAL ***` 标记行/非零退出(CONVERGENCE_FAILED;诊断读对超 16MB 的 job.out 只取首 2MB + 尾 14MB,防 NERR 失控输出整读进内存)→ 正常结束但异常(INTERNAL)。进程用独立进程组,取消 = killpg;`required_outputs` 缺件检查由内核按 declared_outputs 自行判定(runner 默认 None)。
- **超时** = min(用户 `timeout_s`, 全局 `job_timeout_s`),经 `ctx.model_copy` 传内核,queue 层零特判。

## 领域约束(改 passthrough / 解析器 / 示范 .inp 前必读)

- **APDL `*VWRITE` 字符字面量标签 ≤8 字符**,超长被静默截断(撞键);progress.csv 标签与 results.csv 键均受此约束(JobStage 模型 maxLength=8 钉住)。
- **`*CFOPEN` 无 APPEND** → progress.csv 采用整文件覆盖式重写契约;读侧容忍撕裂半行(坏行跳过并记日志)。
- **runner 的 cwd=job_dir** 是上游 .inp 相对引用(`CDREAD` 等)的依赖,写进契约即为承诺。
- **`-j` 作业名固定 `hipjob`**(job_id 含 `-`/`_` 不适合作 MAPDL 文件名前缀)。
- 单位制(mm/MPa/s/℃)责任归上游 .inp,服务不做任何换算。
- **MAPDL 官方退出码表**(Operations Guide §4.1 Table 4.1):0=正常/1=指示错误(含崩溃信号)/5=命令行参数错误/7=许可失败/8=运行结束异常;失败消息经 `runner.EXIT_CODE_MEANINGS` 附官方语义。`/NERR` 的 NMABT 默认 10000(超万条错误+警告即 "terminated by this error",大模型可 `/NERR,,99999999` 抬高)。
- **job.out 启动横幅 `Opening new LOG, ERROR, LOCK and PAGE FILES` 含 "ERROR" 字样**(指 .err 文件,每个 job.out 头部都有);错误行判定必须钉 `*** ERROR ***` 完整标记,裸子串匹配会把退出码 0 的干净作业整批判失败。

## 测试

- `tests/conftest.py`:`settings_factory`(真实主配置 + tmp 存储 + 不存在的 ANSYS 路径)、`client`(进 lifespan 的 TestClient)、`fake_executors`(按方法名注入假内核)。
- 开通道提交的样板:`enabled_settings(settings_factory)` + `upload_apdl(client, name)` 助手在 `tests/test_passthrough.py`;队列投影用例在 `tests/test_progress.py`(经 passthrough 通道端到端)。
- 列表/历史回退用例在 `tests/test_jobs_list.py`(含 `make_history_job` 手造历史作业助手);面板静态托管在 `tests/test_panel.py`;访问日志在 `tests/test_access_log.py`。
- 改 `schemas.py`/`api.py` 的 Swagger 描述后,可用 `create_app(settings).openapi()` 内省自查字段/参数 description 覆盖率。

## 约定

- 不可变优先:配置与状态模型全部 frozen pydantic / dataclass;状态迁移用 `dataclasses.replace` 生成新实例,不改原对象。
- 错误码全服务统一:INVALID_PARAMS / PASSTHROUGH_DISABLED / METHOD_DISABLED / MAPDL_NOT_FOUND / LICENSE_UNAVAILABLE / CONVERGENCE_FAILED / TIMEOUT / ARTIFACT_NOT_FOUND / INTERNAL(PART_NOT_FOUND / GEOMETRY_NOT_FOUND 字符串保留仅供历史作业错误回放)。
- 配置模型 `extra="forbid"`(拼写错误启动即暴露);内核入参在 API 边界经 params_model 校验后才进队列。
- 提交信息:conventional commits(`feat:`/`fix:`/`refactor:`/`test:`/`docs:`),中文描述,无 attribution 尾注。
