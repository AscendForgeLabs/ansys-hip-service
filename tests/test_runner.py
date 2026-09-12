"""runner 单测 — 假 ansys221(bash 脚本)覆盖成功/失败/许可/超时/取消/缺二进制路径。

一切用例的 ansys_bin 都指向 tmp_path 内生成的脚本,绝不触真 MAPDL。
另含 results.extract_error_lines 单元测试(自 test_mesh.py 迁入:解析函数
收敛后仅存 job.out 错误行提取与 results.csv 解析,不再依赖网格/模板)。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ansys_hip.registry import KernelError
from ansys_hip.results import extract_error_lines
from ansys_hip.runner import (
    DIAGNOSTIC_HEAD_BYTES,
    DIAGNOSTIC_TAIL_BYTES,
    _read_text,
    cancel_job,
    interactive_available,
    run_mapdl,
)
from ansys_hip.schemas import RunContext

# 等 cancel_job 生效的最大轮次(每轮 50ms,共 10s,留足进程组就位余量)
CANCEL_POLL_ROUNDS = 200
CANCEL_POLL_INTERVAL_S = 0.05


def _make_fake_ansys(bin_path: Path, body: str) -> None:
    """生成假 ansys221:解析 -o 实参定位输出文件,再执行注入的 body 片段。"""
    script_lines = [
        "#!/usr/bin/env bash",
        'out="job.out"',
        "while [ $# -gt 0 ]; do",
        '  case "$1" in',
        '    -o) out="$2"; shift 2 ;;',
        "    *) shift ;;",
        "  esac",
        "done",
        body,
        "",
    ]
    bin_path.write_text("\n".join(script_lines))
    bin_path.chmod(0o755)


def _make_ctx(job_dir: Path, bin_path: Path, timeout_s: int = 30) -> RunContext:
    """构造最小 RunContext(license 留空 → runner 不注入许可环境变量)。"""
    return RunContext(
        job_dir=job_dir,
        ansys_bin=str(bin_path),
        license_file="",
        ansys_np=2,
        job_timeout_s=timeout_s,
    )


@pytest.fixture()
def job_dir(tmp_path: Path) -> Path:
    """作业目录:内含占位 inp(runner 只检查其存在)。"""
    directory = tmp_path / "job"
    directory.mkdir()
    (directory / "job.inp").write_text("/PREP7\n/SOLU\nSOLVE\n")
    return directory


def test_success_writes_outputs_and_returns_metrics(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 假二进制正常结束(默认 required_outputs=None,不检查产出文件)
    bin_path = tmp_path / "ansys_ok"
    _make_fake_ansys(
        bin_path,
        'echo "MAPDL STRUCTURAL VERSION 22.1" > "$out"\n'
        'echo "SOLUTION IS DONE" >> "$out"',
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act
    result = run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-ok")

    # Assert
    assert result["returncode"] == 0
    assert result["elapsed_s"] >= 0.0
    assert Path(result["out_path"]) == job_dir / "job.out"


def test_error_line_in_out_raises_convergence_failed(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 退出码 0 但 job.out 含 ERROR 行(真 MAPDL 常见形态)
    bin_path = tmp_path / "ansys_err"
    _make_fake_ansys(
        bin_path,
        'echo "*** ERROR *** CP = 1 TIME = 0.5 ELEMENT 5 NOT CONVERGED" > "$out"',
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-err")
    assert excinfo.value.code == "CONVERGENCE_FAILED"
    assert "ELEMENT 5" in excinfo.value.message


def test_banner_only_out_with_exit_zero_succeeds(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — job.out 为 v252 干净运行的完整形态:启动横幅 + 结尾零错误统计,退出码 0
    # (回归钉:横幅曾以裸 "ERROR" 子串被误判,导致退出码 0 的干净作业整批误报)
    bin_path = tmp_path / "ansys_banner"
    _make_fake_ansys(
        bin_path,
        'echo "     Opening new LOG, ERROR, LOCK and PAGE FILES" > "$out"\n'
        'echo "NUMBER OF ERROR   MESSAGES ENCOUNTERED=          0" >> "$out"',
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act
    result = run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-banner")

    # Assert — 横幅含 "ERROR" 字样但非错误行:退出码 0 且无真错误标记 → 成功
    assert result["returncode"] == 0


def test_huge_out_tail_error_still_diagnosed(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 输出超过诊断读首尾预算(2MB+14MB)且真错误行只在最尾部:
    # 有界读必须覆盖尾部(NERR 失控事故现场真错误聚集在末尾 1% 内)
    bin_path = tmp_path / "ansys_big"
    _make_fake_ansys(
        bin_path,
        'yes "WARNING filler line for huge output" | head -c 18000000 > "$out"\n'
        'echo "*** ERROR *** CP = 9 TIME = 1 TAIL-MARKER-5221" >> "$out"',
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-big")
    assert excinfo.value.code == "CONVERGENCE_FAILED"
    assert "TAIL-MARKER-5221" in excinfo.value.message


def test_read_text_bounds_huge_file(tmp_path: Path) -> None:
    # 338MB 事故现场的内存护栏:超预算文件只取首尾(含省略标记行),中部不进内存
    out_path = tmp_path / "job.out"
    filler = "0123456789abcdef" * 64 + "\n"  # 1KB/行
    with out_path.open("w") as handle:
        handle.write("HEAD-MARKER-9021\n")
        for _ in range(17 * 1024):  # ~17MB,超过 16MB 首尾预算
            handle.write(filler)
        handle.write("TAIL-MARKER-5221\n")

    text = _read_text(out_path)

    budget = DIAGNOSTIC_HEAD_BYTES + DIAGNOSTIC_TAIL_BYTES
    assert len(text) <= budget + 200  # 首尾 + 省略标记行
    assert "HEAD-MARKER-9021" in text
    assert "TAIL-MARKER-5221" in text


def test_nonzero_exit_without_error_line_raises_convergence_failed(
    job_dir: Path, tmp_path: Path
) -> None:
    # Arrange — 无 ERROR 行但退出码非零
    bin_path = tmp_path / "ansys_crash"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"\nexit 137')
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-crash")
    assert excinfo.value.code == "CONVERGENCE_FAILED"
    assert "137" in excinfo.value.message


def test_license_error_line_raises_license_unavailable(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 许可失败(诊断阶梯中许可先于 ERROR 行判定)
    bin_path = tmp_path / "ansys_lic"
    _make_fake_ansys(
        bin_path,
        'echo "LICENSE MANAGER ERROR -5: checkout failed" > "$out"',
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-lic")
    assert excinfo.value.code == "LICENSE_UNAVAILABLE"
    assert "LICENSE MANAGER ERROR" in excinfo.value.message


def test_default_required_outputs_none_succeeds_without_outputs(
    job_dir: Path, tmp_path: Path
) -> None:
    # Arrange — 正常结束且不产出任何结果文件
    bin_path = tmp_path / "ansys_nosum"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"')
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert — 默认 required_outputs=None:直通通道由内核按 declared_outputs
    # 自行判定产出,runner 不再强制 summary.csv(类型化方法契约已随方法库移除)
    result = run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-default")
    assert result["returncode"] == 0

    # 显式传清单则恢复缺件 INTERNAL(能力保留,供需要强契约的调用方使用)
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(
            job_dir / "job.inp", job_dir, ctx, "job-explicit",
            required_outputs=("summary.csv",),
        )
    assert excinfo.value.code == "INTERNAL"
    assert "summary.csv" in excinfo.value.message


def test_timeout_kills_process_group(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 长作业 + 1s 超时
    bin_path = tmp_path / "ansys_slow"
    _make_fake_ansys(bin_path, 'echo "start" > "$out"\nsleep 10')
    ctx = _make_ctx(job_dir, bin_path, timeout_s=1)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-timeout")
    assert excinfo.value.code == "TIMEOUT"
    assert "超时" in excinfo.value.message


def test_cancel_job_kills_running_process(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 后台线程跑长作业,主线程 cancel_job 终止进程组
    bin_path = tmp_path / "ansys_cancel"
    _make_fake_ansys(bin_path, 'echo "start" > "$out"\nsleep 30')
    ctx = _make_ctx(job_dir, bin_path, timeout_s=60)

    with ThreadPoolExecutor(max_workers=1) as pool:
        # Act
        future = pool.submit(run_mapdl, job_dir / "job.inp", job_dir, ctx, "job-cancel")
        is_cancelled = False
        for _ in range(CANCEL_POLL_ROUNDS):
            if cancel_job("job-cancel"):
                is_cancelled = True
                break
            time.sleep(CANCEL_POLL_INTERVAL_S)
        assert is_cancelled, "cancel_job 未找到运行中的作业进程"

        # Assert — 被取消的作业以 TIMEOUT 码失败,消息注明取消
        with pytest.raises(KernelError) as excinfo:
            future.result()
    assert excinfo.value.code == "TIMEOUT"
    assert "取消" in excinfo.value.message


def test_missing_binary_raises_mapdl_not_found(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — ansys_bin 指向不存在的路径
    ctx = _make_ctx(job_dir, tmp_path / "no_such_ansys")

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-missing")
    assert excinfo.value.code == "MAPDL_NOT_FOUND"


def test_cancel_unknown_job_is_false() -> None:
    # Act / Assert — 未运行/已结束的 job_id 幂等返回 False
    assert cancel_job("never-ran-job") is False


def test_interactive_available_returns_bool() -> None:
    # Act / Assert — PyMAPDL 未安装时 False,已安装 True;仅要求不抛异常
    assert isinstance(interactive_available(), bool)


# ---------------------------------------------------------------------------
# results.extract_error_lines 单元(自 test_mesh.py 迁入)
# ---------------------------------------------------------------------------

def test_extract_error_lines() -> None:
    out = "\n".join([
        "     Opening new LOG, ERROR, LOCK and PAGE FILES",  # 启动横幅(指 .err 文件),非错误
        "   *** ERROR ***  CP = 1.2",
        "normal line",
        "*** FATAL *** terminated",
        " This could invalidate error estimation.",  # 警告散文中的小写 error,不是错误行
        "NUMBER OF ERROR MESSAGES ENCOUNTERED= 0",  # 结尾统计行,不含错误标记
        "The number of ERROR and WARNING messages exceeds 200.",  # 警告超量提示,不含错误标记
    ])
    lines = extract_error_lines(out)
    assert len(lines) == 2
    assert lines[0].startswith("*** ERROR ***")
    assert lines[1].startswith("*** FATAL ***")


def test_extract_error_lines_ignores_startup_banner() -> None:
    # 回归钉:启动横幅含 "ERROR" 字样(每个 job.out 头部都有),曾以裸子串匹配
    # 被误判为错误行,导致退出码 0 的干净作业整批误报 CONVERGENCE_FAILED
    out = "\n".join([
        "     Opening new LOG, ERROR, LOCK and PAGE FILES",
        "NUMBER OF ERROR   MESSAGES ENCOUNTERED=          0",
        "The number of ERROR and WARNING messages exceeds 10000.",
        "  Use the /NERR command to increase the numberof messages.",
        " The ANSYS run is terminated by this error.",
    ])
    assert extract_error_lines(out) == []


def test_extract_error_lines_capped() -> None:
    out = "\n".join(f"*** ERROR *** {i}" for i in range(60))
    assert len(extract_error_lines(out)) == 40
