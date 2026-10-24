"""run manifest：原子写、状态聚合、status 摘要。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RUN_STATUSES = ("running", "completed", "partial_failed", "failed")

TASK_ERROR_CODES = (
    "DOWNLOAD_FAILED",
    "ASR_FAILED",
    "ASR_QUOTA",
    "SUMMARY_FAILED",
    "NO_API_KEY",
    "SUMMARY_SKIPPED_NO_KEY",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def task_entry(url: str, index: int) -> dict:
    return {
        "index": index,
        "url": url,
        "platform": "unknown",
        "display_title": None,
        "status": "pending",
        "error_code": None,
        "error_detail": None,
        "output_dir": None,
        "duration_seconds": None,
        "asr_source": None,
        "subtitle_path": None,
        "transcript_path": None,
    }


def create_manifest(run_dir: Path, run_id: str, urls: list[str]) -> dict:
    manifest = {
        "run_id": run_id,
        "status": "running",
        "created_at": now_iso(),
        "completed_at": None,
        "total": len(urls),
        "completed": 0,
        "failed": 0,
        "tasks": [task_entry(u, i) for i, u in enumerate(urls)],
    }
    write_manifest(run_dir, manifest)
    return manifest


def write_manifest(run_dir: Path, manifest: dict) -> None:
    """tmp + rename 原子写，避免轮询方读到半截 JSON。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=run_dir, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, run_dir / "manifest.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_manifest(run_dir: Path) -> dict | None:
    p = run_dir / "manifest.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def refresh_aggregates(manifest: dict) -> None:
    tasks = manifest.get("tasks") or []
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    manifest["completed"] = completed
    manifest["failed"] = failed
    if failed and completed:
        manifest["status"] = "partial_failed"
    elif failed and not completed:
        manifest["status"] = "failed"
    else:
        manifest["status"] = "completed"


def finalize_manifest(run_dir: Path, manifest: dict) -> dict:
    refresh_aggregates(manifest)
    manifest["completed_at"] = now_iso()
    write_manifest(run_dir, manifest)
    return manifest


def status_summary(manifest: dict) -> dict:
    tasks = manifest.get("tasks") or []
    failed_urls = [
        {"url": t.get("url"), "error_code": t.get("error_code"), "error_detail": t.get("error_detail")}
        for t in tasks
        if t.get("status") == "failed"
    ]
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "total": manifest.get("total"),
        "completed": manifest.get("completed"),
        "failed": manifest.get("failed"),
        "failed_urls": failed_urls,
    }
