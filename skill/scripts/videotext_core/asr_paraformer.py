"""DashScope paraformer-v2 录音文件识别（异步任务：上传 → 提交 → 轮询 → 解析）。

单文件上限 2 小时 / 2GB，免切片；句级时间戳来自 transcription_url 结果 JSON。
"""

from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path

import httpx

from . import config


class ASRError(Exception):
    """转写失败（用户可读 message）。"""


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _async_headers(api_key: str) -> dict[str, str]:
    # 异步任务提交专用头；getPolicy/轮询等 GET 不带。
    # OssResourceResolve：file_urls 里的 oss:// URI 需显式声明才会被服务端解析
    return {
        **_headers(api_key),
        "X-DashScope-Async": "enable",
        "X-DashScope-OssResourceResolve": "enable",
    }


def _upload_audio(api_key: str, media_path: Path) -> str:
    """本地文件经 DashScope 上传接口存 OSS，返回 oss:// URI。"""
    params = {"action": "getPolicy", "model": config.ASR_MODEL}
    try:
        with httpx.Client(timeout=60.0, trust_env=False) as client:
            r = client.get(
                config.DASHSCOPE_UPLOAD_POLICY_URL,
                params=params,
                headers=_headers(api_key),
            )
    except httpx.HTTPError as exc:
        raise ASRError(f"获取上传凭证失败：{exc}") from exc
    if r.status_code != 200:
        raise ASRError(f"获取上传凭证失败：HTTP {r.status_code} {r.text[:300]}")
    js = r.json()
    # 新版响应包在 data 下，旧 SDK 兼容 output
    info = js.get("data") or js.get("output") or {}

    form = {
        "OSSAccessKeyId": info.get("oss_access_key_id"),
        "Signature": info.get("signature"),
        "policy": info.get("policy"),
        "key": f"{info.get('upload_dir')}/{media_path.name}",
        "x-oss-object-acl": info.get("x_oss_object_acl"),
        "x-oss-forbid-overwrite": info.get("x_oss_forbid_overwrite"),
        "success_action_status": "200",
        "x-oss-content-type": mimetypes.guess_type(str(media_path))[0]
        or "application/octet-stream",
    }
    if not all(form.values()):
        raise ASRError(f"上传凭证字段缺失：{sorted(k for k, v in form.items() if not v)}")

    try:
        with httpx.Client(timeout=3600.0, trust_env=False) as client:
            with media_path.open("rb") as f:
                ur = client.post(info["upload_host"], data=form, files={"file": f})
    except httpx.HTTPError as exc:
        raise ASRError(f"上传音频失败：{exc}") from exc
    if ur.status_code != 200:
        raise ASRError(f"上传音频失败：HTTP {ur.status_code} {ur.text[:300]}")
    return "oss://" + form["key"]


def _submit_task(api_key: str, file_url: str) -> str:
    url = f"{config.DASHSCOPE_BASE}/api/v1/services/audio/asr/transcription"
    payload = {
        "model": config.ASR_MODEL,
        "input": {"file_urls": [file_url]},
        "parameters": {
            "diarization_enabled": False,
            "disfluency_removal_enabled": True,
        },
    }
    try:
        with httpx.Client(timeout=60.0, trust_env=False) as client:
            r = client.post(url, headers=_async_headers(api_key), json=payload)
    except httpx.HTTPError as exc:
        raise ASRError(f"提交转写任务失败：{exc}") from exc
    try:
        js = r.json()
    except ValueError:
        raise ASRError(f"提交转写任务失败：非 JSON 响应（HTTP {r.status_code}）")
    if r.status_code != 200:
        err = js.get("message") or js.get("code") or r.text[:300]
        raise ASRError(f"提交转写任务失败（HTTP {r.status_code}）：{err}")
    task_id = (js.get("output") or {}).get("task_id")
    if not task_id:
        raise ASRError(f"提交转写任务失败：响应缺少 task_id：{str(js)[:300]}")
    return task_id


