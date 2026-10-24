"""B 站稿件页：解析 ``window.__INITIAL_STATE__`` 补全标题与 ``pubdate``（仅元数据，不请求 playinfo）。

yt-dlp 的 B 站 extractor 在 ``_real_extract`` 内仍会拉取播放流信息；风控/412/接口失败会导致整条预览链路失败。
此处仅解析嵌入在 HTML 中的 ``videoData`` / ``videoInfo``，足够工作台预览标题与上传日期。
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import httpx

from . import config


def read_link_preview_bilibili_cookie():
    return config.bilibili_cookie()
from yt_dlp.utils import js_to_json


_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv", re.I)
_B23_HOST_RE = re.compile(r"b23\.tv", re.I)
_BV_PATH_RE = re.compile(r"bilibili\.com/video/(BV[\w]+)", re.I)
_AID_PATH_RE = re.compile(r"bilibili\.com/video/av(\d+)", re.I)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\'])',
    re.I,
)

_last_bili_resolve_hint: str | None = None


def is_bilibili_like_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return bool(_BILI_HOST_RE.search(url))


def get_bili_resolve_hint() -> str | None:
    """预览全部策略失败时的用户可读说明。"""
    return _last_bili_resolve_hint


def clear_bili_resolve_hint() -> None:
    """元数据解析成功时清除失败提示。"""
    global _last_bili_resolve_hint
    _last_bili_resolve_hint = None


def set_bili_preview_failure_hint(message: str) -> None:
    """全部 B 站预览策略失败后设置提示。"""
    global _last_bili_resolve_hint
    _last_bili_resolve_hint = message


def get_bili_preview_hint() -> str:
    """B 站预览失败时的用户可读说明（云端数据中心 IP 常需 Cookie）。"""
    resolve_hint = (get_bili_resolve_hint() or "").strip()
    if resolve_hint:
        return resolve_hint

    ck = (read_link_preview_bilibili_cookie() or "").strip()
    if not ck:
        return (
            "B 站链接未能解析，请在设置页配置 B 站 Cookie（LINK_PREVIEW_BILIBILI_COOKIE），"
            "或改粘贴 bilibili.com 长链"
        )
    return "B 站链接未能解析，请更新设置页中的 B 站 Cookie，或改粘贴 bilibili.com 长链"


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
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}


def _request_headers() -> dict[str, str]:
    h = dict(_DEFAULT_HEADERS)
    ck = (read_link_preview_bilibili_cookie() or "").strip()
    if ck:
        h["Cookie"] = ck
    return h


def is_b23_short_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return bool(_B23_HOST_RE.search(url))


def _canonical_video_page_url(url: str) -> str | None:
    m = _BV_PATH_RE.search(url)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    m = _AID_PATH_RE.search(url)
    if m:
        return f"https://www.bilibili.com/video/av{m.group(1)}"
    return None


def _extract_bv_from_url(url: str) -> str | None:
    m = _BV_PATH_RE.search(url)
    return m.group(1) if m else None


def _extract_bv_from_html(html: str) -> str | None:
    m = re.search(r'"bvid"\s*:\s*"(BV[\w]+)"', html)
    if m:
        return m.group(1)
    m = _BV_PATH_RE.search(html)
    if m:
        return m.group(1)
    return None


def _is_captcha_or_risk_page(html: str) -> bool:
    low = html.lower()
    if "验证码" in html or "captcha" in low or "geetest" in low:
        return True
    if "window.__INITIAL_STATE__" not in html and "og:title" not in low:
        return True
    return False


def _http_status_hint(status_code: int) -> str:
    if status_code == 412:
        return (
            "B 站风控拦截(HTTP 412)，请更新设置页 B 站 Cookie，"
            "或改粘贴 bilibili.com 长链"
        )
    return f"B 站短链请求失败 HTTP {status_code}"


def _risk_page_hint(*, has_cookie: bool) -> str:
    if has_cookie:
        return (
            "B 站短链被风控拦截（验证码页），请更新设置页中的 B 站 Cookie，"
            "或改粘贴 bilibili.com 长链"
        )
    return (
        "B 站短链被风控拦截（验证码页），请在设置页配置有效 B 站 Cookie，"
        "或改粘贴 bilibili.com 长链"
    )


def _resolve_from_response(url: str, html: str) -> str | None:
    resolved = _canonical_video_page_url(url)
    if resolved:
        return resolved
    bv = _extract_bv_from_html(html)
    if bv:
        return f"https://www.bilibili.com/video/{bv}"
    return None


def _resolve_via_curl_cffi_get(sid: str) -> str | None:
    try:
        from curl_cffi import requests as curl_requests

        r = curl_requests.get(
            sid,
            headers=_request_headers(),
            timeout=30,
            impersonate="chrome131",
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        html = r.text
        if _is_captcha_or_risk_page(html):
            return None
        return _resolve_from_response(str(r.url), html)
    except Exception:
        return None


def _resolve_via_head_location(sid: str) -> str | None:
    """跟随 Location 头展开短链，无需拉取完整页面。"""
    try:
        from curl_cffi import requests as curl_requests

        current = sid
        for _ in range(10):
            r = curl_requests.head(
                current,
                headers=_request_headers(),
                timeout=30,
                impersonate="chrome131",
                allow_redirects=False,
            )
            location = r.headers.get("Location") or r.headers.get("location")
            if location:
                next_url = urljoin(str(r.url), location)
                canonical = _canonical_video_page_url(next_url)
                if canonical:
                    return canonical
                if r.status_code in (301, 302, 303, 307, 308):
                    current = next_url
                    continue
            canonical = _canonical_video_page_url(str(r.url))
            if canonical:
                return canonical
            break
    except Exception:
        pass

    try:
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=False) as client:
            current = sid
            for _ in range(10):
                r = client.head(current, headers=_request_headers())
                location = r.headers.get("location")
                if location:
                    next_url = urljoin(str(r.url), location)
                    canonical = _canonical_video_page_url(next_url)
                    if canonical:
                        return canonical
                    if r.status_code in (301, 302, 303, 307, 308):
                        current = next_url
                        continue
                canonical = _canonical_video_page_url(str(r.url))
                if canonical:
                    return canonical
                break
    except Exception:
        pass
    return None


def _resolve_via_httpx_get(sid: str, *, client: httpx.Client | None = None) -> str | None:
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True)
    assert client is not None
    try:
        r = client.get(sid, headers=_request_headers())
        if r.status_code != 200:
            return None
        html = r.text
        if _is_captcha_or_risk_page(html):
            return None
        return _resolve_from_response(str(r.url), html)
    except Exception:
        return None
    finally:
        if own_client:
            client.close()


def resolve_bilibili_video_page_url(url: str, *, client: httpx.Client | None = None) -> str | None:
    """将 b23.tv 短链展开为 ``bilibili.com/video/...`` 稿件页 URL。

    多策略：curl_cffi GET → HEAD Location → httpx GET。不在此处设置用户提示。
    """
    if not is_bilibili_like_url(url):
        return None

    sid = url.strip()
    canonical = _canonical_video_page_url(sid)
    if canonical and not is_b23_short_url(sid):
        return canonical

    resolved = _resolve_via_curl_cffi_get(sid)
    if resolved:
        return resolved

    resolved = _resolve_via_head_location(sid)
    if resolved:
        return resolved

    resolved = _resolve_via_httpx_get(sid, client=client)
    if resolved:
        return resolved

    return None


def _title_from_og_meta(html: str) -> str | None:
    m = _OG_TITLE_RE.search(html)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").strip()
    if not raw:
        return None
    for suffix in ("_哔哩哔哩_bilibili", " - bilibili", " | bilibili"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
    return raw or None


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


def _initial_state_blocking_error(state: dict) -> bool:
    """INITIAL_STATE 内错误码（登录墙 / 下架等）。"""
    err = state.get("error")
    if isinstance(err, dict):
        code = err.get("trueCode")
        if code in (-403, -404):
            return True
    return False


def effective_bilibili_fetch_url(url: str) -> str:
    """短链优先展开为稿件页，便于 yt-dlp 与 HTML 解析共用同一 canonical URL。"""
    sid = url.strip()
    if is_b23_short_url(sid):
        resolved = resolve_bilibili_video_page_url(sid)
        if resolved:
            return resolved
    canonical = _canonical_video_page_url(sid)
    return canonical or sid


def _bvid_for_metadata(url: str) -> str | None:
    fetch_url = effective_bilibili_fetch_url(url)
    bv = _extract_bv_from_url(fetch_url)
    if bv:
        return bv
    if is_b23_short_url(url.strip()):
        resolved = resolve_bilibili_video_page_url(url.strip())
        if resolved:
            return _extract_bv_from_url(resolved)
    return None


def bilibili_metadata_from_video_page(url: str) -> dict | None:
    """返回兼容 ``preferred_title`` / ``upload_date_prefix`` 的扁平字段（``title``, ``timestamp``）。

    使用 httpx 请求 B 站页面（完整浏览器 UA + 可选 Cookie），解析 ``window.__INITIAL_STATE__``。
    数据中心 IP 可能被限流，此时由 ``bilibili_metadata_from_video_page_curl_cffi`` 兜底。
    """
    try:
        if not is_bilibili_like_url(url):
            return None
        fetch_url = effective_bilibili_fetch_url(url)
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            r = client.get(fetch_url, headers=_request_headers())
            if r.status_code != 200:
                return None
            html = r.text

        meta = _parse_metadata_from_html(html)
        if meta:
            clear_bili_resolve_hint()
        return meta
    except Exception:
        return None


def bilibili_metadata_from_video_page_curl_cffi(url: str) -> dict | None:
    """与 ``bilibili_metadata_from_video_page`` 同语义，但使用 curl_cffi 发请求。

    curl_cffi 模拟浏览器 TLS 指纹与 JA3，比 httpx 更不容易被 B 站 CDN/WAF 识别为机器人。
    适合阿里云 ECS 等数据中心 IP 场景，作为 httpx 兜底。
    """
    try:
        if not is_bilibili_like_url(url):
            return None
        from curl_cffi import requests as curl_requests

        h = _request_headers()
        fetch_url = effective_bilibili_fetch_url(url)
        r = curl_requests.get(
            fetch_url,
            headers=h,
            timeout=30,
            impersonate="chrome131",
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        html = r.text
        meta = _parse_metadata_from_html(html)
        if meta:
            clear_bili_resolve_hint()
        return meta
    except Exception:
        return None


def bilibili_metadata_from_view_api(url: str) -> dict | None:
    """通过 B 站 view API 获取标题与 pubdate（HTML 被风控时的兜底）。"""
    if not is_bilibili_like_url(url):
        return None

    bvid = _bvid_for_metadata(url)
    if not bvid:
        return None

    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    headers = _request_headers()
    headers["Accept"] = "application/json"

    for fetcher in (_fetch_view_api_curl_cffi, _fetch_view_api_httpx):
        meta = fetcher(api_url, headers)
        if meta:
            clear_bili_resolve_hint()
            return meta
    return None


def _parse_view_api_payload(payload: dict) -> dict | None:
    if payload.get("code") != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    out: dict = {"title": title.strip()}
    pub = data.get("pubdate")
    if isinstance(pub, int) and pub > 0:
        out["timestamp"] = pub
    return out


def _fetch_view_api_curl_cffi(api_url: str, headers: dict[str, str]) -> dict | None:
    try:
        from curl_cffi import requests as curl_requests

        r = curl_requests.get(
            api_url,
            headers=headers,
            timeout=30,
            impersonate="chrome131",
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        payload = r.json()
        if not isinstance(payload, dict):
            return None
        return _parse_view_api_payload(payload)
    except Exception:
        return None


def _fetch_view_api_httpx(api_url: str, headers: dict[str, str]) -> dict | None:
    try:
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            r = client.get(api_url, headers=headers)
            if r.status_code != 200:
                return None
            payload = r.json()
            if not isinstance(payload, dict):
                return None
            return _parse_view_api_payload(payload)
    except Exception:
        return None


def infer_bilibili_preview_failure_hint(url: str) -> str:
    """全部预览策略失败后，生成最可能的原因说明。"""
    has_cookie = bool((read_link_preview_bilibili_cookie() or "").strip())
    sid = (url or "").strip()

    if is_b23_short_url(sid):
        try:
            from curl_cffi import requests as curl_requests

            r = curl_requests.get(
                sid,
                headers=_request_headers(),
                timeout=30,
                impersonate="chrome131",
                allow_redirects=True,
            )
            if r.status_code == 412:
                return _http_status_hint(412)
            if r.status_code != 200 and _is_captcha_or_risk_page(getattr(r, "text", "")):
                return _risk_page_hint(has_cookie=has_cookie)
        except Exception:
            pass

        try:
            with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
                r = client.get(sid, headers=_request_headers())
                if r.status_code == 412:
                    return _http_status_hint(412)
                if r.status_code != 200:
                    return _http_status_hint(r.status_code)
                if _is_captcha_or_risk_page(r.text):
                    return _risk_page_hint(has_cookie=has_cookie)
        except Exception:
            pass

    if has_cookie:
        return (
            "B 站链接未能解析，请更新设置页中的 B 站 Cookie，"
            "或改粘贴 bilibili.com 长链"
        )
    return (
        "B 站链接未能解析，请在设置页配置 B 站 Cookie（LINK_PREVIEW_BILIBILI_COOKIE），"
        "或改粘贴 bilibili.com 长链"
    )


def _parse_metadata_from_html(html: str) -> dict | None:
    """解析 HTML 中的 ``window.__INITIAL_STATE__`` 或 og:title，提取 title 与 pubdate。"""
    state = _parse_initial_state(html)
    if state and _initial_state_blocking_error(state):
        return None

    vd = None
    if isinstance(state, dict):
        if isinstance(state.get("videoData"), dict):
            vd = state["videoData"]
        elif isinstance(state.get("videoInfo"), dict):
            vd = state["videoInfo"]

    title: str | None = None
    if isinstance(vd, dict):
        raw_title = vd.get("title")
        if isinstance(raw_title, str):
            title = raw_title.strip() or None
    if not title:
        title = _title_from_og_meta(html)
    if not title:
        return None

    out: dict = {"title": title}
    if not isinstance(vd, dict):
        return out
    pub = vd.get("pubdate")
    if isinstance(pub, int) and pub > 0:
        out["timestamp"] = pub
    return out
