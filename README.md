# ansys-hip-service

HIP 仿真**纯 MAPDL 转发器** — 面向 HIPForm 的 ANSYS/MAPDL 计算运输服务(独立进程/独立机器部署)。

> **passthrough 单通道**:`POST /sim/passthrough` 是**唯一**作业提交通道。
> 上游自带 APDL 输入(.inp 及附属 .cdb 等),本服务只负责忠实执行与运输:
> 上传(`POST /uploads/apdl`)→ 提交(202)→ 轮询 `GET /jobs/{id}`(含 stages 阶段进度)
> → 取结果/工件;结果 `fidelity: "passthrough"`,服务不背书物理内容。
> 原有 12 个类型化方法 API 已整体移除、不再兼容(上游确认完全跟随本服务)。
> **安全门槛**:passthrough 是任意 APDL 执行面(可读写文件、起系统命令),开关默认关、
> 仅纯内网允许开启;公网隧道期间必须关闭(关闭时提交得 403 `PASSTHROUGH_DISABLED`)。
> 全端点已启用 **API Key 鉴权**(`X-API-Key` 请求头,fail-closed:未配置 key 时除
> `/health` 外全部 401;配置见 `config/service.yaml` auth 节或环境变量
> `HIP_SERVICE_API_KEYS`)。面板 `/panel` 静态壳免鉴权可达,数据请求首次 401 时弹窗输入 key 一次
> (存 sessionStorage,关标签页即清);`/docs` 浏览器直开仍 401。

## 快速开始

```bash
uv sync --extra dev
uv run uvicorn ansys_hip.main:app --host 0.0.0.0 --port 8010
# Swagger 详细文档: http://<host>:8010/docs
# 运维面板: http://<host>:8010/panel(作业列表/详情/日志/服务日志;根路径 / 自动跳转)
# 日志已全在文件(按天轮转,路径随 config:access_log/service_log):
# 启动不需要 shell 重定向,也不要传 --log-config(会与代码内接管互抢)
# 开启 passthrough: config/service.yaml 的 passthrough.enabled,
#                  或环境变量 HIP_SERVICE_PASSTHROUGH_ENABLED=true
```

## 对接

- 唯一入口:`docs/passthrough-guide.md` — HTTP 交付全流程、.inp 编写指南(declared_outputs
  声明 / progress.csv 阶段侧车 / results.csv 结构化结果)、错误码表、上游工作流映射。
- 示范工程:`docs/examples/passthrough-demo/` — 自包含轴对称包套缩放 .inp + 提交脚本,
  可直接用作通道冒烟。

## 配置

`config/service.yaml`(可被环境变量覆盖:`HIP_SERVICE_CONFIG` / `ANSYS_BIN` /
`ANSYSLMD_LICENSE_FILE` / `HIP_SERVICE_PASSTHROUGH_ENABLED`):

- ANSYS v252 批处理路径与许可文件;
- 队列并发(单许可 → 1)、作业超时(4h,用户 `timeout_s` 取 min);
- 存储目录(jobs/uploads)与保留期;清理三路(49G 爆盘事故后):启动 + `sweep_interval_s`
  周期清扫(按天保留 + `max_total_gb` 总量配额)+ `min_free_gb` 磁盘水位紧急清理
  (均 0 = 关闭对应路);
- 日志位置全配置化:请求访问日志 `access_log.file`、服务运行日志 `service_log.file`
  (uvicorn + 应用 logger 代码内接管,均按天轮转,保留 14 天);
- `passthrough.enabled` 开关(默认 false)。

## 环境

- ANSYS 2025 R2(`ansys252 -b -i` 批处理;单许可 → 队列并发 1)
- Python 3.12 / FastAPI / pydantic v2 / uvicorn

## 测试

```bash
.venv/bin/python -m pytest -q   # 假执行器,不跑真 MAPDL;真求解在部署机手工 e2e
```
