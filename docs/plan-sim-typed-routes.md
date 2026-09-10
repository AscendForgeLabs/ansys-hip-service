# 设计方案:REGISTRY 驱动的类型化提交路由(方案 B)

> 状态:已实现(2026-09-10;§4.2 修正:params 字段为可选而非必填,零件全继承提交仍可用)· 涉及 `api.py` / `schemas.py` / `tests/test_api.py`

## 1. 背景与动机

现状:`POST /sim/{method}` 是泛化端点,请求体 `SimRequest{part, params: dict[str, Any]}`。
12 个方法各自的参数模型(`AxisymHipParams` 等)**不在 OpenAPI spec 里**,字段级文档只能从
`GET /sim/methods` 的 `params_schema` 看。代价:

- 调用方要跨两处查文档;Swagger 里没有类型化表单、没有提前校验提示;
- openapi-md MCP / 代码生成器看不到方法参数;
- Try it out 只能手拼 JSON。

原因:请求体结构由**路径参数** `method` 决定,OpenAPI 的 `oneOf/discriminator`
绑定不到 path 上,塞进 Swagger 只能得到表达不了对应关系的大杂烩。

## 2. 目标 / 非目标

**目标**

- Swagger `/docs` 中每个方法有自己的 `POST /sim/{name}` 端点:字段级描述 + 类型化表单 + 自动校验;
- **新增方法依旧零改动**:路由、包装模型、示例全部从 `REGISTRY` 循环生成;
- 泛化端点 `POST /sim/{method}` 行为完全不变(HIPForm 零改动);
- 两套入口走同一条合并/校验/入队管线,结果 bit 级一致。

**非目标**

- 不改内核契约、队列、错误码、响应模型(`SimAccepted` 不变);
- 不改 `GET /sim/methods`(方法元数据真源,保留);
- 不做每方法独立 URL 前缀(如 `/sim/fem2d/axisym-hip`)。

## 3. 设计总览

```
create_app()
  └─ _sim_router(settings, parts, queue)
       ├─ GET /methods                      (不变)
       ├─ POST /{name}   × 12  ← 循环 REGISTRY 生成(先注册,先匹配)
       │     body = <Method>SimRequest{part, params: <Method>Params}   (create_model 动态包装)
       │     └─ _submit(spec, part, params.model_dump(exclude_unset=True))
       └─ POST /{method}        ← 泛化兜底(后注册;include_in_schema=False)
             └─ _submit(spec, part, merge 后的 inline dict)          (现逻辑)
_submit():disabled 503 / 未实现 501 / part 404 → merge_params 三级合并
           → model_validate → queue.submit          (两路共用)
```

## 4. 关键设计点

### 4.1 路由生成与注册顺序

- FastAPI 按注册顺序匹配:12 个具体路由(`POST /sim/densification` …)先注册,
  泛化 `POST /sim/{method}` 后注册 → 已知方法走类型化路由,未知方法落到泛化路由
  返回 404 `METHOD_NOT_FOUND`(现状语义)。
- 泛化路由设 `include_in_schema=False`:Swagger 只显示 12 个类型化端点(避免
  13 个 POST 的噪音),但**运行时继续可用**(HIPForm 兼容)。

### 4.2 动态包装模型(create_model)

每方法一个请求体模型,名称取 `spec.name` 连字符转驼峰 + `SimRequest`
(如 `AxisymThermalSimRequest`),字段:

```python
create_model(
    f"{camel(spec.name)}SimRequest",
    part=(str | None, Field(default=None, description="零件配置名(config/parts/<name>.yaml)")),
    params=(spec.params_model, Field(description=f"{spec.summary} 参数")),
)
```

- 组件名全局唯一,OpenAPI `components.schemas` 无冲突;
- 端点 `summary=spec.summary`,`description` 拼接 `returns` / `typical_runtime` /
  `tags`,`tags=[spec.group_label]` → Swagger 里按 4 个方法组折叠,信息密度高于现状;
- 现有 `openapi_extra` 3 个请求示例迁移:拆到 `densification`(零件全继承 /
  +内联覆盖 / 纯内联),其余 11 个方法各配 1 个真实参数示例(取自各内核 e2e 测试用例)。

### 4.3 衔接三级合并 —— `exclude_unset` 是关键(最大的坑)

**不能**把 `params.model_dump()` 全量当内联层传给 `merge_params`:
pydantic 会把模型默认值(如 `mesh.mesh_size_mm=6.0`)一起带出,深度合并后
**覆盖零件配置里的 4.0** —— 语义从"没给就继承"劣化为"默认值压配置"。

正解:

```python
inline = params.model_dump(exclude_unset=True)   # 只含调用方显式提供的字段
```