class NoSpeechError(Exception):
    """任务成功但未检测到语音（静音/纯音乐视频的正常结果）。"""


def _poll_task(api_key: str, task_id: str) -> dict:
    """轮询到终态，SUCCEEDED 时返回 results[0]（含 transcription_url）。"""
    url = f"{config.DASHSCOPE_BASE}/api/v1/tasks/{task_id}"
    deadline = time.monotonic() + config.POLL_TIMEOUT_SECONDS
    with httpx.Client(timeout=60.0, trust_env=False) as client:
        while True:
            try:
                r = client.get(url, headers=_headers(api_key))
            except httpx.HTTPError as exc:
                raise ASRError(f"查询转写任务失败：{exc}") from exc
            try:
                js = r.json()
            except ValueError:
                raise ASRError(f"查询转写任务失败：非 JSON 响应（HTTP {r.status_code}）")
            if r.status_code != 200:
                err = js.get("message") or js.get("code") or r.text[:300]
                raise ASRError(f"查询转写任务失败（HTTP {r.status_code}）：{err}")
            output = js.get("output") or {}
            status = output.get("task_status")
            if status == "SUCCEEDED":
                results = output.get("results") or []
                if not results:
                    raise ASRError("转写完成但响应缺少 results")
                sub = (results[0].get("subtask_status") or "").upper()
                # "成功但无有效语音片段"：静音/纯音乐视频的正常结果
                if sub in ("SUCCESS_WITH_NO_VALID_FRAGMENT", "NO_VALID_FRAGMENT"):
                    raise NoSpeechError("未检测到有效语音（可能是纯音乐或静音视频）")
                return results[0]
            if status == "FAILED":
                code = output.get("code") or ""
                msg = output.get("message") or ""
                raise ASRError(f"转写任务失败：{code} {msg}"[:800])
            if status not in ("PENDING", "RUNNING"):
                raise ASRError(f"转写任务未知状态：{status}")
            if time.monotonic() > deadline:
                raise ASRError(
                    f"转写任务超时（>{config.POLL_TIMEOUT_SECONDS}s）：task_id={task_id}，"
                    "可稍后用 status 命令查询或重试"
                )
            time.sleep(config.POLL_INTERVAL_SECONDS)


def _download_result(api_key: str, result: dict) -> dict:
    """下载 transcription_url 指向的识别结果 JSON（transcripts[].sentences[]）。"""
    t_url = result.get("transcription_url")
    if not t_url:
        raise ASRError("转写完成但缺少 transcription_url")
    try:
        with httpx.Client(timeout=300.0, trust_env=False) as client:
            r = client.get(t_url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise ASRError(f"下载识别结果失败：{exc}") from exc
    if r.status_code != 200:
        raise ASRError(f"下载识别结果失败：HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise ASRError("识别结果非 JSON")


def _parse_result(data: dict) -> dict:
    transcripts = data.get("transcripts") or []
    sentences: list[dict] = []
    texts: list[str] = []
    for t in transcripts:
        for s in t.get("sentences") or []:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            sentences.append(
                {
                    "begin_ms": int(s.get("begin_time") or 0),
                    "end_ms": int(s.get("end_time") or 0),
                    "text": text,
                }
            )
            texts.append(text)
    transcript = data.get("transcript") or "".join(texts)
    if not transcript.strip() and not sentences:
        raise ASRError("识别结果为空")
    return {"transcript": transcript.strip(), "sentences": sentences}


def transcribe(api_key: str, media_path: Path) -> dict:
    """编排：上传 → 提交 → 轮询 → 下载解析。返回 {"transcript", "sentences"}。"""
    oss_uri = _upload_audio(api_key, media_path)
    task_id = _submit_task(api_key, oss_uri)
    result = _poll_task(api_key, task_id)
    data = _download_result(api_key, result)
    return _parse_result(data)
