"""ffmpeg 定位、媒体时长探测、音轨抽取。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

_DURATION_HDR_RE = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)",
    re.IGNORECASE,
)


class MediaPrepareError(Exception):
    """无法得到可用于 ASR 的媒体（用户可读 message）。"""


def _bundled_imageio_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    return exe if exe else None


def _resolve_ffmpeg() -> str | None:
    cfg = config.ffmpeg_path_env()
    if cfg:
        p = Path(cfg)
        if p.is_file():
            return str(p.resolve())
        exe = p / "ffmpeg.exe" if (p / "ffmpeg.exe").is_file() else p / "ffmpeg"
        if exe.is_file():
            return str(exe.resolve())
        if shutil.which(cfg):
            return cfg
        return None
    w = shutil.which("ffmpeg")
    if w:
        return w
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", "")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", "")
        for base in filter(None, {pf, pfx86}):
            cand = Path(base) / "ffmpeg" / "bin" / "ffmpeg.exe"
            if cand.is_file():
                return str(cand.resolve())
    return _bundled_imageio_ffmpeg()


def resolve_ffmpeg_exe() -> str | None:
    return _resolve_ffmpeg()


def resolve_ffprobe_exe() -> str | None:
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return None
    p = Path(ffmpeg)
    name = p.name.lower()
    if name == "ffmpeg.exe":
        cand = p.with_name("ffprobe.exe")
    elif name == "ffmpeg":
        cand = p.with_name("ffprobe")
    else:
        cand = p.parent / (
            "ffprobe.exe" if os.name == "nt" and p.suffix.lower() == ".exe" else "ffprobe"
        )
    if cand.is_file():
        return str(cand.resolve())
    w = shutil.which("ffprobe")
    return str(Path(w).resolve()) if w else None


def probe_media_duration_seconds(media_path: Path) -> float | None:
    if not media_path.is_file():
        return None
    ffprobe = resolve_ffprobe_exe()
    if ffprobe:
        try:
            p = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if p.returncode == 0:
                s = (p.stdout or "").strip()
                if s:
                    d = float(s)
                    if d > 0:
                        return d
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pass
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return None
    try:
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(media_path.resolve())],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        m = _DURATION_HDR_RE.search(p.stderr or "")
        if not m:
            return None
        h, mn, sec = m.groups()
        d = int(h) * 3600 + int(mn) * 60 + float(sec)
        return d if d > 0 else None
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def _run_ffmpeg(
    ffmpeg: str,
    src: Path,
    dst: Path,
    codec_args: list[str],
    timeout: int,
) -> tuple[int, str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src.resolve()),
        *codec_args,
        str(dst.resolve()),
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "ffmpeg 执行超时"
    err = (p.stderr or "") + (p.stdout or "")
    tail = err.strip()[-1200:] if err.strip() else ""
    return p.returncode, tail


def extract_audio_track(video_path: Path, dst: Path, timeout: int = 1800) -> Path:
    """抽取音轨为 16kHz 单声道 MP3（paraformer 要求 16kHz+，无体积约束）。失败抛 MediaPrepareError。"""
    if not video_path.is_file():
        raise MediaPrepareError("视频文件不存在")
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        raise MediaPrepareError(
            "未检测到 ffmpeg，无法抽取音轨。请安装 ffmpeg 并加入 PATH，"
            "或在 skill/.env 设置 FFMPEG_PATH。"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    codec_args = [
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "96k",
        "-f",
        "mp3",
    ]
    code, tail = _run_ffmpeg(ffmpeg, video_path, dst, codec_args, timeout)
    if code != 0:
        raise MediaPrepareError(f"抽取音轨失败：{tail or f'ffmpeg 退出码 {code}'}"[:800])
    if not dst.is_file() or dst.stat().st_size == 0:
        raise MediaPrepareError(f"抽取音轨失败：输出为空。{tail[:400]}")
    return dst