- 只取 `model_fields_set`,与今天"内联 dict 只包含显式键"的语义完全对齐;
- 附带改善:嵌套子模型也只 dump 已设子键(如只给 `geometry.capsule_step`,
  不会再以 `cavity_step=None` 顶掉零件配置的型腔路径 — 现状手拼 dict 做不到);
- 合并后仍走 `spec.params_model.model_validate(merged)` 重新校验
(零件/主配置来源的值同样受约束,现状已如此)。

### 4.4 未知字段必须继续 400(`extra="forbid"`)

泛化路由靠 `_resolve_params` 手工 unknown-field 检查报 400;类型化路由由
pydantic 校验,但各方法参数模型默认 `extra="ignore"` 会**静默吞掉拼错的字段**。
方案:`schemas.py` 加共享基类,12 个参数模型全部继承:

```python
class MethodParamsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

`RequestValidationError` 已有全局 handler → 400 `INVALID_PARAMS`(错误体格式不变)。
泛化路由的手工检查保留(双保险,两入口行为一致)。

### 4.5 状态码与错误映射(零变化)

| 场景 | 类型化路由 | 泛化路由(现状) |
|---|---|---|
| 未知方法 | 不可能命中(落泛化) | 404 METHOD_NOT_FOUND |
| 方法下线 | 503 METHOD_DISABLED | 同左 |
| 内核未实现 | 501 METHOD_NOT_IMPLEMENTED | 同左 |
| part 不存在 | 404 PART_NOT_FOUND | 同左 |
| 参数校验失败 | 422→400 INVALID_PARAMS(pydantic) | 400 INVALID_PARAMS(手工+pydantic) |
| 受理 | 202 SimAccepted | 同左 |

## 5. 兼容性声明

- `POST /sim/{method}`(泛化)请求/响应/错误体逐字节不变 —— HIPForm 无感;
- `GET /sim/methods`、`/jobs/*`、`/parts/*`、`/uploads`、`/health` 不动;
- 唯一对外可见变化:`/docs` 的 sim 组从 1 个端点变 12 个,`/openapi.json` 变大
  (12 个包装模型 + 12 个参数模型进 components)。

## 6. 测试计划

`tests/test_api.py` 增补(fake_executors 管线复用):

1. **openapi 内省**(参数化 12 方法):每个 `POST /sim/{name}` 存在、请求体引用
   正确的包装模型、`params` 子 schema 含字段 description;
2. **提交等价性**(参数化):同一 (part, 显式参数) 分别走类型化与泛化路由,
   fake executor 收到的合并参数一致、resolved-params.json 一致;
3. **exclude_unset 语义**:类型化路由只给 `initial_relative_density`,零件配置的
   mesh/cycle 仍生效(防默认值覆盖回归);
4. **extra=forbid**:类型化路由传未知字段 → 400 INVALID_PARAMS;
5. **兜底路由**:未知方法 404、disabled 503(在 settings.methods.disabled 里加名)、
   泛化路由 include_in_schema=False(openapi paths 不含 `/{method}` POST);
6. 现有 232 行测试全部保持绿色(泛化路由回归保护)。

## 7. 文件改动清单

| 文件 | 改动 | 规模 |
|---|---|---|
| `src/ansys_hip/api.py` | `_sim_router` 重构:抽 `_submit` 共用助手 + 循环生成 12 路由 + 泛化兜底 | ~+70 行 |
| `src/ansys_hip/schemas.py` | `MethodParamsBase`(extra=forbid)基类,12 模型改继承 | ~+15 行 |
| `tests/test_api.py` | 上述 6 组测试 | ~+130 行 |
| `README.md` / `CLAUDE.md` | 架构总览补一句"提交路由 REGISTRY 生成,泛化端点为兜底" | 各 1-2 行 |
| `docs/api-brief.md` | 如提及单一提交端点,补类型化路由说明 | 视内容 |

## 8. 风险与开放决策点

| # | 事项 | 建议 |
|---|---|---|
| R1 | `create_model` 生成的模型在 Swagger 的展示质量(嵌套 Cycle/材料选择的展开) | 实现后用 `/docs` 实测,必要时给包装模型补 `model_config` json_schema_extra |
| R2 | 泛化路由是否留在 Swagger(`include_in_schema`)| **隐藏**(本文按此写);若 lead 希望文档里保留泛化形态可改 True |
| R3 | `extra="forbid"` 对 `SimRequest.params` 手拼 dict 用户的破坏性 | 无:泛化路由本就手工拒绝未知字段,行为一致 |
| R4 | 12 个示例的取材 | 从各内核 e2e/测试用例中取真实值,不编造 |

## 9. 工作量

实现 + 测试 + 文档约 **1.5–2 小时**;无迁移、无配置变更、可一步回滚(整个改动收敛在 `_sim_router` 内)。
