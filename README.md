# ansys-hip-service

HIP 仿真**计算方法提供方** — 面向 HIPForm 的方法级 ANSYS/MAPDL 计算服务(独立进程/独立机器部署)。

> 定位:HIPForm 需要某项计算(致密化曲线/轴对称/3D 求解/参数标定…)时按需调用单个方法,
> 不复制上游业务 API(configs/inputs 资产管理不在本服务)。

## 快速开始

```bash
uv sync
uv run uvicorn ansys_hip.main:app --host 0.0.0.0 --port 8010
# Swagger 详细文档: http://<host>:8010/docs   (简要文档: docs/api-brief.md)
```

## 方法一览(12 个,阶段 1 全部上线)

| 分组 | 方法 | 内核(阶段 1) |
|---|---|---|
| 快速计算 | densification / process-window / shrinkage-estimate / material-query / mesh | 真实(Arrhenius·scipy·gmsh) |
| 2D FEM | axisym-hip* / axisym-thermal / axisym-mechanical* | thermal 真实;*冒烟(阶段 2 换本构,API 不变) |
| 3D FEM | full3d-hip* | 冒烟(真实网格+占位本构) |
| 反演/优化 | calibrate / compensate / sensitivity | 真实(scipy·缩放) |

冒烟方法在 `GET /sim/methods` 标 `status: experimental`,结果 JSON 带 `fidelity: "smoke"`。

## 配置体系(三级覆盖)

```
请求内联 params > config/parts/<零件>.yaml > config/service.yaml (defaults)
```

- `config/service.yaml` — 网关级主配置:端口/ANSYS 路径/许可/并发/jobs 目录/默认工艺曲线(900℃/120MPa/3h)
- `config/parts/*.yaml` — 零件配置:几何路径/材料选定/曲线与网格覆盖

## 环境

- ANSYS 2022 R1 (`ansys221` 批处理;单许可 → 队列并发 1)
- Python 3.12 / FastAPI / pydantic v2 / numpy / scipy / gmsh / meshio

## 文档

- `docs/api-brief.md` — 简要 API 文档(快速开始/错误码/curl 示例)
- `docs/materials-process.md` — 材料/工艺规格(TC4 粉末 + 20 钢包套 + 900℃/120MPa/3h)
- `/docs`(Swagger)/`/redoc` — 全端点详细文档(字段级 description/examples)
