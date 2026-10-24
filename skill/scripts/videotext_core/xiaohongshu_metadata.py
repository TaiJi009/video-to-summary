"""小红书 PC 笔记页：解析 ``window.__INITIAL_STATE__`` 补全标题与发布日期（xhslink 需 Cookie）。

部分机房/IP 只会返回「壳」页面（``noteDetailMap`` 仅占位），此时无法解析，属平台风控限制。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import httpx
from yt_dlp.utils import js_to_json
from yt_dlp.utils.traversal import traverse_obj

from . import config


def read_link_preview_xiaohongshu_cookie():
    return config.xiaohongshu_cookie()

_XHS_HOST_RE = re.compile(r"xiaohongshu\.com|xhslink\.com", re.I)
_XHSLINK_RE = re.compile(r"xhslink\.com", re.I)
_NOTE_PATH_RE = re.compile(
    r"(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(?P<id>[\da-f]+)",
    re.I,
)
_MS_FROM_RAW_PATTERNS = (
    r'"publishTime"\s*:\s*(\d{13})',
    r'"notePublishTime"\s*:\s*(\d{13})',
    r'"lastUpdateTime"\s*:\s*(\d{13})',
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\'])',
    re.I,
)
_TWITTER_TITLE_RE = re.compile(
    r'<meta[^>]+(?:name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\'])',
    re.I,
)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_last_xhs_resolve_hint: str | None = None


def is_xiaohongshu_like_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return bool(_XHS_HOST_RE.search(url))


def is_xhslink_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return bool(_XHSLINK_RE.search(url))


def get_xhs_preview_hint() -> str | None:
    """最近一次 ``resolve_xhs_note_page_urls`` 失败时的用户可读说明。"""
    return _last_xhs_resolve_hint


def _request_headers() -> dict[str, str]:
    h = dict(_DEFAULT_HEADERS)
    ck = (read_link_preview_xiaohongshu_cookie() or "").strip()
    if ck:
        h["Cookie"] = ck
    return h


def _note_id_from_url(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    m = _NOTE_PATH_RE.search(url.strip())
    return m.group("id").lower() if m else None


def note_id_from_url(url: str) -> str | None:
    """从笔记页 URL 或 ``redirectPath`` 解码结果提取 note id（供下载等模块复用）。"""
    return _note_id_from_url(url)


def _redirect_path_from_url(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    try:
        qs = parse_qs(urlsplit(u).query)
    except ValueError:
        qs = {}
    raw = qs.get("redirectPath", [None])[0]
    if raw:
        return unquote(str(raw))
    m = re.search(r"redirectPath=([^&'\"\s<>]+)", u, re.I)
    if m:
        return unquote(m.group(1))
    return None


def _urls_for_note_id(note_id: str, source_url: str | None = None) -> list[str]:
    """由笔记 ID 生成 explore / discovery 候选 URL（保留 source 的 query）。"""
    note_id = note_id.lower()
    query = ""
    fragment = ""
    scheme = "https"
    host = "www.xiaohongshu.com"
    if source_url:
        try:
            sp = urlsplit(source_url.strip())
            if sp.query:
                query = sp.query
            if sp.fragment:
                fragment = sp.fragment
            if sp.scheme:
                scheme = sp.scheme
            if sp.netloc and "xiaohongshu.com" in sp.netloc.lower():
                host = sp.netloc
        except ValueError:
            pass

    acc: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            acc.append(u)

    if source_url and _note_id_from_url(source_url) == note_id:
        add(source_url.strip())
    add(urlunsplit((scheme, host, f"/explore/{note_id}", query, fragment)))
    add(urlunsplit((scheme, host, f"/discovery/item/{note_id}", query, fragment)))
    return acc


def _merge_page_urls(seeds: list[str]) -> list[str]:
    acc: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        nid = _note_id_from_url(seed)
        if nid:
            for u in _urls_for_note_id(nid, seed):
                if u not in seen:
                    seen.add(u)
                    acc.append(u)
        elif seed.strip() and seed.strip() not in seen:
            seen.add(seed.strip())
            acc.append(seed.strip())
    return acc


def resolve_xhs_note_page_urls(url: str, *, client: httpx.Client | None = None) -> list[str]:
    """
    将小红书长链/短链规范化为待 GET 的笔记页 URL 列表。
    副作用：更新 ``get_xhs_preview_hint()``。
    """
    global _last_xhs_resolve_hint
    _last_xhs_resolve_hint = None

    if not is_xiaohongshu_like_url(url):
        return []

    sid = url.strip()
    seeds: list[str] = []
    hint: str | None = None

    rp_input = _redirect_path_from_url(sid)
    if rp_input:
        seeds.append(rp_input)

    def ingest_final(final_url: str, html: str) -> None:
        nonlocal hint
        rp = _redirect_path_from_url(final_url)
        if rp:
            seeds.append(rp)
        nid = _note_id_from_url(final_url)
        if nid:
            seeds.append(final_url)
        low = final_url.lower()
        if "/login" in low and rp:
            hint = hint or (
                "小红书短链需要有效登录 Cookie，请更新 skill/.env 的 "
                "LINK_PREVIEW_XIAOHONGSHU_COOKIE 后重试；或改粘贴 explore 长链"
            )
        elif _note_id_from_url(final_url) is None and (
            low.rstrip("/").endswith("xiaohongshu.com/explore") or low.endswith("/explore")
        ):
            hint = hint or (
                "小红书短链未解析到笔记页（可能 Cookie 失效），请更新 "
                "LINK_PREVIEW_XIAOHONGSHU_COOKIE 或改粘贴 explore 长链"
            )
        elif "/login" in low and not rp:
            hint = hint or (
                "小红书短链需要有效登录 Cookie，请更新 skill/.env 的 "
                "LINK_PREVIEW_XIAOHONGSHU_COOKIE 后重试；或改粘贴 explore 长链"
            )

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True)

    assert client is not None
    try:
        if _note_id_from_url(sid):
            return _merge_page_urls([sid, *seeds])

        if is_xhslink_url(sid):
            r = client.get(sid, headers=_request_headers())
            if r.status_code == 200:
                ingest_final(str(r.url), r.text)
            else:
                _last_xhs_resolve_hint = f"短链请求失败 HTTP {r.status_code}"
                return []
        else:
            seeds.append(sid)
            r = client.get(sid, headers=_request_headers())
            if r.status_code == 200:
                ingest_final(str(r.url), r.text)

        page_urls = _merge_page_urls(seeds)
        if not page_urls and not hint:
            hint = "未能从小红书链接解析笔记地址，可手动填写标题"
        _last_xhs_resolve_hint = hint
        return page_urls
    except Exception:
        _last_xhs_resolve_hint = "小红书链接解析异常，请稍后重试或改粘贴 explore 长链"
        return []
    finally:
        if own_client:
            client.close()


def _extract_initial_state_brace_block(webpage: str) -> str | None:
    marker = "window.__INITIAL_STATE__"
    idx = webpage.find(marker)
    if idx == -1:
        return None
    eq = webpage.find("=", idx + len(marker))
    if eq == -1:
        return None
    i = eq + 1
    n = len(webpage)
    while i < n and webpage[i] in " \t\n\r":
        i += 1
    if i >= n or webpage[i] != "{":
        return None
    depth = 0
    start = i
    for j in range(i, n):
        c = webpage[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return webpage[start : j + 1]
    return None


def _publish_ms_from_raw_initial(raw_js: str) -> int | None:
    for pat in _MS_FROM_RAW_PATTERNS:
        m = re.search(pat, raw_js)
        if m:
            return int(m.group(1))
    return None


def _parse_initial_state(webpage: str) -> dict | None:
    raw = _extract_initial_state_brace_block(webpage)
    if not raw:
        return None
    try:
        normalized = js_to_json(raw)
        obj = json.loads(normalized)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _collect_note_candidates(state: dict, note_id: str) -> list[dict]:
    ndm = traverse_obj(state, ("note", "noteDetailMap")) or traverse_obj(
        state, ("note", "note_detail_map")
    )
    candidates: list[dict] = []

    def push(maybe: dict | None) -> None:
        if isinstance(maybe, dict):
            candidates.append(maybe)

    if isinstance(ndm, dict):
        entry = ndm.get(note_id) or ndm.get(note_id.upper())
        if isinstance(entry, dict):
            push(entry.get("note"))
        slot = ndm.get("undefined")
        if isinstance(slot, dict):
            push(slot.get("note"))

    push(traverse_obj(state, ("note", "noteDetailMap", note_id, "note")))
    push(traverse_obj(state, ("note", "note_detail_map", note_id, "note")))

    out: list[dict] = []
    seen: set[int] = set()
    for note in candidates:
        oid = id(note)
        if oid in seen:
            continue
        seen.add(oid)
        out.append(note)
    return out


def _note_detail(state: dict, note_id: str) -> dict | None:
    candidates = _collect_note_candidates(state, note_id)
    with_title: dict | None = None
    with_time: dict | None = None
    for note in candidates:
        if _title_from_note(note):
            with_title = note
            break
        if with_time is None and _pick_publish_seconds(note) is not None:
            with_time = note
    return with_title or with_time


def _timestamp_candidate_to_seconds(val: object) -> float | None:
    if val is None:
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    if x >= 1e12:
        x /= 1000.0
    elif x < 1e9:
        return None
    return x


def _pick_publish_seconds(note: dict) -> float | None:
    keys = (
        "publishTime",
        "lastUpdateTime",
        "notePublishTime",
        "createTime",
        "ctime",
    )
    for k in keys:
        sec = _timestamp_candidate_to_seconds(note.get(k))
        if sec is not None:
            return sec
    return None


def _seconds_to_date_prefix_utc(sec: float) -> str | None:
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _title_from_note(note: dict) -> str | None:
    for k in ("title", "displayTitle", "display_title", "desc"):
        v = note.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _title_from_og_meta(html: str) -> str | None:
    for pat in (_OG_TITLE_RE, _TWITTER_TITLE_RE):
        m = pat.search(html)
        if m:
            t = (m.group(1) or m.group(2) or "").strip()
            if t and t not in ("小红书", "Xiaohongshu"):
                return t
    return None


def _metadata_from_html(html: str, note_id: str) -> dict | None:
    state = _parse_initial_state(html)
    if not state:
        return None
    note = _note_detail(state, note_id)
    if not isinstance(note, dict):
        return None
    title = _title_from_note(note)
    if not title:
        return None
    out: dict = {"title": title}
    sec = _pick_publish_seconds(note)
    if sec is not None:
        out["timestamp"] = int(sec)
    return out


def _metadata_from_page(html: str, note_id: str | None) -> dict | None:
    if note_id:
        meta = _metadata_from_html(html, note_id)
        if meta:
            return meta
    title = _title_from_og_meta(html)
    if not title:
        return None
    out: dict = {"title": title}
    if note_id:
        raw_block = _extract_initial_state_brace_block(html)
        if raw_block:
            ms = _publish_ms_from_raw_initial(raw_block)
            if ms is not None:
                sec = ms / 1000.0
                out["timestamp"] = int(sec)
    return out


def _date_prefix_from_html(html: str, note_id: str) -> str | None:
    raw_block = _extract_initial_state_brace_block(html)
    if raw_block:
        ms = _publish_ms_from_raw_initial(raw_block)
        if ms is not None:
            dp = _seconds_to_date_prefix_utc(ms / 1000.0)
            if dp:
                return dp

    state = _parse_initial_state(html)
    if not state:
        return None
    note = _note_detail(state, note_id)
    if not isinstance(note, dict):
        return None
    sec = _pick_publish_seconds(note)
    if sec is None:
        return None
    return _seconds_to_date_prefix_utc(sec)


def xiaohongshu_metadata_from_video_page(url: str) -> dict | None:
    """返回兼容 ``preferred_title`` / ``upload_date_prefix`` 的扁平字段（``title``, ``timestamp``）。"""
    try:
        if not is_xiaohongshu_like_url(url):
            return None
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            page_urls = resolve_xhs_note_page_urls(url, client=client)
            for page_url in page_urls:
                r = client.get(page_url, headers=_request_headers())
                if r.status_code != 200:
                    continue
                html = r.text
                note_id = _note_id_from_url(str(r.url)) or _note_id_from_url(page_url)
                if not note_id:
                    continue
                meta = _metadata_from_page(html, note_id)
                if meta:
                    return meta
        return None
    except Exception:
        return None


def xiaohongshu_publish_date_prefix_from_page(url: str) -> str | None:
    """GET 笔记页并解析 INITIAL_STATE；成功返回 ``YYYY-MM-DD``，失败返回 None。"""
    meta = xiaohongshu_metadata_from_video_page(url)
    if meta:
        ts = meta.get("timestamp")
        if ts is not None:
            dp = _seconds_to_date_prefix_utc(float(ts))
            if dp:
                return dp
    try:
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            page_urls = resolve_xhs_note_page_urls(url, client=client)
            for page_url in page_urls:
                r = client.get(page_url, headers=_request_headers())
                if r.status_code != 200:
                    continue
                note_id = _note_id_from_url(str(r.url)) or _note_id_from_url(page_url)
                if not note_id:
                    continue
                dp = _date_prefix_from_html(r.text, note_id)
                if dp:
                    return dp
        return None
    except Exception:
        return None
