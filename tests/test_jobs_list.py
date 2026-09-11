"""作业列表与历史回退测试 — GET /jobs / 服务重启后历史可查 / log source 白名单.

覆盖:
    1. GET /jobs 列表(空 / 提交后出现 / 按受理时间倒序 / 内存与盘上历史合并);
    2. 历史回退(模拟重启:同 settings 二次 create_app → 列表/详情/日志/工件/
       结果全可用;历史 failed 的 /result 409 透传;DELETE 历史作业真清理目录;
       坏 state.json 跳过不 500);
    3. /jobs/{id}/log?source= 白名单(job.log 缺省向后兼容;根部 job.out 可读 + tail)。

全部用假内核(fake_executors)与手造历史目录,不运行 MAPDL。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ansys_hip.api import create_app
from ansys_hip.queue import JobQueue

# 复用既有测试工具(tests/ 在 sys.path;假内核/提交/轮询/开通道助手/终态常量)
from test_api_core import (
    TERMINAL_STATUSES,
    FakeKernel,
    passthrough_payload,
    submit,
    wait_for_terminal,
)
from test_passthrough import enabled_settings


# ---------------------------------------------------------------------------
# 工具:手造历史作业目录(服务重启后内存清空,只余盘上 state.json)
# ---------------------------------------------------------------------------

def make_history_job(
    jobs_root: Path,
    job_id: str,
    *,
    status: str = "succeeded",
    created_at: str | None = None,
    error: dict[str, str] | None = None,
    job_log: str | None = None,
    job_out: str | None = None,
    artifacts: dict[str, str] | None = None,
) -> str:
    """手造历史作业目录(state.json + 可选 job.log/job.out/artifacts/),返回 job_id。

    created_at 缺省取当前时刻(手造历史恒终态且须晚于保留期截断点,
    否则 create_app 启动清扫会把它删掉);payload 形态对齐 _persist 落盘。
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = created_at or now_iso
    job_dir = jobs_root / job_id
    (job_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    payload = {
        "id": job_id,
        "method": "passthrough",
        "status": status,
        "fidelity": "passthrough",
        "created_at": created,
        "started_at": created,
        "finished_at": created if status in TERMINAL_STATUSES else None,
        "error": error,
        "log_url": f"/jobs/{job_id}/log",
        "result_url": f"/jobs/{job_id}/result",
        "artifacts_url": f"/jobs/{job_id}/artifacts",
    }
    (job_dir / "state.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if job_log is not None:
        (job_dir / "job.log").write_text(job_log, encoding="utf-8")
    if job_out is not None:
        (job_dir / "job.out").write_text(job_out, encoding="utf-8")
    for name, content in (artifacts or {}).items():
        (job_dir / "artifacts" / name).write_text(content, encoding="utf-8")
    return job_id


# ---------------------------------------------------------------------------
# 1. GET /jobs 列表
# ---------------------------------------------------------------------------

def test_empty_jobs_list(client: TestClient) -> None:
    """无作业时返回空列表(端点存在且形态为 list)。"""
    response = client.get("/jobs")
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_submitted_job_appears_in_list(settings_factory, fake_executors) -> None:
    """提交 → 列表出现且实时反映状态;终态 succeeded 后列表同步。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "passthrough", passthrough_payload(settings))
        listed = client.get("/jobs").json()
        assert [job["id"] for job in listed] == [accepted["id"]]
        assert listed[0]["status"] in ("pending", "running", "succeeded")

        wait_for_terminal(client, accepted["id"])
        listed = client.get("/jobs").json()
        assert listed[0]["status"] == "succeeded"
        assert listed[0]["method"] == "passthrough"
        assert listed[0]["log_url"] == f"/jobs/{accepted['id']}/log"


def test_list_orders_by_created_at_desc(settings_factory) -> None:
    """倒序:受理时间新者在前(显式异秒钉序;同秒撞键由排序键 id 兜底)。"""
    settings = enabled_settings(settings_factory)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    make_history_job(
        settings.jobs_root, "hist-older",
        created_at=base.isoformat(timespec="seconds"),
    )
    make_history_job(
        settings.jobs_root, "hist-newer",
        created_at=(base + timedelta(hours=1)).isoformat(timespec="seconds"),
    )
    with TestClient(create_app(settings)) as client:
        listed = client.get("/jobs").json()
    assert [job["id"] for job in listed] == ["hist-newer", "hist-older"]


def test_list_merges_memory_and_history(settings_factory, fake_executors) -> None:
    """合并:内存终态作业与盘上历史作业同列(新受理在前)。"""
    fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
    settings = enabled_settings(settings_factory)
    old_created = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="seconds")
    make_history_job(settings.jobs_root, "hist-old", created_at=old_created)
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "passthrough", passthrough_payload(settings))
        wait_for_terminal(client, accepted["id"])
        listed = client.get("/jobs").json()
    assert [job["id"] for job in listed] == [accepted["id"], "hist-old"]


def test_corrupt_state_json_skipped_without_500(settings_factory) -> None:
    """坏 state.json / 无 state.json 的目录跳过,不拖垮列表也不 500;单查该 ID → 404。"""
    settings = enabled_settings(settings_factory)
    make_history_job(settings.jobs_root, "hist-good")
    bad_dir = settings.jobs_root / "hist-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "state.json").write_text("{not-json", encoding="utf-8")
    (settings.jobs_root / "hist-nostate").mkdir(parents=True)  # 无 state.json 的孤儿目录
    with TestClient(create_app(settings)) as client:
        listed = client.get("/jobs")
        assert listed.status_code == 200
        assert [job["id"] for job in listed.json()] == ["hist-good"]
        assert client.get("/jobs/hist-bad").status_code == 404
        assert client.get("/jobs/hist-nostate").status_code == 404


# ---------------------------------------------------------------------------
# 2. 历史回退:服务重启后(内存清空)历史作业对全部 /jobs/{id} 端点仍可用
# ---------------------------------------------------------------------------

def test_restart_keeps_history_queryable(settings_factory, fake_executors) -> None:
    """模拟重启(同 settings 二次 create_app):列表/详情/日志/工件/下载/结果全可用。"""
    fake_executors["passthrough"] = FakeKernel(
        result={"fidelity": "passthrough", "artifacts": ["curve.csv"]},
        artifacts={"curve.csv": "x,y\n1,2\n"},
    )
    settings = enabled_settings(settings_factory)
    with TestClient(create_app(settings)) as client:
        accepted = submit(client, "passthrough", passthrough_payload(settings))
        state = wait_for_terminal(client, accepted["id"])
        assert state["status"] == "succeeded", state

    with TestClient(create_app(settings)) as client:  # 模拟重启:内存清空
        listed = client.get("/jobs").json()
        assert [job["id"] for job in listed] == [accepted["id"]]
        assert listed[0]["status"] == "succeeded"

        detail = client.get(f"/jobs/{accepted['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "succeeded"

        log = client.get(f"/jobs/{accepted['id']}/log")
        assert log.status_code == 200
        assert "作业受理" in log.text

        artifacts = client.get(f"/jobs/{accepted['id']}/artifacts")
        assert artifacts.status_code == 200
        assert artifacts.json() == ["curve.csv"]

        download = client.get(f"/jobs/{accepted['id']}/artifacts/curve.csv")
        assert download.status_code == 200
        assert download.text.startswith("x,y")

        result = client.get(f"/jobs/{accepted['id']}/result")
        assert result.status_code == 200, result.text
        assert result.json()["artifacts"] == ["curve.csv"]


def test_historic_failed_result_returns_409_with_error(settings_factory) -> None:
    """历史 failed 作业的 /result → 409 JOB_FAILED,盘上 error 原样透传。"""
    settings = enabled_settings(settings_factory)
    make_history_job(
        settings.jobs_root, "hist-fail", status="failed",
        error={"code": "CONVERGENCE_FAILED", "message": "MAPDL 未收敛: 步长发散"},
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/jobs/hist-fail/result")
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "JOB_FAILED"
        assert "CONVERGENCE_FAILED" in body["message"]
        assert "未收敛" in body["message"]


def test_delete_historic_job_cleans_directory(settings_factory) -> None:
    """DELETE 历史作业 → 204 且目录真清理(过去 404,现在成为运维真清理)。"""
    settings = enabled_settings(settings_factory)
    make_history_job(settings.jobs_root, "hist-del", status="cancelled")
    with TestClient(create_app(settings)) as client:
        response = client.delete("/jobs/hist-del")
        assert response.status_code == 204, response.text
        assert not (settings.jobs_root / "hist-del").exists()
        assert client.get("/jobs/hist-del").status_code == 404
        assert client.delete("/jobs/hist-del").status_code == 404  # 再删 → 404
        assert client.get("/jobs").json() == []


def test_queue_refuses_illegal_dir_names(settings_factory) -> None:
    """裸目录名守卫:snapshot/remove 对 ''/'.'/'..'/'\\'/'/' 拒绝,
    绝不误伤 jobs 根自身(直调队列钉住;URL 层会被 httpx 规范化,构造不出)。"""
    settings = enabled_settings(settings_factory)
    settings.jobs_root.mkdir(parents=True, exist_ok=True)
    queue = JobQueue(settings)
    for illegal in ("", ".", "..", "/", "\\"):
        assert queue.snapshot(illegal) is None, illegal
        assert queue.remove(illegal) is False, illegal
    assert settings.jobs_root.is_dir()  # 纵深防御:根目录未被误删


# ---------------------------------------------------------------------------
# 3. /jobs/{id}/log?source= 白名单(缺省 job.log 向后兼容;根部 job.out 可读)
# ---------------------------------------------------------------------------

def test_log_source_default_is_job_log(settings_factory) -> None:
    """向后兼容钉测:缺省 source 仍读 job.log(既有上游调用零改动)。"""
    settings = enabled_settings(settings_factory)
    make_history_job(settings.jobs_root, "hist-log", job_log="[事件] 状态变更行\n")
    with TestClient(create_app(settings)) as client:
        response = client.get("/jobs/hist-log/log")
        assert response.status_code == 200
        assert "状态变更行" in response.text


def test_log_source_whitelist_rejects_bookkeeping(settings_factory) -> None:
    """白名单外一律 400:根部簿记文件(state.json)与穿越串(../state.json)都不可读。"""
    settings = enabled_settings(settings_factory)
    make_history_job(settings.jobs_root, "hist-src")
    with TestClient(create_app(settings)) as client:
        for bad_source in ("state.json", "../state.json", "launcher.log", "artifacts"):
            response = client.get(f"/jobs/hist-src/log?source={bad_source}")
            assert response.status_code == 400, bad_source
            assert response.json()["code"] == "INVALID_PARAMS", bad_source


def test_log_source_job_out_reads_root_file_with_tail(settings_factory) -> None:
    """source=job.out:读作业目录根部的实时 MAPDL 输出(运行中仅存在于根部,
    不进 artifacts/),tail 截尾同语义。"""
    settings = enabled_settings(settings_factory)
    make_history_job(
        settings.jobs_root, "hist-out",
        job_out="MAPDL 第1行\nMAPDL 第2行\nMAPDL 第3行\n",
    )
    with TestClient(create_app(settings)) as client:
        artifacts = client.get("/jobs/hist-out/artifacts").json()
        assert artifacts == []  # 根部 job.out 未发布进 artifacts(白名单读根部即为此)

        full = client.get("/jobs/hist-out/log?source=job.out")
        assert full.status_code == 200
        assert len(full.text.splitlines()) == 3
        assert full.text.startswith("MAPDL 第1行")

        tail = client.get("/jobs/hist-out/log?source=job.out&tail=2")
        assert tail.status_code == 200
        assert tail.text == "MAPDL 第2行\nMAPDL 第3行\n"
