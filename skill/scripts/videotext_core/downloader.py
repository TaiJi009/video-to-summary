"""yt-dlp 下载与平台字幕轨拉取（纯函数）。"""

from __future__ import annotations

import re
from pathlib import Path

_ANSI_ESC = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_ESC.sub("", text)


def _stem_collides(directory: Path, stem: str) -> bool:
    for p in directory.iterdir():
        if p.is_file() and p.stem == stem:
            return True
    return False


def allocate_unique_stem(directory: Path, base_stem: str) -> str:
    """同名冲突 → ``stem (1)`` 递增。"""
    if not _stem_collides(directory, base_stem):
        return base_stem
    n = 1
    while True:
        cand = f"{base_stem} ({n})"
        if not _stem_collides(directory, cand):
            return cand
        n += 1


def _bilibili_like_url(url: str) -> bool:
    u = url.lower()
    return "bilibili.com" in u or "b23.tv" in u


_XHS_RESOLVE_FAIL_MSG = (
    "小红书短链未能解析为笔记页，请更新 LINK_PREVIEW_XIAOHONGSHU_COOKIE 或改粘贴 explore 长链"
)


def _needs_xhs_url_resolve(url: str) -> bool:
    from .xiaohongshu_metadata import is_xhslink_url, is_xiaohongshu_like_url, note_id_from_url

    if is_xhslink_url(url):
        return True
    if is_xiaohongshu_like_url(url) and not note_id_from_url(url):
        return True
    return False


def effective_download_url(source_url: str) -> str:
    """小红书短链 → 笔记页 URL；B 站 b23.tv → 稿件页；其它平台原样返回。"""
    from yt_dlp.utils import DownloadError

    from .bilibili_metadata import (
        effective_bilibili_fetch_url,
        get_bili_preview_hint,
        is_b23_short_url,
    )
    from .xiaohongshu_metadata import (
        get_xhs_preview_hint,
        note_id_from_url,
        resolve_xhs_note_page_urls,
    )

    url = (source_url or "").strip()
    if _needs_xhs_url_resolve(url):
        candidates = resolve_xhs_note_page_urls(url)
        for candidate in candidates:
            if note_id_from_url(candidate):
                return candidate

        hint = (get_xhs_preview_hint() or "").strip()
        raise DownloadError(hint or _XHS_RESOLVE_FAIL_MSG)

    if "b23.tv" in url.lower():
        if is_b23_short_url(url):
            expanded = effective_bilibili_fetch_url(url)
            if expanded != url:
                return expanded
            hint = (get_bili_preview_hint() or "").strip()
            raise DownloadError(hint or "B 站短链未能解析，请改粘贴 bilibili.com 长链")

    return url


def _prefer_ytdlp_exception(errors: list[Exception]) -> Exception:
    """合并类错误优先于最后的「Requested format is not available」（B 站无预合成 best）。"""
    needle = "Requested format is not available"
    if not errors:
        return RuntimeError("yt-dlp 下载失败")
    non_fmt = [e for e in errors if needle.lower() not in str(e).lower()]
    if non_fmt:
        return non_fmt[0]
    return errors[-1]


def _ffmpeg_location_for_ytdlp() -> str | None:
    from .media import resolve_ffmpeg_exe

    exe = resolve_ffmpeg_exe()
    if not exe:
        return None
    p = Path(exe).resolve()
    return str(p) if p.is_file() else None


def download_with_ytdlp(url: str, outtmpl: str) -> dict | None:
    """多格式尝试下载；B 站 DASH 音视频分离须 ffmpeg 合并。返回 yt-dlp info dict。"""
    import yt_dlp
    from yt_dlp.utils import DownloadError

    shared: dict = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": _ytdlp_http_headers(url),
    }
    loc = _ffmpeg_location_for_ytdlp()
    if loc:
        shared["ffmpeg_location"] = loc
    elif _bilibili_like_url(url):
        raise DownloadError(
            "未检测到 ffmpeg：B 站稿件多为「视频轨 + 音频轨」分离流，下载后必须用 ffmpeg 合并。"
            "请安装 ffmpeg 并加入 PATH，或在 skill/.env 设置 FFMPEG_PATH。"
        )

    # B 站多为音视频分离 DASH：须能合并；勿用单独的 best（预合成）兜底，否则会误报
    #「Requested format is not available」并掩盖真实的 ffmpeg 问题。
    attempts: tuple[tuple[str, bool], ...] = (
        ("bestvideo+bestaudio/best", True),
        ("bv*+ba/b", True),
        ("bestvideo*+bestaudio/best", True),
        ("bv+ba/best", True),
    )
    errors: list[Exception] = []
    for fmt, merge_mp4 in attempts:
        opts = {**shared, "format": fmt}
        if merge_mp4:
            opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            errors.append(e)
            continue
    raise _prefer_ytdlp_exception(errors)


