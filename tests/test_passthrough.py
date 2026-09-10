"""passthrough 直通通道测试 — 配置开关 / APDL 上传 / 参数校验 / 内核 / 路由注册.

覆盖计划 Step 1→4:开关(settings + service.yaml + /health + 启动告警)、
POST /uploads/apdl(上传唯一通道)、PassthroughParams 边界(保留名/裸文件名/
去重/上限)、kernels/passthrough.py(复制→runner→发布→results.csv)、
runner required_outputs 参数化(默认 None 跳过缺件检查,新默认由
tests/test_runner.py 钉住,此处补显式清单语义)、注册表 passthrough 单方法
与手写 POST /sim/passthrough、HIPForm AnsysClient 同形兼容。
全部用假 MAPDL 脚本或假内核,不触真 ansys221。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ansys_hip import api as api_module
from ansys_hip import registry
from ansys_hip.api import ApiError, _submit, create_app
from ansys_hip.kernels.passthrough import run_passthrough
from ansys_hip.queue import _sweep_expired_uploads
from ansys_hip.registry import KernelError
from ansys_hip.results import PROGRESS_FILENAME, RESULTS_FILENAME, parse_results_csv
from ansys_hip.runner import run_mapdl
from ansys_hip.schemas import PassthroughParams, RunContext
from ansys_hip.settings import PassthroughConfig, Settings, load_settings

# 复用既有测试工具(tests/ 在 sys.path;假 ansys 脚本构造器与提交/轮询助手)
from test_api_core import REAL_CONFIG_PATH, read_resolved_params, submit, wait_for_terminal
from test_runner import _make_fake_ansys

# 保留名钉测的期望全集(与 schemas.RESERVED_JOB_DIR_NAMES 同源断言,防散落漂移)
EXPECTED_RESERVED_JOB_DIR_NAMES = {
    "state.json", "resolved-params.json", "result.json", "job.log",
    "job.out", "launcher.log", "progress.csv", "artifacts",
}


# ---------------------------------------------------------------------------
# 工具:开启开关的 Settings / 客户端 / APDL 上传
# ---------------------------------------------------------------------------

def enabled_settings(settings_factory) -> Settings:
    """默认测试 Settings + passthrough 开关开启(model_copy 生成新实例)。"""
    return settings_factory().model_copy(
        update={"passthrough": PassthroughConfig(enabled=True)}
    )


def upload_apdl(client: TestClient, name: str, content: bytes = b"/PREP7\n") -> str:
    """经 POST /uploads/apdl 上传并返回服务端路径(即 entry_file/extra_files 取值)。"""
    response = client.post(
        "/uploads/apdl",
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()["path"]


# ---------------------------------------------------------------------------
# Step 1:配置开关
# ---------------------------------------------------------------------------

def test_real_config_ships_passthrough_disabled_by_default() -> None:
    """config/service.yaml 带 passthrough 节且默认关闭(安全前提:默认不开)。"""
    settings = load_settings(REAL_CONFIG_PATH)
    assert settings.passthrough.enabled is False


def test_passthrough_config_rejects_unknown_keys() -> None:
    """frozen + extra=forbid:配置键拼错启动即暴露。"""
    with pytest.raises(ValidationError):
        PassthroughConfig.model_validate({"enabled": False, "oops": True})


def test_env_override_enables_passthrough(monkeypatch) -> None:
    """HIP_SERVICE_PASSTHROUGH_ENABLED 沿用既有环境变量覆盖机制。"""
    monkeypatch.setenv("HIP_SERVICE_PASSTHROUGH_ENABLED", "true")
    settings = load_settings(REAL_CONFIG_PATH)
    assert settings.passthrough.enabled is True


def test_health_reports_passthrough_disabled_by_default(client: TestClient) -> None:
    """/health 新增 passthrough_enabled 字段(可观测;默认 False)。"""
    report = client.get("/health").json()
    assert report["passthrough_enabled"] is False


def test_health_reflects_enabled_switch(settings_factory) -> None:
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["passthrough_enabled"] is True


def test_startup_warns_when_passthrough_enabled(settings_factory, caplog) -> None:
    """开启时启动日志显著标注任意 APDL 执行面(安全前提落日志)。"""
    settings = enabled_settings(settings_factory)
    with caplog.at_level(logging.WARNING, logger="ansys_hip.api"):
        with TestClient(create_app(settings)):
            pass
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "passthrough" in record.message
    ]
    assert warnings, "开启 passthrough 时启动应输出 WARNING 日志"
    assert any("APDL" in record.message for record in warnings)


def test_startup_silent_when_passthrough_disabled(settings_factory, caplog) -> None:
    settings = settings_factory()
    with caplog.at_level(logging.WARNING, logger="ansys_hip.api"):
        with TestClient(create_app(settings)):
            pass
    assert not [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "passthrough" in record.message
    ]


# ---------------------------------------------------------------------------
# Step 2a:POST /uploads/apdl
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", [".inp", ".cdb", ".mac", ".csv", ".txt"])
def test_upload_apdl_accepts_whitelisted_suffixes(
    settings_factory, suffix: str
) -> None:
    """后缀白名单五类全收;响应复用 UploadAccepted,落盘进 uploads 根。"""
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        payload = b"/PREP7\nSOLVE\n"
        response = client.post(
            "/uploads/apdl",
            files={"file": (f"job{suffix}", payload, "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["path"].endswith(f"_job{suffix}")
        assert body["size_bytes"] == len(payload)
        assert Path(body["path"]).parent == settings.uploads_root
        assert Path(body["path"]).is_file()


def test_upload_apdl_rejected_when_passthrough_disabled(client: TestClient) -> None:
    """开关关闭:上传与提交同受门控(403 PASSTHROUGH_DISABLED,不留旁路上传面)。"""
    response = client.post(
        "/uploads/apdl",
        files={"file": ("job.inp", b"/PREP7\n", "application/octet-stream")},
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PASSTHROUGH_DISABLED"


def test_upload_apdl_rejects_geometry_and_unknown_suffixes(settings_factory) -> None:
    """几何/未知后缀在 APDL 通道拒绝(直通通道只收 MAPDL 文本类输入)。"""
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        for filename in ("m.step", "solver.exe", "noext"):
            response = client.post(
                "/uploads/apdl",
                files={"file": (filename, b"x", "application/octet-stream")},
            )
            assert response.status_code == 400, filename
            assert response.json()["code"] == "INVALID_PARAMS"


def test_upload_apdl_rejects_oversize(settings_factory, monkeypatch) -> None:
    """超过单文件上限 → 413 PAYLOAD_TOO_LARGE;半写文件即清,不留守卫磁盘。"""
    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 8)
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/uploads/apdl",
            files={"file": ("big.inp", b"x" * 64, "application/octet-stream")},
        )
        assert response.status_code == 413, response.text
        assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
        assert not list(settings.uploads_root.glob("*_big.inp"))


def test_accept_upload_rejects_control_characters_in_filename(tmp_path: Path) -> None:
    """文件名含 NUL 等控制字符 → 400 而非落盘时 ValueError → 500。

    直调 _accept_upload(HTTP 头本身不允许控制字符,此路仅 ASGI 层异常构造可达)。
    """
    class _FakeUpload:  # 最小鸭子类型:校验只读 filename,不触文件句柄
        filename = "evil\x00.inp"

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(
            api_module._accept_upload(
                _FakeUpload(), tmp_path, api_module.APDL_UPLOAD_SUFFIXES, kind="APDL"
            )
        )
    assert excinfo.value.status_code == 400


def test_sweep_expired_uploads_removes_only_stale(tmp_path: Path) -> None:
    """超期上传按 mtime 清扫;新文件保留;目录缺失静默返回 0。"""
    stale = tmp_path / "old_job.inp"
    fresh = tmp_path / "new_job.inp"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).timestamp()
    os.utime(stale, (old_ts, old_ts))

    assert _sweep_expired_uploads(tmp_path, retention_days=3) == 1
    assert not stale.exists()
    assert fresh.is_file()
    assert _sweep_expired_uploads(tmp_path / "missing", retention_days=3) == 0


# ---------------------------------------------------------------------------
# Step 2b:PassthroughParams 边界校验
# ---------------------------------------------------------------------------

def _post_passthrough(client: TestClient, params: dict[str, Any]):
    return client.post("/sim/passthrough", json={"params": params})


def test_declared_outputs_must_be_nonempty(settings_factory, fake_executors) -> None:
    """空清单无意义(服务据此发布工件)→ 400 INVALID_PARAMS。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(
            client,
            {"entry_file": "/var/uploads/x_job.inp", "declared_outputs": []},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "declared_outputs" in body["message"]


def test_declared_outputs_capped_at_64(settings_factory, fake_executors) -> None:
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(
            client,
            {"entry_file": "/var/uploads/x_job.inp",
             "declared_outputs": [f"out_{i}.csv" for i in range(65)]},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"


def test_declared_outputs_overlap_with_inputs_rejected(settings_factory) -> None:
    """declared 与入口的服务端 basename 同名 → 400(输入复制进根部会自我满足
    缺件检查,零产出也算成功;上传名带 token 前缀,按落盘名判)。"""
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        entry = upload_apdl(client, "demo.inp")
        basename = Path(entry).name
        response = _post_passthrough(
            client,
            {"entry_file": entry, "declared_outputs": [basename]},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert basename in body["message"]


def test_declared_outputs_deduplicated_preserving_order(
    settings_factory, fake_executors
) -> None:
    """重复声明去重(保序),resolved-params.json 落盘归一结果。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        entry = upload_apdl(client, "job.inp")
        accepted = submit(
            client,
            "passthrough",
            {"params": {
                "entry_file": entry,
                "declared_outputs": ["a.csv", "b.csv", "a.csv"],
            }},
        )
        state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state
    resolved = read_resolved_params(settings, accepted["id"])
    assert resolved["declared_outputs"] == ["a.csv", "b.csv"]


def test_reserved_job_dir_names_constant_pinned() -> None:
    """保留名常量钉测:根部簿记文件 + progress.csv 侧车 + artifacts 目录,
    增删条目须显式改本断言(防静默漂移破坏 queue/T2 读时投影)。"""
    from ansys_hip.schemas import RESERVED_JOB_DIR_NAMES

    assert RESERVED_JOB_DIR_NAMES == EXPECTED_RESERVED_JOB_DIR_NAMES


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_file", "/var/uploads/state.json"),           # entry basename 撞保留名
        ("extra_files", ["/var/uploads/launcher.log"]),      # extra basename 撞保留名
        ("declared_outputs", ["job.out"]),                   # 声明产出撞保留名
        ("declared_outputs", ["progress.csv"]),              # T2 阶段进度侧车
        ("declared_outputs", ["artifacts"]),                 # 工件目录名
    ],
)
def test_reserved_names_rejected(settings_factory, fake_executors, field, value) -> None:
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    params: dict[str, Any] = {
        "entry_file": "/var/uploads/x_job.inp",
        "declared_outputs": ["results.csv"],
    }
    params[field] = value
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(client, params)
    assert response.status_code == 400, (field, value)
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "保留名" in body["message"]


@pytest.mark.parametrize(
    "bad_name",
    ["sub/dir.csv", "a\\b.txt", "..", ".", "bad\x07name", ""],
)
def test_declared_outputs_bare_filename_rules(
    settings_factory, fake_executors, bad_name
) -> None:
    """裸文件名沿用 uploads 消毒风格:路径分隔符/'..'/不可打印字符即拒。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(
            client,
            {"entry_file": "/var/uploads/x_job.inp",
             "declared_outputs": [bad_name or "x.csv"] if bad_name else []},
        )
    # 空串走 min_length、其余走裸文件名校验,均为 400
    if bad_name:
        assert response.status_code == 400, bad_name
        assert response.json()["code"] == "INVALID_PARAMS"


def test_timeout_must_be_positive(settings_factory, fake_executors) -> None:
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(
            client,
            {"entry_file": "/var/uploads/x_job.inp",
             "declared_outputs": ["results.csv"], "timeout_s": 0},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"


def test_workflow_field_echoed_in_resolved_params(
    settings_factory, fake_executors
) -> None:
    """workflow 纯溯源:落 resolved-params.json 回显,服务不据此分支。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        entry = upload_apdl(client, "job.inp")
        accepted = submit(
            client,
            "passthrough",
            {"params": {
                "entry_file": entry,
                "declared_outputs": ["results.csv"],
                "workflow": "hip-demo/1.2",
            }},
        )
        state = wait_for_terminal(client, accepted["id"])
    assert state["status"] == "succeeded", state
    resolved = read_resolved_params(settings, accepted["id"])
    assert resolved["workflow"] == "hip-demo/1.2"


def test_unknown_param_field_rejected(settings_factory, fake_executors) -> None:
    """extra=forbid 沿袭方法参数模型口径:拼错字段名不被静默吞掉。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(
            client,
            {"entry_file": "/var/uploads/x_job.inp",
             "declared_outputs": ["results.csv"], "entry_files": "oops"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "entry_files" in body["message"]


# ---------------------------------------------------------------------------
# Step 4:提交开关(类型化手写路由 + 泛化兜底两路)
# ---------------------------------------------------------------------------

def test_submit_rejected_403_when_disabled(client: TestClient, fake_executors) -> None:
    """关闭 → 403 PASSTHROUGH_DISABLED(区别于 404 端点不存在与 503 方法下线)。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    response = _post_passthrough(
        client,
        {"entry_file": "/var/uploads/x_job.inp", "declared_outputs": ["results.csv"]},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "PASSTHROUGH_DISABLED"
    assert "passthrough" in body["message"]


def test_submit_generic_path_rejected_403_when_disabled(
    settings: Settings, fake_executors
) -> None:
    """泛化路径直调 _submit(手写路由先注册截获已知方法,直调即泛化处理器
    的请求处理体)— 两路共用同一条管线,开关检查同样生效。"""
    app = create_app(settings)  # 不进 lifespan:403 早返回,队列零接触
    spec = registry.REGISTRY[registry.PASSTHROUGH_METHOD]
    with pytest.raises(ApiError) as excinfo:
        _submit(
            spec,
            {"entry_file": "/var/uploads/x_job.inp", "declared_outputs": ["results.csv"]},
            settings, app.state.queue,
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "PASSTHROUGH_DISABLED"


def test_submit_full_lifecycle_when_enabled(settings_factory, fake_executors) -> None:
    """开启 → 上传→提交(202)→终态 succeeded;受理响应字段形态与既有方法一致。"""
    fake_executors["passthrough"] = lambda params, ctx: {
        "fidelity": "passthrough", "artifacts": ["job.out"],
    }
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        entry = upload_apdl(client, "job.inp")
        accepted = submit(
            client,
            "passthrough",
            {"params": {"entry_file": entry, "declared_outputs": ["results.csv"]}},
        )
        state = wait_for_terminal(client, accepted["id"])
    assert accepted["method"] == "passthrough"
    assert accepted["status_url"] == f"/jobs/{accepted['id']}"
    assert state["status"] == "succeeded", state
    assert state["method"] == "passthrough"


def test_disabled_switch_wins_over_other_payloads(client: TestClient) -> None:
    """开关关闭时,本会被后续校验拒绝的载荷也直接 403(策略闸门先于业务校验)。

    注:pydantic 请求体校验在框架层先于 handler(schema 坏体 → 400,与既有
    12 个类型化路由行为一致);此处载荷 schema 合法但 entry 指向上传目录外 —
    开关开启时会是 400,关闭时须是 403。"""
    sneaky = {
        "params": {
            "entry_file": "/etc/passwd",
            "declared_outputs": ["results.csv"],
        }
    }
    for payload in ({}, sneaky):
        response = client.post("/sim/passthrough", json=payload)
        assert response.status_code == 403, payload
        assert response.json()["code"] == "PASSTHROUGH_DISABLED"


# ---------------------------------------------------------------------------
# Step 2c:entry/extra 必须位于 uploads 根(经 /uploads/apdl 上传)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_file", "/etc/passwd"),
        ("extra_files", ["/home/yushen/opt/secret.cdb"]),
    ],
)
def test_files_outside_uploads_rejected(
    settings_factory, fake_executors, field, value
) -> None:
    """entry/extra 会被复制进 job_dir 并作为工件发布 → 必须限定在上传目录内,
    防任意路径读取经工件下载外泄。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    params: dict[str, Any] = {
        "entry_file": "/var/uploads/x_job.inp",
        "declared_outputs": ["results.csv"],
    }
    params[field] = value
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        response = _post_passthrough(client, params)
    assert response.status_code == 400, (field, value)
    body = response.json()
    assert body["code"] == "INVALID_PARAMS"
    assert "上传目录" in body["message"]


# ---------------------------------------------------------------------------
# Step 3a:runner required_outputs 参数化(新默认 None 由 test_runner.py 钉住)
# ---------------------------------------------------------------------------

def _make_job_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "job"
    directory.mkdir()
    (directory / "job.inp").write_text("/PREP7\n/SOLU\nSOLVE\n", encoding="utf-8")
    return directory


def _ctx(job_dir: Path, bin_path: Path, timeout_s: int = 30) -> RunContext:
    return RunContext(
        job_dir=job_dir, ansys_bin=str(bin_path), license_file="",
        ansys_np=2, job_timeout_s=timeout_s,
    )


def test_runner_required_outputs_none_skips_summary_check(tmp_path: Path) -> None:
    """passthrough 传 None:正常结束但无 summary.csv 不再构成 INTERNAL。"""
    job_dir = _make_job_dir(tmp_path)
    bin_path = tmp_path / "ansys_ok"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"')
    result = run_mapdl(
        job_dir / "job.inp", job_dir, _ctx(job_dir, bin_path), "pt-none",
        required_outputs=None,
    )
    assert result["returncode"] == 0


def test_runner_explicit_required_outputs_restores_internal(tmp_path: Path) -> None:
    """默认不传(= None)正常结束即成功;显式传清单则恢复缺件 INTERNAL
    (类型化方法的 summary.csv 契约已移除,显式清单留作强契约能力)。"""
    job_dir = _make_job_dir(tmp_path)
    bin_path = tmp_path / "ansys_nosum"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"')
    ctx = _ctx(job_dir, bin_path)

    result = run_mapdl(job_dir / "job.inp", job_dir, ctx, "pt-default")
    assert result["returncode"] == 0

    with pytest.raises(KernelError) as excinfo:
        run_mapdl(
            job_dir / "job.inp", job_dir, ctx, "pt-explicit",
            required_outputs=("summary.csv",),
        )
    assert excinfo.value.code == "INTERNAL"
    assert "summary.csv" in excinfo.value.message


def test_runner_custom_required_outputs_enforced(tmp_path: Path) -> None:
    """自定义清单:summary.csv 在而声明文件缺席 → INTERNAL 点名缺失文件。"""
    job_dir = _make_job_dir(tmp_path)
    bin_path = tmp_path / "ansys_partial"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"\ntouch summary.csv')
    with pytest.raises(KernelError) as excinfo:
        run_mapdl(
            job_dir / "job.inp", job_dir, _ctx(job_dir, bin_path), "pt-custom",
            required_outputs=("summary.csv", "declared.txt"),
        )
    assert excinfo.value.code == "INTERNAL"
    assert "declared.txt" in excinfo.value.message


# ---------------------------------------------------------------------------
# Step 3b:kernels/passthrough.py(run_passthrough 直调,假 ansys 脚本)
# ---------------------------------------------------------------------------

def _kernel_ctx(job_dir: Path, bin_path: Path, timeout_s: int = 60) -> RunContext:
    return RunContext(
        job_dir=job_dir, ansys_bin=str(bin_path), license_file="",
        ansys_np=2, job_timeout_s=timeout_s,
    )


def _kernel_params(
    entry: Path,
    declared: list[str],
    extra: list[Path] | None = None,
    timeout_s: int | None = None,
) -> PassthroughParams:
    return PassthroughParams(
        entry_file=str(entry),
        extra_files=[str(path) for path in (extra or [])],
        declared_outputs=declared,
        timeout_s=timeout_s,
    )


def test_run_passthrough_success_publishes_and_parses(tmp_path: Path) -> None:
    """成功路径:entry+extra 原名复制进 job_dir 根 → 执行 → 发布
    (entry/job.out/progress.csv/results.csv/声明产出)→ values 解析进结果 dict。"""
    job_dir = _make_job_dir(tmp_path)
    source_dir = tmp_path / "uploads"
    source_dir.mkdir()
    entry = source_dir / "tok_job.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    extra = source_dir / "tok_capsule.cdb"
    extra.write_text("MAPDL cdb commands\n", encoding="utf-8")
    bin_path = tmp_path / "ansys_pt"
    _make_fake_ansys(
        bin_path,
        'echo "MAPDL STRUCTURAL VERSION 22.1" > "$out"\n'
        'echo "SOLUTION IS DONE" >> "$out"\n'
        "printf 'key,value\\nfinal_den,0.97\\nmax_sig,120.5\\n' > results.csv\n"
        "printf 'MESH,0.0\\nSEG_P10,600.0\\n' > progress.csv\n"
        "touch final.cdb\n",
    )

    result = run_passthrough(
        _kernel_params(entry, ["final.cdb"], extra=[extra]),
        _kernel_ctx(job_dir, bin_path),
    )

    # 结果 dict 契约
    assert result["fidelity"] == "passthrough"
    assert result["returncode"] == 0
    assert result["elapsed_s"] >= 0.0
    assert result["values"] == {"final_den": pytest.approx(0.97),
                                "max_sig": pytest.approx(120.5)}
    assert set(result["artifacts"]) == {
        "tok_job.inp", "job.out", PROGRESS_FILENAME, RESULTS_FILENAME, "final.cdb",
    }
    # MAPDL cwd=job_dir:entry/extra 按原名复制进根部,相对引用自然解析
    assert (job_dir / "tok_job.inp").is_file()
    assert (job_dir / "tok_capsule.cdb").is_file()
    # 发布 = 复制进 artifacts/ 供下载端点服务
    for name in result["artifacts"]:
        assert (job_dir / "artifacts" / name).is_file(), name


def test_run_passthrough_failure_still_publishes_job_out(tmp_path: Path) -> None:
    """MAPDL 非零退出 → CONVERGENCE_FAILED;入口与 job.out 仍发布供上游排查。"""
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "fail.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    bin_path = tmp_path / "ansys_fail"
    _make_fake_ansys(
        bin_path,
        'echo " *** ERROR *** nonlinear convergence failed" > "$out"\nexit 1\n',
    )

    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(entry, ["out.csv"]), _kernel_ctx(job_dir, bin_path)
        )

    assert excinfo.value.code == "CONVERGENCE_FAILED"
    artifacts_dir = job_dir / "artifacts"
    assert (artifacts_dir / "job.out").is_file()
    assert (artifacts_dir / "fail.inp").is_file()


def test_run_passthrough_without_results_csv_omits_values(tmp_path: Path) -> None:
    """results.csv 缺席 → 结果 dict 无 values 字段(可选契约,缺席不算错)。"""
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "job2.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    bin_path = tmp_path / "ansys_pt2"
    _make_fake_ansys(
        bin_path,
        'echo "SOLUTION IS DONE" > "$out"\ntouch out.txt\n',
    )
    result = run_passthrough(
        _kernel_params(entry, ["out.txt"]), _kernel_ctx(job_dir, bin_path)
    )
    assert "values" not in result
    assert "results.csv" not in result["artifacts"]
    assert parse_results_csv(job_dir) is None


def test_parse_results_csv_tolerates_torn_lines(tmp_path: Path) -> None:
    """撕裂容忍:半行/坏行/表头跳过,不抛异常(写出与读取可能并发)。"""
    job_dir = tmp_path / "torn"
    job_dir.mkdir()
    (job_dir / RESULTS_FILENAME).write_text(
        "key,value\na,1\nbroken-line\nb,\nc,2.5\n", encoding="utf-8"
    )
    assert parse_results_csv(job_dir) == {"a": pytest.approx(1.0),
                                          "c": pytest.approx(2.5)}


def test_run_passthrough_missing_entry_rejected(tmp_path: Path) -> None:
    job_dir = _make_job_dir(tmp_path)
    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(tmp_path / "nope.inp", ["out.csv"]),
            _kernel_ctx(job_dir, tmp_path / "ansys_x"),
        )
    assert excinfo.value.code == "INVALID_PARAMS"
    assert "entry" in excinfo.value.message or "入口" in excinfo.value.message


def test_run_passthrough_empty_entry_rejected(tmp_path: Path) -> None:
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "empty.inp"
    entry.write_text("", encoding="utf-8")
    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(entry, ["out.csv"]), _kernel_ctx(job_dir, tmp_path / "ansys_x")
        )
    assert excinfo.value.code == "INVALID_PARAMS"


def test_run_passthrough_missing_extra_rejected(tmp_path: Path) -> None:
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "ok.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(entry, ["out.csv"], extra=[tmp_path / "gone.cdb"]),
            _kernel_ctx(job_dir, tmp_path / "ansys_x"),
        )
    assert excinfo.value.code == "INVALID_PARAMS"
    assert "附属" in excinfo.value.message


def test_run_passthrough_missing_declared_output_fails(tmp_path: Path) -> None:
    """声明输出缺失 → ARTIFACT_NOT_FOUND;job.out 仍无条件发布供上游排查。"""
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "job3.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    bin_path = tmp_path / "ansys_pt3"
    _make_fake_ansys(bin_path, 'echo "SOLUTION IS DONE" > "$out"\ntouch partial.csv\n')

    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(entry, ["partial.csv", "missing.csv"]),
            _kernel_ctx(job_dir, bin_path),
        )
    assert excinfo.value.code == "ARTIFACT_NOT_FOUND"
    assert "missing.csv" in excinfo.value.message
    assert (job_dir / "artifacts" / "job.out").is_file()
    assert (job_dir / "artifacts" / "partial.csv").is_file()  # 存在者照常发布


def test_run_passthrough_basename_collision_rejected(tmp_path: Path) -> None:
    """entry 与 extra 同名 → 拒绝(显式失败优于静默覆盖)。"""
    job_dir = _make_job_dir(tmp_path)
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir(); dir_b.mkdir()
    (dir_a / "same.inp").write_text("/PREP7\n", encoding="utf-8")
    (dir_b / "same.inp").write_text("/PREP7\n", encoding="utf-8")
    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(dir_a / "same.inp", ["out.csv"], extra=[dir_b / "same.inp"]),
            _kernel_ctx(job_dir, tmp_path / "ansys_x"),
        )
    assert excinfo.value.code == "INVALID_PARAMS"
    assert "冲突" in excinfo.value.message


