"""MAPDL 结果提取 — job.out 错误行、results.csv / progress.csv 解析。

文件名约定(全服务统一,kernels 按此写出/读回):
    job.out      MAPDL 批处理输出(runner 经 -o 指定)
    results.csv  两列标签,数值(可选);直通通道上游 inp 自写的关键结果,
                 标签沿用历史 summary.csv 短标签约定(≤8 字符,
                 受 APDL *VWRITE 字符字面量宽度限制)
    progress.csv 两列 label,time_s,无表头;上游 .inp 经 *CFOPEN 覆盖式整文件
                 重写的进度侧车(队列按读时投影解析,行序 = 阶段完成序)

解析均为纯文本处理,可独立单测,不依赖 MAPDL。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from .schemas import JobStage

logger = logging.getLogger(__name__)

# --- 文件名常量(runner / kernels 共用,勿散落字面量) ---
OUT_FILENAME = "job.out"
RESULTS_FILENAME = "results.csv"
PROGRESS_FILENAME = "progress.csv"

# MAPDL 输出中的错误行特征(job.out 中同时用于 CONVERGENCE_FAILED 诊断)
ERROR_LINE_KEYWORDS: tuple[str, ...] = ("ERROR", "FATAL")

# MAPDL 结尾/中途的统计行前缀(如 "NUMBER OF ERROR MESSAGES ENCOUNTERED= N"),
# 含 ERROR 关键字但不是错误,提取时排除
STATISTICS_LINE_PREFIXES: tuple[str, ...] = ("NUMBER OF", "THE NUMBER OF")

# 提取错误行上限(KernelError message 面向人读,过载无益)
MAX_ERROR_LINES = 40


def extract_error_lines(out_text: str) -> list[str]:
    """从 job.out 文本中提取含错误特征的行(去空白、保序、限量)。

    匹配 ANSYS 经典的 "*** ERROR ***" / "*** FATAL ***" 行(大小写敏感:真错误行
    恒为大写关键字,小写 "error" 只出现在警告散文里,如 "could invalidate error
    estimation.");许可类错误行通常也含 ERROR 关键字,由 runner 先行按许可特征
    识别,本函数不区分。结尾的 "NUMBER OF ERROR/FATAL MESSAGES ENCOUNTERED= N"
    与中途的 "The number of ERROR and WARNING messages exceeds 200."(警告超量
    提示,真错误自身会以 *** ERROR *** 行出现)都是统计行,不是错误,排除。
    """
    lines = [
        stripped
        for raw in out_text.splitlines()
        for stripped in (raw.strip(),)
        if stripped
        and any(keyword in stripped for keyword in ERROR_LINE_KEYWORDS)
        and not stripped.upper().startswith(STATISTICS_LINE_PREFIXES)
    ]
    return lines[:MAX_ERROR_LINES]


def parse_results_csv(job_dir: Path) -> dict[str, float] | None:
    """读直通通道的 results.csv(标签,数值 两列)→ {标签: 数值}。

    可选契约:文件缺失返回 None(由调用方决定结果 dict 是否带 values);
    撕裂容忍:表头行/字段数不对/数值不可解析的行一律跳过,不抛异常
    (写出与读取可能并发,坏行只丢该行)。
    """
    path = Path(job_dir) / RESULTS_FILENAME
    if not path.is_file():
        return None
    values: dict[str, float] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = [field.strip() for field in raw.split(",")]
        if len(fields) != 2 or not fields[0]:
            continue
        if fields[0].lower() == "key":  # 容错:跳过可能的表头行
            continue
        try:
            values[fields[0]] = float(fields[1])
        except ValueError:
            continue
    return values


def parse_progress_csv(job_dir: Path) -> list[JobStage] | None:
    """读 progress.csv(label,time_s 两列,无表头)→ 阶段序列;文件不存在 → None。

    侧车由上游 .inp 用 *CFOPEN 覆盖式整文件重写,读时可能撞上写一半:
    撕裂半行与任何不满足 JobStage 约束的坏行(列数错/时间非浮点/空或超
    8 字符标签/负耗时)一律跳过并记日志,只返回可完整解析的阶段。
    读盘失败(权限/句柄占用等)→ None 并告警,不拖垮状态查询;
    文件存在但无完整行(首帧撕裂)→ [],语义上区别于 None(无侧车)。
    """
    path = Path(job_dir) / PROGRESS_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("读取 %s 失败,按无侧车处理: %r", path, exc)
        return None
    stages: list[JobStage] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stage = _parse_stage_row(raw)
        if stage is None:
            logger.warning("%s 第 %d 行无法解析,已跳过: %r", path, line_no, raw)
            continue
        stages.append(stage)
    return stages


def _parse_stage_row(raw: str) -> JobStage | None:
    """单行 → JobStage;撕裂/坏行返回 None(记日志由调用方统一处理)。"""
    fields = [field.strip() for field in raw.split(",")]
    if len(fields) != 2:
        return None
    try:
        return JobStage(label=fields[0], time_s=float(fields[1]))
    except (ValueError, ValidationError):
        return None
