"""方法内核包 — 每个模块提供若干 run_<method>(params, ctx) -> dict。

子模块(与 registry.KERNEL_MODULES 对应):
    arrhenius   densification / process-window / sensitivity
    shrinkage   shrinkage-estimate / compensate
    calibrate   calibrate
    materials   material-query(数据在 data/materials.yaml)
    fem         axisym-hip / axisym-thermal / axisym-mechanical / full3d-hip / mesh

约定(见 schemas.RunContext 与 registry.KernelError):
    - 同步函数,由队列在工作线程调用;
    - 返回 dict 必含 "fidelity" 键;
    - 用户可下载工件统一写入 artifact_dir(ctx)(= job_dir/artifacts/,
      下载端点 GET /jobs/{id}/artifacts/{name} 只服务该子目录),并在
      "artifacts" 列出**裸文件名**(与集合端点列表严格一致);
      job_dir 根部保留簿记文件(state.json/result.json/job.log 及 MAPDL 的
      job.out/launcher.log 等),不对外下载;
    - 主动失败抛 KernelError(code, message)。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Sequence

from ..schemas import RunContext

__all__ = ["artifact_dir", "publish_artifacts"]

logger = logging.getLogger(__name__)


def artifact_dir(ctx: RunContext) -> Path:
    """用户可下载工件目录 job_dir/artifacts/(不存在则创建)。"""
    directory = ctx.job_dir / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def publish_artifacts(ctx: RunContext, filenames: Sequence[str]) -> list[str]:
    """把 MAPDL 在 job_dir 根写出的文件(series/summary 等)复制进 artifacts/。

    MAPDL 以 job_dir 为工作目录,结果 csv 只能落在根部;发布 = 复制到
    artifacts/ 供下载端点服务。未产出的文件跳过并告警(不算错误),
    返回成功发布的裸文件名列表。
    """
    published: list[str] = []
    for name in filenames:
        source = ctx.job_dir / name
        if not source.is_file():
            logger.warning("待发布工件未产出,跳过: %s", source)
            continue
        shutil.copyfile(source, artifact_dir(ctx) / name)
        published.append(name)
    return published
