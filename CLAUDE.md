# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

HIP 仿真**计算方法提供方** — 面向 HIPForm 的方法级 ANSYS/MAPDL 计算服务(FastAPI,独立进程部署)。12 个仿真方法共用一个泛化端点 `POST /sim/{method}`,异步作业模式:提交(202)→ 轮询 `GET /jobs/{id}` → 取结果/工件。仓库注释、文档、提交信息全部用中文。

## 常用命令

```bash
uv sync                                          # 安装依赖(uv.lock;或已有 .venv)
.venv/bin/python -m pytest -q                    # 全量测试(bare python 不在 PATH,须用 venv 或 uv run)
.venv/bin/python -m pytest tests/test_mesh.py -q                        # 单文件
.venv/bin/python -m pytest "tests/test_mesh.py::TestFemHelpers::test_probe_names" -q   # 单测试
.venv/bin/python -m pytest -m "not slow" -q      # 跳过 @slow(真 STEP 夹具)
uv run uvicorn ansys_hip.main:app --port 8010    # 启动服务;Swagger 在 /docs
```

- 测试**不跑真 MAPDL**(conftest 默认 ANSYS 路径指向 `/nonexistent`,/health 为 degraded);真求解由 lead 用部署机 v252 手工 e2e 验证。
- `@slow` 标记 = `~/tempt/` 下真 STEP 夹具(仅 gmsh 网格，文件不存在自动 skip)。
- MAPDL 路径/许可来自 `config/service.yaml`(v252),可被 `ANSYS_BIN` / `ANSYSLMD_LICENSE_FILE` / `HIP_SERVICE_CONFIG` 环境变量覆盖。

## 架构总览

请求流水线(跨文件追踪的入口):

```
api.py(decorator 路由,统一错误体 ErrorBody{code,message};
       提交路由由 REGISTRY 循环生成 12 个类型化 POST /sim/{name},
       泛化 POST /sim/{method} 为兜底(include_in_schema=False),两路共用 _submit 管线)
  → registry.py(REGISTRY 冻结表:MethodSpec 元数据 + 懒加载内核执行器)
  → settings.py merge_params(三级合并,纯函数)
  → queue.py(asyncio 队列;max_concurrent=1 单许可;to_thread 跑同步内核;
             超时/取消/异常映射;状态机 pending→running→succeeded/failed/cancelled)
  → kernels/<mod>.run_<method>(params, ctx) -> dict   # 内核契约
       FEM 系:fem.py → apdl/*.inp(Jinja2 StrictUndefined)→ runner.py(唯一 MAPDL 子进程入口)
              → results.py 解析 series.csv / summary.csv
```

关键设计：

- **新增方法** = `registry.py` REGISTRY 加一个 `MethodSpec` + `schemas.py` 加参数模型 + `kernels/` 某模块提供 `run_<method>`(方法名连字符→下划线)。API 层零改动。
- **内核契约**(见 `kernels/__init__.py` docstring):同步函数;返回 dict 必含 `fidelity`;用户可下载工件只写 `artifact_dir(ctx)`(= `job_dir/artifacts/`)且 `"artifacts"` 列**裸文件名**;主动失败抛 `KernelError(code, message)`。MAPDL 在 job_dir 根写出的 csv 经 `publish_artifacts` 复制进 artifacts/。
- **三级参数合并**：内联 `params` > `config/parts/<零件>.yaml` > `config/service.yaml` defaults;合并结果落盘 `resolved-params.json` 保证可追溯。分节规则看 `settings.py merge_params` docstring(cycle 整体替换、materials 深合并等)。
- **作业目录** `var/jobs/<id>/`:`state.json` / `resolved-params.json` / `result.json` / `job.log` / `artifacts/` / MAPDL 的 `job.out`/`launcher.log`(根部簿记文件不对外下载)。重启时遗留 pending/running → failed;超保留期目录自动清扫。
- **runner.py 诊断阶梯**：许可错误(LICENSE_UNAVAILABLE)→ job.out ERROR 行/非零退出(CONVERGENCE_FAILED)→ 正常结束但无 summary.csv(INTERNAL)。进程用独立进程组,取消 = killpg。
- 冒烟 vs 真实：方法标 `fidelity: smoke`(axisym-hip / axisym-mechanical / full3d-hip,线性占位本构)与 `real`;阶段 2 换本构时 **API 不变**。

## 领域约束(改 FEM/模板/网格前必读)

- **单位**:mm / MPa / s / ℃;APDL 侧 KXX=mW/(mm·K)、C=mJ/(tonne·K)、DENS=tonne/mm³(`apdl/material_apdl.py` 负责换算)。
- **MAT 编号全局约定**：1=powder、2=capsule — mesh.py 的 physical group、.cdb 的 MAT、各求解模板三处必须一致。
- **APDL `*VWRITE` 字符字面量标签 ≤8 字符**，超长被静默截断(撞键)。`kernels/fem.py SUMMARY_LABELS` 是 summary.csv 短标签 ↔ API 结果键的唯一映射;`tests/test_mesh.py _assert_summary_labels` 是写读钉测(改标签须同时改模板与读回侧)。
- **series.csv 探针列**用字符字面量写出(`(E16.8,',',A2,',',E16.8)`,I2 对字符数据打 `**`),读回侧 `int(row["probe"])`;探针数因此 ≤99。
- **SOLID45 退化面**：六面体面 4/6 是退化凝聚面，SFE 加压被 MAPDL 静默忽略；四面体面→可加载面映射 `mesh.py TET_FACE_TO_SOLID45`(值域 {1,2,3,5})。3D 模板的 `pressure_faces` 由 `exterior_faces_from_cdb` 产出。
- **gmsh 全局 API 非线程安全**：所有调用经 `mesh.py _GMSH_LOCK` 串行;`gmsh.initialize(interruptible=False)`(内核在工作线程跑，gmsh 的 SIGINT 处理器仅主线程合法)。
- **meshio 的 "ansys" 格式实为 Fluent .msh**,MAPDL 读不了 → .cdb 命令流是手写的(`mesh.py`)。
- `-j` 作业名固定 `hipjob`(job_id 含 `-`/`_` 不适合作 MAPDL 文件名前缀)。

## 测试

- `tests/conftest.py`:`settings_factory`(真实主配置 + tmp 存储 + 不存在的 ANSYS 路径)、`client`(进 lifespan 的 TestClient)、`fake_executors`(按方法名注册假内核，未注册 = 模拟未实现)。
- `test_mesh.py` 用 gmsh OCC 现建嵌套盒 STEP 作夹具(不依赖外部文件);模板渲染测试断言关键词级内容。
- 改 `schemas.py`/`api.py` 的 Swagger 描述后，可用 `create_app().openapi()` 内省自查字段/参数 description 覆盖率。

## 约定

- 不可变优先：配置与状态模型全部 frozen pydantic / dataclass;状态迁移用 `dataclasses.replace` 生成新实例，不改原对象。
- 错误码全服务统一(见 `schemas.py` 顶部注释):INVALID_PARAMS / PART_NOT_FOUND / GEOMETRY_NOT_FOUND / METHOD_DISABLED / MAPDL_NOT_FOUND / LICENSE_UNAVAILABLE / CONVERGENCE_FAILED / TIMEOUT / INTERNAL。
- 配置模型 `extra="forbid"`(拼写错误启动即暴露)；内核入参在 API 边界经 params_model 校验后才进队列。
- 提交信息:conventional commits(`feat:`/`fix:`/`refactor:`/`test:`/`docs:`),中文描述,无 attribution 尾注。