def _ytdlp_http_headers(url: str) -> dict[str, str]:
    from .metadata import ytdlp_http_headers

    return ytdlp_http_headers(url)


def find_downloaded_video(root: Path, stem: str) -> Path | None:
    """下载完成后在 root 下定位 stem 对应的视频文件。"""
    candidates = []
    for ext in (".mp4", ".webm", ".mkv", ".mov", ".m4a"):
        p = root / f"{stem}{ext}"
        if p.is_file():
            candidates.append(p)
    if candidates:
        return max(candidates, key=lambda x: x.stat().st_mtime)

    globs = list(root.glob(f"{stem}.*"))
    videos = [
        p
        for p in globs
        if p.is_file() and p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")
    ]
    if videos:
        return max(videos, key=lambda x: x.stat().st_mtime)
    return None


_SUBTITLE_LANGS = ["zh-Hans", "zh-CN", "ai-zh", "zh", "en"]

# B站 AI 字幕 JSON 行结构：{"from": sec, "to": sec, "content": "..."}
_BILI_AI_LANS = ("ai-zh", "zh-Hans", "zh-CN", "zh")


def _bili_ai_subtitle_via_api(url: str, task_dir: Path, stem: str) -> Path | None:
    """yt-dlp 拿不到 B站 AI 字幕（其 extractor 的字幕请求不带 Cookie），
    直接调 player API 用 Cookie 取 ai-zh 轨 JSON 转 SRT。失败返回 None。"""
    import json as _json
    import re as _re

    import httpx

    from . import config
    from .subtitles import ms_to_srt_time

    ck = config.bilibili_cookie()
    if not ck:
        return None
    m = _re.search(r"(BV[\w]+)", url)
    if not m:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Cookie": ck,
    }
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            # BV → aid/cid：走 view API 拿 cid（wbi 签名对 player API 非必需的旧接口也行；
            # 直接用 wbi/v2 无签名在带 Cookie + Referer 时通常可用）
            vr = client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": m.group(1)},
                headers=headers,
            )
            vd = (vr.json() or {}).get("data") or {}
            aid, cid = str(vd.get("aid") or ""), ""
            pages = vd.get("pages") or []
            if pages:
                cid = str(pages[0].get("cid") or "")
            if not (aid and cid):
                return None

            pr = client.get(
                "https://api.bilibili.com/x/player/wbi/v2",
                params={"aid": aid, "cid": cid},
                headers=headers,
            )
            subs = ((pr.json() or {}).get("data") or {}).get("subtitle", {}).get("subtitles") or []
            track = next((s for s in subs if s.get("lan") in _BILI_AI_LANS), None)
            if not track:
                return None
            s_url = track.get("subtitle_url") or ""
            if s_url.startswith("//"):
                s_url = "https:" + s_url
            if not s_url:
                return None

            jr = client.get(s_url, headers={"User-Agent": headers["User-Agent"], "Referer": "https://www.bilibili.com/"})
            body = jr.json().get("body") or []
            if not body:
                return None

            lines = []
            for i, item in enumerate(body, start=1):
                begin = ms_to_srt_time(float(item.get("from") or 0) * 1000)
                end = ms_to_srt_time(float(item.get("to") or 0) * 1000)
                text = (item.get("content") or "").strip()
                if not text:
                    continue
                lines.append(f"{len(lines) + 1}\n{begin} --> {end}\n{text}\n")
            if not lines:
                return None
            srt_path = task_dir / f"{stem}.srt"
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text("\n".join(lines), encoding="utf-8")
            return srt_path
    except Exception:
        return None


def try_platform_subtitles(url: str, task_dir: Path, stem: str) -> Path | None:
    """尝试拉取平台现成字幕轨（YouTube 自动字幕 / B站 AI 字幕）。

    命中则规范成 SRT 落盘并返回路径；未命中返回 None（调用方走 ASR）。
    """
    import yt_dlp

    from .metadata import ytdlp_http_headers

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": _SUBTITLE_LANGS,
        "subtitlesformat": "srt/vtt/best",
        "outtmpl": str(task_dir / f"{stem}.%(ext)s"),
        "http_headers": ytdlp_http_headers(url),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        pass

    for ext in (".zh-Hans.srt", ".zh-CN.srt", ".ai-zh.srt", ".zh.srt", ".en.srt",
                ".zh-Hans.vtt", ".ai-zh.vtt", ".zh.vtt", ".en.vtt"):
        p = task_dir / f"{stem}{ext}"
        if p.is_file():
            srt_path = task_dir / f"{stem}.srt"
            if p.suffixes[-1] == ".vtt":
                from .subtitles import convert_to_srt_file

                convert_to_srt_file(p, srt_path)
                p.unlink(missing_ok=True)
            elif p != srt_path:
                p.rename(srt_path)
            return srt_path

    # yt-dlp 拿不到（B站 extractor 不带 Cookie 请求字幕）→ player API 直查
    return _bili_ai_subtitle_via_api(url, task_dir, stem)
