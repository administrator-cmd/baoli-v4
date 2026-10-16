"""逐份真实提交去章 PDF，保存请求摘要和作业状态；不重试、不改业务任务。"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import get_settings


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def check_file(path: Path, filename: str, output: Path, settings) -> dict:
    content = path.read_bytes()
    options = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    record = {
        "file": str(path), "filename": filename, "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "model": settings.ocr_model, "optionalPayload": options,
        "started_at": datetime.now().astimezone().isoformat(),
        "outcome": "SUBMITTING", "events": [],
    }
    save(output / "summary.json", record)
    headers = {"Authorization": f"bearer {settings.ocr_token_value}"}
    url = settings.ocr_service_url.rstrip("/")
    try:
        with requests.Session() as session:
            response = session.post(
                url, headers=headers,
                data={"model": settings.ocr_model, "optionalPayload": json.dumps(options)},
                files={"file": (filename, content)},
                timeout=settings.request_timeout_seconds,
            )
            record["submit_http_status"] = response.status_code
            response.raise_for_status()
            body = response.json()
            record["submit_trace_id"] = body.get("traceId")
            job_id = body["data"]["jobId"]
            record.update(job_id=job_id, outcome="PENDING")
            save(output / "summary.json", record)
            print(f"job_id={job_id}", flush=True)
            while True:
                response = session.get(
                    f"{url}/{job_id}", headers=headers,
                    timeout=settings.request_timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                data = body["data"]
                state = data["state"]
                event = {
                    "time": datetime.now().astimezone().isoformat(),
                    "http_status": response.status_code, "traceId": body.get("traceId"),
                    "data": {key: value for key, value in data.items() if key != "resultUrl"},
                }
                record["events"].append(event)
                record["outcome"] = state
                save(output / "summary.json", record)
                print(f"state={state} progress={data.get('extractProgress')}", flush=True)
                if state == "failed":
                    print(f"provider_error={data.get('errorMsg', data.get('error'))}", flush=True)
                    break
                if state == "done":
                    result = session.get(
                        data["resultUrl"]["jsonUrl"], timeout=settings.request_timeout_seconds,
                    )
                    result.raise_for_status()
                    (output / "result.jsonl").write_bytes(result.content)
                    pages = 0
                    characters = 0
                    for line in result.text.splitlines():
                        if not line.strip():
                            continue
                        for page in json.loads(line)["result"]["layoutParsingResults"]:
                            text = page["markdown"]["text"]
                            pages += 1
                            characters += len(text)
                            (output / f"page_{pages:03d}.md").write_text(text, encoding="utf-8")
                    record.update(page_count=pages, text_characters=characters)
                    print(f"downloaded_pages={pages} text_characters={characters}", flush=True)
                    break
                if state not in {"pending", "running"}:
                    raise ValueError(f"Unknown provider state: {state}")
                time.sleep(settings.ocr_poll_interval_seconds)
    except Exception as exc:
        # 批量诊断继续检查下一个文件；失败保留完整 traceback 并以非零状态退出。
        record["client_error_type"] = type(exc).__name__
        record["provider_state_before_client_error"] = record["outcome"]
        record["outcome"] = "CLIENT_ERROR"
        (output / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(f"client_error={type(exc).__name__}; see exception.txt", flush=True)
    record["finished_at"] = datetime.now().astimezone().isoformat()
    save(output / "summary.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--filenames", nargs="+", required=True, help="与 files 一一对应的上传文件名")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.files) != len(args.filenames):
        parser.error("files 与 filenames 数量必须相同")
    settings = get_settings()
    if not settings.ocr_service_url or not settings.ocr_token_value or not settings.ocr_model:
        raise ValueError("OCR URL/token/model configuration missing")
    paths = [path.resolve(strict=True) for path in args.files]
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] {path}", flush=True)
        target = args.output / f"file_{index:02d}"
        target.mkdir()
        results.append(check_file(path, args.filenames[index - 1], target, settings))
        save(args.output / "summary.json", results)
    print(f"Report: {args.output.resolve()}", flush=True)
    return 0 if all(item["outcome"] == "done" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
