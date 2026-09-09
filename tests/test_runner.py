"""runner 单测 — 假 ansys221(bash 脚本)覆盖成功/失败/许可/超时/取消/缺二进制路径。

一切用例的 ansys_bin 都指向 tmp_path 内生成的脚本,绝不触真 MAPDL。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ansys_hip.registry import KernelError
from ansys_hip.runner import cancel_job, interactive_available, run_mapdl
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
    # Arrange — 假二进制正常结束并产出 summary.csv
    bin_path = tmp_path / "ansys_ok"
    _make_fake_ansys(
        bin_path,
        'echo "MAPDL STRUCTURAL VERSION 22.1" > "$out"\n'
        'echo "SOLUTION IS DONE" >> "$out"\n'
        "touch summary.csv",
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act
    result = run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-ok")

    # Assert
    assert result["returncode"] == 0
    assert result["elapsed_s"] >= 0.0
    assert Path(result["out_path"]) == job_dir / "job.out"
    assert (job_dir / "summary.csv").is_file()


def test_error_line_in_out_raises_convergence_failed(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 退出码 0 但 job.out 含 ERROR 行(真 MAPDL 常见形态)
    bin_path = tmp_path / "ansys_err"
    _make_fake_ansys(
        bin_path,
        'echo "*** ERROR *** CP = 1 TIME = 0.5 ELEMENT 5 NOT CONVERGED" > "$out"\n'
        "touch summary.csv",
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-err")
    assert excinfo.value.code == "CONVERGENCE_FAILED"
    assert "ELEMENT 5" in excinfo.value.message


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
    # Arrange — 许可失败(且即便 summary 存在也优先判许可)
    bin_path = tmp_path / "ansys_lic"
    _make_fake_ansys(
        bin_path,
        'echo "LICENSE MANAGER ERROR -5: checkout failed" > "$out"\n'
        "touch summary.csv",
    )
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-lic")
    assert excinfo.value.code == "LICENSE_UNAVAILABLE"
    assert "LICENSE MANAGER ERROR" in excinfo.value.message


def test_normal_exit_without_summary_raises_internal(job_dir: Path, tmp_path: Path) -> None:
    # Arrange — 正常结束但没产出 summary.csv(模板结果写出环节失效)
    bin_path = tmp_path / "ansys_nosum"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"')
    ctx = _make_ctx(job_dir, bin_path)

    # Act / Assert
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(job_dir / "job.inp", job_dir, ctx, "job-nosum")
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
