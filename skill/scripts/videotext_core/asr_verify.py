"""DashScope API key 可用性验证（零额度：只调 getPolicy 鉴权接口，不提交转写任务）。

注意：getPolicy 只能证明 key 有效；模型是否已在百炼控制台"开通"要等首次真实调用才可知。
"""

from __future__ import annotations

import httpx

from . import config
from .asr_paraformer import _headers

CHECK_MODELS = ("paraformer-v2", "paraformer-realtime-v2")


def _check_one(api_key: str, model: str) -> dict:
    params = {"action": "getPolicy", "model": model}
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            r = client.get(config.DASHSCOPE_UPLOAD_POLICY_URL, params=params, headers=_headers(api_key))
    except httpx.HTTPError as exc:
        return {"status": "error", "http_status": None, "detail": str(exc)[:300]}
    if r.status_code == 200:
        return {"status": "ok", "http_status": 200, "detail": ""}
    if r.status_code in (401, 403):
        return {"status": "invalid", "http_status": r.status_code, "detail": r.text[:300]}
    return {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}


def verify_key(api_key: str) -> dict:
    checks = {model: _check_one(api_key, model) for model in CHECK_MODELS}
    statuses = {c["status"] for c in checks.values()}
    if statuses == {"ok"}:
        overall = "ok"
    elif "invalid" in statuses:
        overall = "invalid"
    else:
        overall = "error"
    return {"status": overall, "checks": checks}
