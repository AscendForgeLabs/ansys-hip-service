"""直通通道内核 — 上游自带 APDL 输入直接交 MAPDL 执行(方法 passthrough)。

服务侧零仿真逻辑,只做治理:校验入口/附属文件 → 按原名复制进作业目录根部
(MAPDL 以根部为 cwd,入口内的相对引用自然解析)→ runner 批处理执行
(不检查 summary.csv,产出改由 declared_outputs 判定)→ 发布工件。

发布面(全部裸文件名,复制进 artifacts/ 供下载):入口文件、job.out
(无条件,失败作业也留排查证据)、results.csv(可选结构化结果,存在才发布)、
progress.csv(阶段进度侧车,存在才发布)、declared_outputs;声明输出缺失 →
KernelError(ARTIFACT_NOT_FOUND)在发布之后抛出 — 存在的产出与 job.out 已可下载。
声明产出的认定 = 根部发布 **或** .inp 直写 artifacts/(上游模板惯例,文件
本就在下载面;结构化 values 仍只看根部 results.csv)。MAPDL 执行失败
(runner 抛 KernelError)同样先发布入口与 job.out 再上抛。

结果 dict 契约:
    {fidelity: "passthrough", artifacts[], returncode, elapsed_s, values?}
    values 仅当 results.csv 存在(标签,数值 两列,标签沿用 summary.csv
    短标签约定 ≤8 字符);缺席不带该键。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..registry import KernelError
from ..results import (
    OUT_FILENAME,
    PROGRESS_FILENAME,
    RESULTS_FILENAME,
    parse_results_csv,
)
from ..runner import run_mapdl
from ..schemas import RESERVED_JOB_DIR_NAMES, PassthroughParams, RunContext
from . import artifact_dir, publish_artifacts


def run_passthrough(params: PassthroughParams, ctx: RunContext) -> dict:
    """直通执行:暂存输入 → MAPDL 批处理 → 发布并校验声明产出。"""
    started = time.monotonic()
    entry_dest, _extra_dests = _stage_inputs(params, ctx.job_dir)
    kernel_ctx = _with_clamped_timeout(ctx, params.timeout_s)
    try:
        outcome = run_mapdl(
            entry_dest,
            ctx.job_dir,
            kernel_ctx,
            ctx.job_dir.name,       # 作业名(= job_id,runner 进程表键;-j hipjob 文件名前缀)
            required_outputs=None,  # 产出由 declared_outputs 判定,不检查 summary.csv
        )
    except KernelError:
        # 求解失败也保排查证据:入口与 job.out 无条件发布后再上抛
        publish_artifacts(ctx, [entry_dest.name, OUT_FILENAME])
        raise

    values = parse_results_csv(ctx.job_dir)
    # 保序去重:results.csv/progress.csv 可能同时出现在可选项与声明清单中
    # (progress.csv 虽是保留名不可声明,仍防御性去重)
    candidates = list(dict.fromkeys([
        entry_dest.name,
        OUT_FILENAME,
        PROGRESS_FILENAME,
        *([RESULTS_FILENAME] if values is not None else []),
        *params.declared_outputs,
    ]))
    published = publish_artifacts(ctx, candidates)
    # 上游惯例兼容:.inp 直写 artifacts/ 子目录的声明产出同样算已产出
    # (artifacts/ 提交时为空,凡在必为本轮运行所写,无"输入自我满足"漏洞);
    # 直写文件并入发布名单 — 它们本就在下载面
    direct_written = [
        name for name in params.declared_outputs
        if name not in published and (artifact_dir(ctx) / name).is_file()
    ]
    missing = [
        name for name in params.declared_outputs
        if name not in published and name not in direct_written
    ]
    if missing:
        raise KernelError(
            "ARTIFACT_NOT_FOUND",
            f"声明输出未产出: {missing}(job.out 已发布,可下载排查)",
        )

    result = {
        "fidelity": "passthrough",
        "artifacts": [*published, *direct_written],
        "returncode": outcome["returncode"],
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    if values is not None:
        result["values"] = values
    return result


def declared_output_overlap(params: PassthroughParams) -> list[str]:
    """声明输出与入口/附属文件 basename 的交集(排序)。

    输入会按原名复制进作业目录根部(见 _stage_inputs),同名声明输出会被
    这份拷贝"自我满足",缺件检查被短路(MAPDL 零产出也算 succeeded)→
    交集非空即应拒绝。供 API 边界做 400 fail-fast:该不变量是 staging
    复制行为的推论,随本模块演化,不散落在 API 层重推。
    """
    input_names = {Path(params.entry_file).name}
    input_names.update(Path(path).name for path in params.extra_files)
    return sorted(input_names.intersection(params.declared_outputs))


def _stage_inputs(params: PassthroughParams, job_dir: Path) -> tuple[Path, list[Path]]:
    """校验入口/附属文件 → 按原名复制进 job_dir 根部;返回 (入口, 附属列表) 的目标路径。

    校验:入口存在且非空、附属存在、basename 不撞保留名且互不冲突
    (复制而非引用:作业自包含才可复算/审计,上传文件可能先于作业被清退)。
    """
    entry = Path(params.entry_file)
    if not entry.is_file() or entry.stat().st_size == 0:
        raise KernelError("INVALID_PARAMS", f"入口文件不存在或为空: {params.entry_file}")
    extras = [Path(path) for path in params.extra_files]
    for path in extras:
        if not path.is_file():
            raise KernelError("INVALID_PARAMS", f"附属文件不存在: {path}")

    sources = (entry, *extras)
    for label, source in zip(("入口", "附属"), sources):
        if source.name in RESERVED_JOB_DIR_NAMES:
            raise KernelError(
                "INVALID_PARAMS", f"{label}文件 '{source.name}' 是作业目录保留名"
            )
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise KernelError(
            "INVALID_PARAMS", f"入口/附属文件 basename 冲突,无法同名复制: {sorted(names)}"
        )

    destinations = [job_dir / name for name in names]
    for source, dest in zip(sources, destinations):
        shutil.copyfile(source, dest)
    return destinations[0], destinations[1:]


def _with_clamped_timeout(ctx: RunContext, timeout_s: int | None) -> RunContext:
    """用户超时与全局上限取小;model_copy 生成新实例,不改原 ctx(queue 零改动)。"""
    if timeout_s is None:
        return ctx
    return ctx.model_copy(update={"job_timeout_s": min(timeout_s, ctx.job_timeout_s)})
