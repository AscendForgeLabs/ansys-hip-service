"""MAPDL 结果提取 — job.out 错误行、summary.csv / series.csv 解析。

文件名约定(全服务统一,apdl 模板与 kernels/fem 按此写出/读回):
    job.out      MAPDL 批处理输出(runner 经 -o 指定)
    summary.csv  两列 key,value;模板 *VWRITE 写出的终态摘要
    series.csv   三列 time_s,probe,value,无表头;probe 为 1 起始的探针序号,
                 模板按探针分块顺序追加(行序不影响解析)

解析均为纯文本处理,可独立单测,不依赖 MAPDL。
"""

from __future__ import annotations

from pathlib import Path

# --- 文件名常量(runner / apdl 模板 / kernels 共用,勿散落字面量) ---
OUT_FILENAME = "job.out"
SUMMARY_FILENAME = "summary.csv"
SERIES_FILENAME = "series.csv"

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


def parse_summary_csv(job_dir: Path) -> dict:
    """读 summary.csv(key,value 两列)→ dict;value 尽力转 float,失败保字符串。

    文件缺失/为空返回空 dict(由调用方判断是否构成 INTERNAL);格式异常行跳过。
    """
    path = Path(job_dir) / SUMMARY_FILENAME
    if not path.is_file():
        return {}
    summary: dict = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = [field.strip() for field in raw.split(",")]
        if len(fields) != 2 or not fields[0]:
            continue
        if fields[0].lower() == "key":  # 容错:跳过可能的表头行
            continue
        key, raw_value = fields
        try:
            summary[key] = float(raw_value)
        except ValueError:
            summary[key] = raw_value
    return summary


def parse_series_csv(job_dir: Path) -> list[dict]:
    """读 series.csv(time_s,probe,value 三列,无表头)→ [{time_s,probe,value}]。

    probe 为模板写出的探针序号(保留字符串,如 "1");fem 内核按序号映射探针名。
    无法解析的行(如表头/空行)跳过;按 time_s 升序稳定排序。
    """
    path = Path(job_dir) / SERIES_FILENAME
    if not path.is_file():
        return []
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = [field.strip() for field in raw.split(",")]
        if len(fields) != 3:
            continue
        try:
            time_s = float(fields[0])
            value = float(fields[2])
        except ValueError:
            continue
        rows.append({"time_s": time_s, "probe": fields[1], "value": value})
    rows.sort(key=lambda row: (row["time_s"], row["probe"]))
    return rows
