"""作业阶段进度测试 — progress.csv 侧车的读时投影(计划 Step 5).

覆盖三层:
    1. results.parse_progress_csv 纯函数解析(缺失/撕裂/坏行容忍);
    2. schemas.JobStage 冻结模型与 ≤8 字符标签约束;
    3. 队列读时投影端到端(pending→None / running→实时 / 终态→末帧;
       无侧车时 GET /jobs/{id} 响应与现状逐字段一致 + openapi 覆盖自查)。

全部用假内核(经 conftest fake_executors 注入),不运行 MAPDL。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ansys_hip.api import create_app
from ansys_hip.results import PROGRESS_FILENAME, parse_progress_csv
from ansys_hip.schemas import JobStage

# 复用 passthrough 测试工具(tests/ 在 sys.path;开通道的 Settings 与上传助手)
from test_passthrough import enabled_settings, upload_apdl

POLL_INTERVAL_S = 0.02
WAIT_TIMEOUT_S = 10.0

# 现状(改动前)GET /jobs/{id} 的全部字段;stages 为本流唯一新增字段。
LEGACY_JOB_STATE_KEYS = {
    "id", "method", "part", "status", "fidelity",
    "created_at", "started_at", "finished_at", "error",
    "log_url", "result_url", "artifacts_url",
}


def write_progress(job_dir: Path, content: str) -> None:
    """向 job_dir 根写一帧 progress.csv(模拟上游 .inp 的覆盖式重写)。"""
    (job_dir / PROGRESS_FILENAME).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. parse_progress_csv 纯函数
# ---------------------------------------------------------------------------

class TestParseProgressCsv:
    def test_missing_file_returns_none(self, tmp_path):
        assert parse_progress_csv(tmp_path) is None

    def test_parses_rows_in_completion_order(self, tmp_path):
        # 阶段序列即文件行序(已完成阶段的顺序),不做排序;容忍 APDL 定宽填充
        write_progress(tmp_path, "HEAT,3600\nHOLD,7200\nCOOL, 9000.5\n")
        stages = parse_progress_csv(tmp_path)
        assert stages == [
            JobStage(label="HEAT", time_s=3600.0),
            JobStage(label="HOLD", time_s=7200.0),
            JobStage(label="COOL", time_s=9000.5),
        ]

    def test_accepts_apdl_scientific_notation(self, tmp_path):
        # *VWRITE E16.8 写出形如 0.36000000E+04 的定宽浮点
        write_progress(tmp_path, " HEAT,  0.36000000E+04\n")
        stages = parse_progress_csv(tmp_path)
        assert stages == [JobStage(label="HEAT", time_s=3600.0)]

    def test_torn_half_line_skipped(self, tmp_path):
        # 覆盖式整文件重写被读时撞上:末行写了一半(标签残缺/数值截断)→ 跳过该行
        write_progress(tmp_path, "HEAT,3600\nHOL")
        assert parse_progress_csv(tmp_path) == [JobStage(label="HEAT", time_s=3600.0)]

    def test_torn_trailing_comma_skipped(self, tmp_path):
        write_progress(tmp_path, "HEAT,3600\nHOLD,")
        assert parse_progress_csv(tmp_path) == [JobStage(label="HEAT", time_s=3600.0)]

    def test_bad_rows_skipped_with_log(self, tmp_path, caplog):
        # 表头行(时间列非浮点)/列数错/空标签/超 8 字符标签/负耗时 → 全部跳过并记日志
        write_progress(
            tmp_path,
            "label,time_s\n"        # 表头:time_s 非浮点
            "HEAT,3600\n"
            "A,B,C\n"               # 列数错
            ",100\n"                # 空标签
            "TOOLONG_LBL,100\n"     # 标签 >8 字符(APDL 写出宽度约束)
            "NEG,-5\n"              # 负累计耗时
            "HOLD,7200\n",
        )
        with caplog.at_level("WARNING"):
            stages = parse_progress_csv(tmp_path)
        assert stages == [
            JobStage(label="HEAT", time_s=3600.0),
            JobStage(label="HOLD", time_s=7200.0),
        ]
        assert any("progress" in record.getMessage() or PROGRESS_FILENAME in record.getMessage()
                   for record in caplog.records)

    def test_empty_file_returns_empty_list(self, tmp_path):
        # 文件存在但尚无完整行(首帧撕裂):[] ≠ None(None 专属"无侧车")
        write_progress(tmp_path, "")
        assert parse_progress_csv(tmp_path) == []

    def test_read_failure_returns_none(self, tmp_path, monkeypatch, caplog):
        # 读盘失败(如权限/并发句柄)不拖垮 GET /jobs/{id}:告警并按无侧车处理
        write_progress(tmp_path, "HEAT,3600\n")

        def _raise_read(self: Path, **kwargs: Any) -> str:
            raise OSError("boom")

        monkeypatch.setattr(Path, "read_text", _raise_read)
        with caplog.at_level("WARNING"):
            assert parse_progress_csv(tmp_path) is None
        assert any("progress" in record.getMessage() or PROGRESS_FILENAME in record.getMessage()
                   for record in caplog.records)


# ---------------------------------------------------------------------------
# 2. JobStage 模型约束
# ---------------------------------------------------------------------------

class TestJobStageModel:
    def test_frozen(self):
        stage = JobStage(label="HEAT", time_s=1.0)
        with pytest.raises(ValidationError):
            stage.label = "COOL"  # type: ignore[misc]

    def test_label_length_bounds(self):
        with pytest.raises(ValidationError):
            JobStage(label="", time_s=1.0)
        with pytest.raises(ValidationError):
            JobStage(label="NINECHARS", time_s=1.0)  # 9 字符超限

    def test_negative_time_rejected(self):
        with pytest.raises(ValidationError):
            JobStage(label="HEAT", time_s=-1.0)


# ---------------------------------------------------------------------------
# 3. 队列读时投影端到端
# ---------------------------------------------------------------------------

class SidecarKernel:
    """写 progress.csv 侧车的假内核:写首帧后阻塞,放行后覆盖写末帧。"""

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, params: Any, ctx: Any) -> dict[str, Any]:
        self.calls.append((params, ctx))
        write_progress(ctx.job_dir, "HEAT,3600\n")
        self._release.wait(timeout=WAIT_TIMEOUT_S)
        write_progress(ctx.job_dir, "HEAT,3600\nHOLD,7200\n")
        return {"fidelity": "real"}


def poll_until(
    client: TestClient, job_id: str, predicate, timeout_s: float = WAIT_TIMEOUT_S
) -> dict[str, Any]:
    """轮询 GET /jobs/{id} 直到谓词命中;超时 fail 并附最后一次状态。"""
    deadline = time.monotonic() + timeout_s
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = client.get(f"/jobs/{job_id}").json()
        if predicate(state):
            return state
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"作业 {job_id} 在 {timeout_s}s 内未满足轮询条件,最后状态: {state}")


def wait_for_terminal(client: TestClient, job_id: str) -> dict[str, Any]:
    return poll_until(
        client, job_id, lambda state: state["status"] in {"succeeded", "failed", "cancelled"}
    )


class TestQueueProjection:
    @staticmethod
    def _submit_sidecar_job(client: TestClient, params_extra: dict[str, Any] | None = None):
        """经 passthrough 通道提交作业(上传 entry 后 POST /sim/passthrough)。"""
        entry = upload_apdl(client, "demo.inp")
        params = {"entry_file": entry, "declared_outputs": ["results.csv"]}
        if params_extra:
            params.update(params_extra)
        response = client.post("/sim/passthrough", json={"params": params})
        assert response.status_code == 202, response.text
        return response.json()

    def test_running_and_terminal_reflect_sidecar(self, settings_factory, fake_executors):
        # Arrange:running 期间可见首帧(实时),终态保留末帧快照
        release = threading.Event()
        fake_executors["passthrough"] = SidecarKernel(release)
        with TestClient(create_app(enabled_settings(settings_factory))) as client:
            job_id = self._submit_sidecar_job(client)["id"]

            running = poll_until(
                client, job_id,
                lambda state: state["status"] == "running" and state["stages"] is not None,
            )
            # Assert(running → 实时;分两步避免轮询窗口里已写末帧时误判)
            assert running["stages"] in (
                [{"label": "HEAT", "time_s": 3600.0}],
                [{"label": "HEAT", "time_s": 3600.0}, {"label": "HOLD", "time_s": 7200.0}],
            )

            release.set()
            final = wait_for_terminal(client, job_id)
        # Assert(终态 → 末帧快照,文件在盘上自然保留)
        assert final["status"] == "succeeded"
        assert final["stages"] == [
            {"label": "HEAT", "time_s": 3600.0},
            {"label": "HOLD", "time_s": 7200.0},
        ]

    def test_pending_job_stages_is_none(self, settings_factory, fake_executors):
        # Arrange:单并发队列,首个作业阻塞 → 第二个作业停在 pending(尚无文件)
        release = threading.Event()
        fake_executors["passthrough"] = SidecarKernel(release)
        with TestClient(create_app(enabled_settings(settings_factory))) as client:
            first_id = self._submit_sidecar_job(client)["id"]
            poll_until(client, first_id, lambda s: s["status"] == "running")

            second = self._submit_sidecar_job(client)

            # Act
            pending = client.get(f"/jobs/{second['id']}").json()

            # Assert
            assert pending["status"] == "pending"
            assert pending["stages"] is None

            release.set()
            wait_for_terminal(client, first_id)
            wait_for_terminal(client, second["id"])

    def test_no_sidecar_response_matches_legacy_field_set(self, settings_factory, fake_executors):
        # 钉测:无侧车作业(passthrough 未写 progress.csv)→ 响应键集 = 全集 + stages,
        # 且 stages=None(与既有方法形态一致)
        fake_executors["passthrough"] = lambda params, ctx: {"fidelity": "passthrough"}
        with TestClient(create_app(enabled_settings(settings_factory))) as client:
            job_id = self._submit_sidecar_job(client)["id"]

            final = wait_for_terminal(client, job_id)

            assert final["status"] == "succeeded"
            assert set(final.keys()) == LEGACY_JOB_STATE_KEYS | {"stages"}
            assert final["stages"] is None
            assert final["method"] == "passthrough"
            assert final["error"] is None
            assert final["result_url"] == f"/jobs/{job_id}/result"


# ---------------------------------------------------------------------------
# 4. openapi 内省自查(Swagger 覆盖)
# ---------------------------------------------------------------------------

class TestOpenapiSchema:
    def test_job_state_has_stages_field(self, settings):
        schema = create_app(settings).openapi()
        job_state = schema["components"]["schemas"]["JobState"]
        stages = job_state["properties"]["stages"]
        # list[JobStage] | None(缺省)→ anyOf [array, null],且不入 required(只增不减)
        assert stages["anyOf"][0] == {
            "type": "array",
            "items": {"$ref": "#/components/schemas/JobStage"},
        }
        assert stages["anyOf"][1] == {"type": "null"}
        assert "description" in stages and stages["description"]
        assert "stages" not in job_state.get("required", [])

        stage_schema = schema["components"]["schemas"]["JobStage"]
        label = stage_schema["properties"]["label"]
        assert label["maxLength"] == 8
        assert label["minLength"] == 1
        assert "description" in label and label["description"]
        time_field = stage_schema["properties"]["time_s"]
        assert time_field["minimum"] == 0
        assert "description" in time_field and time_field["description"]
