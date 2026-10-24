"""平台识别与元数据解析（B 站/小红书页面兜底）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import config

_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv", re.I)
_XHS_HOST_RE = re.compile(r"xiaohongshu\.com|xhslink\.com", re.I)


def detect_platform(url: str) -> str:
    u = url.lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "xiaohongshu"
    return "unknown"


def normalize_urls(raw: list[str]) -> tuple[list[str], str | None]:
    cleaned: list[str] = []
    for item in raw:
        s = item.strip()
        if not s:
            continue
        p = urlparse(s)
        if p.scheme not in ("http", "https") or not p.netloc:
            return [], "illegal_url"
        cleaned.append(s)
    if not cleaned:
        return [], "empty_urls"
    return cleaned, None


def strip_topic_hashtags(title: str) -> str:
    if not title:
        return title
    s = re.sub(r"#[^#\s\[]+\[话题\]#", "", title)
    s = re.sub(r"#[^#\s]+#", "", s)
    return " ".join(s.split())


def preferred_title(info: dict | None) -> str | None:
    if not info or not isinstance(info, dict):
        return None
    t = (info.get("title") or info.get("fulltitle") or "").strip()
    if not t:
        return None
    return strip_topic_hashtags(t)


def extract_duration_seconds(info: dict | None) -> float | None:
    if not info or not isinstance(info, dict):
        return None
    raw = info.get("duration")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def upload_date_prefix(info: dict | None) -> str | None:
    if not info or not isinstance(info, dict):
        return None
    for key in ("upload_date", "release_date"):
        ud = info.get(key)
        if ud is None:
            continue
        s = str(ud).strip().replace("-", "")
        if len(s) >= 8 and s[:8].isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts is not None:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError, OverflowError):
            pass
    return None


def format_video_publish_label(info: dict | None, *, source_url: str = "") -> str | None:
    if not info or not isinstance(info, dict):
        return None
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts is not None:
        try:
            cn = timezone(timedelta(hours=8))
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(cn)
            return dt.strftime("%Y-%m-%d %H:%M:%S（北京时间）")
        except (ValueError, TypeError, OSError, OverflowError):
            pass
    dp = upload_date_prefix_with_fallback(info, source_url)
    if dp:
        return f"{dp}（平台标注上传日期，精确到日）"
    return None


def upload_date_prefix_with_fallback(meta: dict | None, url: str) -> str | None:
    dp = upload_date_prefix(meta)
    if dp:
        return dp
    if detect_platform(url) != "xiaohongshu":
        return None
    from .xiaohongshu_metadata import xiaohongshu_publish_date_prefix_from_page

    return xiaohongshu_publish_date_prefix_from_page(url)


def display_title_with_publish_date(origin: str, date_prefix: str | None) -> str:
    o = strip_topic_hashtags((origin or "").strip())
    dp = (date_prefix or "").strip()
    if not dp:
        return o
    return f"{o}（{dp}）"


def ytdlp_http_headers(url: str) -> dict[str, str]:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if _BILI_HOST_RE.search(url):
        h["Referer"] = "https://www.bilibili.com/"
        h["Origin"] = "https://www.bilibili.com"
        ck = config.bilibili_cookie()
        if ck:
            h["Cookie"] = ck
    elif _XHS_HOST_RE.search(url):
        h["Referer"] = "https://www.xiaohongshu.com/"
        h["Origin"] = "https://www.xiaohongshu.com"
        ck = config.xiaohongshu_cookie()
        if ck:
            h["Cookie"] = ck
    return h


def extract_info_metadata_only(url: str) -> dict | None:
    """仅拉取页面元数据（不下载），失败返回 None。"""
    import yt_dlp

    from .bilibili_metadata import (
        bilibili_metadata_from_video_page,
        bilibili_metadata_from_video_page_curl_cffi,
        bilibili_metadata_from_view_api,
        effective_bilibili_fetch_url,
        is_bilibili_like_url,
    )
    from .xiaohongshu_metadata import (
        is_xhslink_url,
        is_xiaohongshu_like_url,
        xiaohongshu_metadata_from_video_page,
    )

    fetch_url = effective_bilibili_fetch_url(url) if is_bilibili_like_url(url) else url
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": ytdlp_http_headers(fetch_url),
    }

    if is_xhslink_url(url):
        fb = xiaohongshu_metadata_from_video_page(url)
        if fb and preferred_title(fb):
            return fb
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
                if info and preferred_title(info):
                    return info
        except Exception:
            pass
        return fb if (fb and preferred_title(fb)) else None

    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # process=False：跳过 YoutubeDL 层格式校验；B 站 ie.extract 仍会请求 playinfo，
            # 失败时用网页 INITIAL_STATE 兜底
            info = ydl.extract_info(fetch_url, download=False, process=False)
    except Exception:
        pass

    if info and preferred_title(info):
        return info

    if is_bilibili_like_url(url):
        fb = bilibili_metadata_from_video_page(url)
        if fb and preferred_title(fb):
            return fb
        # 数据中心 IP 可能被 httpx 限流，用 curl_cffi（TLS 指纹伪装）再试一次
        fb = bilibili_metadata_from_video_page_curl_cffi(url)
        if fb and preferred_title(fb):
            return fb
        fb = bilibili_metadata_from_view_api(url)
        if fb and preferred_title(fb):
            return fb

    if is_xiaohongshu_like_url(url):
        fb = xiaohongshu_metadata_from_video_page(url)
        if fb and preferred_title(fb):
            return fb

    return info