def test_run_passthrough_timeout_clamped_to_user_value(tmp_path: Path) -> None:
    """timeout_s=1(全局上限 60)→ runner 以收紧后的 1s 超时终止进程组。"""
    job_dir = _make_job_dir(tmp_path)
    entry = tmp_path / "slow.inp"
    entry.write_text("/PREP7\n", encoding="utf-8")
    bin_path = tmp_path / "ansys_slow"
    _make_fake_ansys(bin_path, 'echo "start" > "$out"\nsleep 10')
    with pytest.raises(KernelError) as excinfo:
        run_passthrough(
            _kernel_params(entry, ["out.csv"], timeout_s=1),
            _kernel_ctx(job_dir, bin_path, timeout_s=60),
        )
    assert excinfo.value.code == "TIMEOUT"


# ---------------------------------------------------------------------------
# Step 4:注册表 / 路由 / Swagger / HIPForm 兼容
# ---------------------------------------------------------------------------

def test_registry_contains_passthrough_spec() -> None:
    spec = registry.REGISTRY["passthrough"]
    assert registry.PASSTHROUGH_METHOD == "passthrough"
    assert spec.group == "passthrough"
    assert spec.params_model is PassthroughParams
    assert "passthrough" in registry.GROUP_LABELS
    assert "run_passthrough" == "run_" + registry.PASSTHROUGH_METHOD.replace("-", "_")


