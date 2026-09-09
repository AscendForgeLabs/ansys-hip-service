"""模板渲染 — apdl/*.inp 的 jinja2 环境与输入文件写出。

数值一律经 `|g` 过滤器以 %.9g 落盘(APDL 参数读取安全,无浮点尾噪声);
模板保持逻辑轻:分段/坐标/材料命令块等全部由 kernels/fem.py 预计算后传入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATE_DIR = Path(__file__).parent


def _format_number(value: Any) -> str:
    """数值 → %.9g;其余原样(供 jinja2 `|g` 过滤器)。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.9g}"
    return str(value)


_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)
_ENV.filters["g"] = _format_number


def render(template_name: str, **context: Any) -> str:
    """渲染模板为 APDL 文本;缺变量直接报错(StrictUndefined,防静默空值)。"""
    return _ENV.get_template(template_name).render(**context)


def write_input(template_name: str, out_path: Path | str, **context: Any) -> Path:
    """渲染并写出 inp 文件,返回其路径。"""
    path = Path(out_path)
    path.write_text(render(template_name, **context), encoding="utf-8")
    return path
