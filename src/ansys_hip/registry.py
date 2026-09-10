"""方法注册表(冻结版)— passthrough 单方法的元数据与执行器解析.

API 层(/sim/*)只依赖本注册表;内核按签名约定提供
`run_<method>(params, ctx) -> dict`,注册表按需懒加载,缺失时返回
METHOD_NOT_IMPLEMENTED。类型化方法库已整体移除,服务收敛为直通通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .schemas import PassthroughParams


class KernelError(Exception):
    """内核主动失败(作业 → failed,error 原样返回给调用方)。

    code 取值见 schemas.py 顶部错误码约定;message 面向人读,
    MAPDL 类错误应附 job.out 中的关键行。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


Group = Literal["passthrough"]

GROUP_LABELS: dict[Group, str] = {
    "passthrough": "直通通道(上游自带 APDL 输入)",
}

# 直通通道方法名(api 层开关闸门逻辑据此判定,勿散落字面量)
PASSTHROUGH_METHOD = "passthrough"


@dataclass(frozen=True)
class MethodSpec:
    """一个 /sim/{method} 端点的全部元数据(自描述用,序列化给 GET /sim/methods)。"""

    name: str
    group: Group
    status: Literal["available", "experimental", "planned"]
    fidelity: Literal["real", "smoke"]
    summary: str                       # 一句话用途(HIPForm 侧据此选方法)
    returns: str                       # 结果 JSON 形态简述
    typical_runtime: str
    requires_mapdl: bool
    requires_geometry: bool            # True = 必须给 geometry 或 part 配置
    params_model: type[BaseModel] = field(compare=False)
    tags: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        """GET /sim/methods 条目(params 模型展开为 JSON Schema,Swagger 外的自描述)。"""
        return {
            "name": self.name,
            "group": self.group,
            "group_label": GROUP_LABELS[self.group],
            "status": self.status,
            "fidelity": self.fidelity,
            "summary": self.summary,
            "returns": self.returns,
            "typical_runtime": self.typical_runtime,
            "requires_mapdl": self.requires_mapdl,
            "requires_geometry": self.requires_geometry,
            "params_schema": self.params_model.model_json_schema(),
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# 注册表(单方法:passthrough;需 passthrough.enabled=true,默认关闭 → 403)
# ---------------------------------------------------------------------------

REGISTRY: dict[str, MethodSpec] = {
    "passthrough": MethodSpec(
        name="passthrough",
        group="passthrough",
        status="experimental",
        fidelity="real",   # 注册表口径须为 real/smoke(queue.Fidelity 枚举);实际保真度由内核结果 dict 的 fidelity="passthrough" 表达
        summary="直通通道:上游自带 APDL 输入(.inp/.cdb/.mac)直接交 MAPDL 执行,服务只治理作业/队列/超时/工件(需 passthrough.enabled=true)",
        returns="{artifacts[], returncode, elapsed_s, values?}(values 仅当入口写出 results.csv)",
        typical_runtime="取决于入口输入(受 ansys.job_timeout_s 或 timeout_s 约束)",
        requires_mapdl=True,
        requires_geometry=False,
        params_model=PassthroughParams,
        tags=("apdl", "raw-mapdl"),
    ),
}


# ---------------------------------------------------------------------------
# 执行器解析(懒加载)
# ---------------------------------------------------------------------------

# 按序搜索的 kernels 子模块(run_<method> 带下划线名,与方法名连字符对应)
KERNEL_MODULES: tuple[str, ...] = (
    "passthrough",  # T1: passthrough(上游自带 APDL 输入直通执行)
)

Executor = Callable[[BaseModel, Any], dict]


def get_method(method: str) -> MethodSpec | None:
    """按名称取方法元数据;不存在返回 None(由 API 层回 404)。"""
    return REGISTRY.get(method)


def resolve_executor(method: str) -> Executor | None:
    """解析内核执行器 run_<method>(params, ctx) -> dict。

    懒加载 kernels 子模块:任一子模块可选依赖缺失不影响 API 启动;
    找不到返回 None(由调用方回 METHOD_NOT_IMPLEMENTED)。
    """
    spec = REGISTRY.get(method)
    if spec is None:
        return None
    func_name = "run_" + method.replace("-", "_")
    for mod_name in KERNEL_MODULES:
        try:
            module = import_module(f"ansys_hip.kernels.{mod_name}")
        except ImportError:  # 可选依赖缺失或子模块尚未实现
            continue
        func = getattr(module, func_name, None)
        if func is not None:
            return func
    return None


def methods_payload() -> list[dict[str, Any]]:
    """GET /sim/methods 响应体:按名排序的全部方法自描述。"""
    specs = sorted(REGISTRY.values(), key=lambda s: s.name)
    return [s.public_dict() for s in specs]
