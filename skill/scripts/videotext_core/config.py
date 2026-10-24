"""skill 配置：读取 skill/.env 与内置常量。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"

load_dotenv(SKILL_DIR / ".env")


def dashscope_api_key() -> str | None:
    raw = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not raw or not raw.startswith("sk-"):
        return None
    return raw


def bilibili_cookie() -> str | None:
    return (os.environ.get("LINK_PREVIEW_BILIBILI_COOKIE") or "").strip() or None


def xiaohongshu_cookie() -> str | None:
    return (os.environ.get("LINK_PREVIEW_XIAOHONGSHU_COOKIE") or "").strip() or None


def ffmpeg_path_env() -> str | None:
    return (os.environ.get("FFMPEG_PATH") or "").strip() or None


# --- 用户偏好（skill/preferences.json，gitignore）---

PREFERENCES_PATH = SKILL_DIR / "preferences.json"


def read_default_output_dir() -> str | None:
    import json

    try:
        data = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    val = (data.get("default_output_dir") or "").strip()
    return val or None


def save_default_output_dir(path: str) -> None:
    import json
    from datetime import datetime, timezone

    PREFERENCES_PATH.write_text(
        json.dumps(
            {
                "default_output_dir": str(Path(path).expanduser().resolve()),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# --- ASR（DashScope paraformer-v2 录音文件识别）---

ASR_MODEL = "paraformer-v2"
# 单文件上限：2 小时 / 2GB（官方文档）
ASR_MAX_FILE_SECONDS = 7200
# 轮询节奏
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 3600

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com"
DASHSCOPE_UPLOAD_POLICY_URL = f"{DASHSCOPE_BASE}/api/v1/uploads"