def test_methods_endpoint_includes_passthrough(client: TestClient) -> None:
    """/sim/methods 顺序表同步:passthrough 带独立分组与 group_label。"""
    methods = client.get("/sim/methods").json()
    entry = {method["name"]: method for method in methods}["passthrough"]
    assert entry["group"] == "passthrough"
    assert entry["group_label"] == registry.GROUP_LABELS["passthrough"]
    assert entry["requires_mapdl"] is True
    assert entry["params_schema"]


def test_passthrough_route_shape_in_openapi(client: TestClient) -> None:
    """手写 POST /sim/passthrough:引用 PassthroughSimRequest{params},
    带示例与分组标签;PassthroughParams 字段 description 全覆盖。"""
    spec = client.app.openapi()
    operation = spec["paths"]["/sim/passthrough"]["post"]
    schema_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert schema_ref == "#/components/schemas/PassthroughSimRequest"

    wrapper = spec["components"]["schemas"]["PassthroughSimRequest"]
    params_ref = wrapper["properties"]["params"]["anyOf"][0]["$ref"]
    assert params_ref == "#/components/schemas/PassthroughParams"

    params_schema = spec["components"]["schemas"]["PassthroughParams"]
    assert params_schema["properties"], "PassthroughParams 应有字段"
    assert all(
        "description" in prop for prop in params_schema["properties"].values()
    ), "PassthroughParams 字段 description 未全覆盖"

    assert operation["requestBody"]["content"]["application/json"].get("examples")
    assert operation["tags"] == [registry.GROUP_LABELS["passthrough"]]
    # 403 文档化(开关关闭时的专用错误码)
    assert "403" in operation["responses"]


def test_ansys_client_request_shape_compatible(
    settings_factory, fake_executors
) -> None:
    """HIPForm AnsysClient.submit 只发 {"params": {...}} 且校验受理响应的
    id/method/status_url — 该请求/响应形状对 passthrough 零改动可用。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    with TestClient(create_app(enabled_settings(settings_factory))) as client:
        entry = upload_apdl(client, "client_job.inp")
        response = client.post(
            "/sim/passthrough",
            json={"params": {
                "entry_file": entry,
                "extra_files": [],
                "declared_outputs": ["results.csv"],
                "workflow": "wf-06-solve",
            }},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        for field in ("id", "method", "status_url"):
            assert isinstance(accepted[field], str) and accepted[field], field
        # get_job 形状:status 枚举与 id 回显
        state = client.get(f"/jobs/{accepted['id']}").json()
        assert state["id"] == accepted["id"]
        assert state["status"] in ("pending", "running", "succeeded", "failed", "cancelled")
        # get_result 形状:JSON dict
        terminal = wait_for_terminal(client, accepted["id"])
        assert terminal["status"] == "succeeded", terminal
        result = client.get(f"/jobs/{accepted['id']}/result").json()
        assert isinstance(result, dict)
