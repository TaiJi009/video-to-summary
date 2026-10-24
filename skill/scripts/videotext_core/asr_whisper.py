"""本地 whisper 引擎（faster-whisper，可选依赖）：接口与 asr_paraformer.transcribe 对齐。

未安装 faster-whisper 时抛 WhisperNotInstalledError（含安装指引）。
"""

from __future__ import annotations

from pathlib import Path

from . import config


class WhisperNotInstalledError(Exception):
    """faster-whisper 未安装。"""


class WhisperError(Exception):
    """本地转写失败（用户可读 message）。"""


def _ensure_installed():
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise WhisperNotInstalledError(
            "本地 whisper 引擎未安装。请在本 skill 仓库执行："
            "./setup.sh --with-whisper（或 .venv/bin/pip install faster-whisper）。"
            "首次运行会下载模型（WHISPER_MODEL 默认 small 约 460MB），"
            "国内网络建议先 export HF_ENDPOINT=https://hf-mirror.com"
        ) from None


def whisper_model_name() -> str:
    import os

    return (os.environ.get("WHISPER_MODEL") or "").strip() or "small"


def transcribe(media_path: Path) -> dict:
    """本地转写。返回 {"transcript": str, "sentences": [{"begin_ms","end_ms","text"}]}。"""
    _ensure_installed()
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(
            whisper_model_name(),
            device="auto",
            compute_type="auto",
        )
        segments_iter, info = model.transcribe(
            str(media_path),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        segments = list(segments_iter)
    except WhisperNotInstalledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WhisperError(f"本地 whisper 转写失败：{exc}"[:500]) from exc

    sentences = []
    texts = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        sentences.append(
            {
                "begin_ms": int(round(seg.start * 1000)),
                "end_ms": int(round(seg.end * 1000)),
                "text": text,
            }
        )
        texts.append(text)
    if info.duration and not sentences:
        raise WhisperError("本地转写完成但未识别到语音（可能是纯音乐或静音视频）")
    return {"transcript": "".join(texts).strip(), "sentences": sentences}
