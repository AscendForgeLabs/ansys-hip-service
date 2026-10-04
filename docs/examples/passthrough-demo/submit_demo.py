#!/usr/bin/env python3
"""passthrough 示范提交脚本 — 上传 → 提交 → 轮询(status+stages)→ 下载工件.

鉴权:服务开启 API Key 后,导出 HIP_SERVICE_API_KEYS=<key>(脚本自动带 X-API-Key;
下方等价 curl 未标注,联调时请自行加 -H "X-API-Key: <key>")。

等价 curl(逐阶段):
  # 1) 上传入口 .inp(multipart;附属 .cdb 同法逐个上传)
  curl -s -F 'file=@capsule_shrink.inp' http://localhost:8010/uploads/apdl
  #    → 200 {"path": "/var/uploads/<token>_capsule_shrink.inp", "size_bytes": ...}

  # 2) 提交(202;path 换成上一步返回值)
  curl -s -X POST http://localhost:8010/sim/passthrough \
    -H 'Content-Type: application/json' \
    -d '{"params": {"entry_file": "/var/uploads/<token>_capsule_shrink.inp",
          "extra_files": [],
          "declared_outputs": ["frame_1.csv", "frame_2.csv", "frame_3.csv",
                                "frame_4.csv", "deform.csv"],
          "workflow": "HIP_DEMO_V1", "timeout_s": 1800}}'
  #    → 202 {"id": "...", "method": "passthrough", "status_url": "/jobs/..."}

  # 3) 轮询(stages 来自 .inp 写的 progress.csv 侧车)
  watch -n3 'curl -s http://localhost:8010/jobs/<id>'

  # 4) 取结果 / 工件列表 / 流式下载
  curl -s http://localhost:8010/jobs/<id>/result
  curl -s http://localhost:8010/jobs/<id>/artifacts
  curl -s -O http://localhost:8010/jobs/<id>/artifacts/deform.csv

declared_outputs 说明(契约见 docs/passthrough-guide.md §4.3):
  * 只声明 .inp 承诺产出、且调用方要用的裸文件名 —— 本例 = 4 个帧文件
    (frame_1..4.csv,动画数据)+ deform.csv(末态变形坐标,final_powder.step
    重构的数据源);缺一即作业 failed(ARTIFACT_NOT_FOUND)。
  * progress.csv / results.csv 由服务自动处理(侧车 / 自动解析进 result.values),
    **不要声明** —— 声明了反而变成额外存在性义务。
  * 入口 .inp 与 job.out 也是自动发布,无需声明。

用法:
  python submit_demo.py [BASE_URL]        # 缺省 http://localhost:8010
依赖: pip install httpx (>=0.24)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL_DEFAULT = "http://localhost:8010"
ENTRY_FILE = "capsule_shrink.inp"
# 与 capsule_shrink.inp 的 NSEG=4 对应(*CFOPEN,frame_%I%,csv 生成);
# 改 .inp 段数时须同步此清单。
DECLARED_OUTPUTS = [
    "frame_1.csv",
    "frame_2.csv",
    "frame_3.csv",
    "frame_4.csv",
    "deform.csv",
]
WORKFLOW_TAG = "HIP_DEMO_V1"      # 纯溯源标签,服务不据此分支
POLL_INTERVAL_S = 3
DOWNLOAD_DIR = Path("demo-artifacts")   # 工件落盘子目录
TIMEOUT_S = 1800


def upload_entry(client: httpx.Client, base_url: str) -> str:
    """上传入口 .inp,返回服务端路径(multipart,等价 curl -F 'file=@...')."""
    entry_path = Path(__file__).resolve().parent / ENTRY_FILE
    if not entry_path.is_file():
        raise SystemExit(f"入口文件缺失: {entry_path}")
    with entry_path.open("rb") as stream:
        response = client.post(
            f"{base_url}/uploads/apdl",
            files={"file": (ENTRY_FILE, stream)},
        )
    if response.status_code != 200:
        raise SystemExit(f"上传失败 HTTP {response.status_code}: {response.text}")
    payload = response.json()
    print(f"[1/4] 上传成功: {payload['path']}({payload['size_bytes']} 字节)")
    return payload["path"]


def submit(client: httpx.Client, base_url: str, entry_path: str) -> str:
    """提交 passthrough 作业,返回 job_id(202 受理,POST 不重试)."""
    response = client.post(
        f"{base_url}/sim/passthrough",
        json={"params": {
            "entry_file": entry_path,
            "extra_files": [],                 # 本演示自包含,无附属 .cdb
            "declared_outputs": DECLARED_OUTPUTS,
            "workflow": WORKFLOW_TAG,
            "timeout_s": TIMEOUT_S,
        }},
    )
    if response.status_code != 202:
        # 常见:403 PASSTHROUGH_DISABLED(开关未开,纯内网才允许开)
        raise SystemExit(f"提交失败 HTTP {response.status_code}: {response.text}")
    accepted = response.json()
    print(f"[2/4] 已受理: id={accepted['id']} status_url={accepted['status_url']}")
    return accepted["id"]


def poll_until_done(client: httpx.Client, base_url: str, job_id: str) -> dict:
    """轮询至终态,逐次打印 status + stages(已完成阶段序列)."""
    terminal = {"succeeded", "failed", "cancelled"}
    while True:
        response = client.get(f"{base_url}/jobs/{job_id}")
        if response.status_code != 200:
            raise SystemExit(f"轮询失败 HTTP {response.status_code}: {response.text}")
        state = response.json()
        stages = state.get("stages")
        stage_text = (
            " → ".join(f"{row['label']}@{row['time_s']:g}s" for row in stages)
            if stages else "(侧车未就绪)"
        )
        print(f"[3/4] status={state['status']} stages=[{stage_text}]")
        if state["status"] in terminal:
            if state["status"] != "succeeded":
                error = state.get("error") or {}
                raise SystemExit(
                    f"作业终态={state['status']} error=[{error.get('code')}] {error.get('message')}"
                )
            return state
        time.sleep(POLL_INTERVAL_S)


def fetch_result_and_artifacts(client: httpx.Client, base_url: str, job_id: str) -> None:
    """取结果 JSON 并把全部工件流式下载到本地 DOWNLOAD_DIR."""
    result = client.get(f"{base_url}/jobs/{job_id}/result").json()
    print(f"[4/4] fidelity={result['fidelity']} elapsed_s={result.get('elapsed_s')}")
    values = result.get("values")
    if values:
        print("      values=" + ", ".join(f"{k}={v}" for k, v in values.items()))
    names = client.get(f"{base_url}/jobs/{job_id}/artifacts").json()
    target_dir = Path(__file__).resolve().parent / DOWNLOAD_DIR
    target_dir.mkdir(exist_ok=True)
    for name in names:
        with client.stream("GET", f"{base_url}/jobs/{job_id}/artifacts/{name}") as resp:
            if resp.status_code != 200:
                print(f"      下载失败 {name}: HTTP {resp.status_code}")
                continue
            target = target_dir / name
            with target.open("wb") as out:      # 流式落盘,不整读内存
                for chunk in resp.iter_bytes():
                    out.write(chunk)
        print(f"      工件已下载: {DOWNLOAD_DIR}/{name}")
    print("完成。帧文件可按 frame_1..4 顺序做前端插值动画;deform.csv 供几何重构。")


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else BASE_URL_DEFAULT
    # 鉴权:服务开启 API Key 后必带;取环境变量 HIP_SERVICE_API_KEYS 首个值
    # (与服务端同名,可直接复用部署配置)
    api_key = os.environ.get("HIP_SERVICE_API_KEYS", "").split(",")[0].strip()
    headers = {"X-API-Key": api_key} if api_key else None
    with httpx.Client(timeout=60, headers=headers) as client:
        entry_path = upload_entry(client, base_url)
        job_id = submit(client, base_url, entry_path)
        poll_until_done(client, base_url, job_id)
        fetch_result_and_artifacts(client, base_url, job_id)


if __name__ == "__main__":
    main()
