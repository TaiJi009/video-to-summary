"""字幕产物：纯文本转换、SRT 时间轴写盘（真时间轴来自 paraformer sentences）。"""

from __future__ import annotations

from pathlib import Path


def ms_to_srt_time(ms: int | float) -> str:
    """毫秒 → SRT 时间戳 "HH:MM:SS,mmm"（负数按 0 处理）。"""
    total = max(0, int(round(ms)))
    h = total // 3_600_000
    m = (total % 3_600_000) // 60_000
    s = (total % 60_000) // 1000
    ms_part = total % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part:03d}"


def write_srt_with_timestamps(path: Path, sentences: list[dict]) -> None:
    """按句级时间戳写规范 SRT；sentences: [{"begin_ms","end_ms","text"}]。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for i, sent in enumerate(sentences, start=1):
        begin = ms_to_srt_time(sent.get("begin_ms") or 0)
        end = ms_to_srt_time(sent.get("end_ms") or 0)
        text = (sent.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"{i}\n{begin} --> {end}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def write_txt(path: Path, transcript: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = transcript.replace("\r\n", "\n").strip()
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def srt_to_plain(text: str) -> str:
    blocks = text.replace("\r\n", "\n").strip().split("\n\n")
    lines_out: list[str] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        i = 0
        if lines[0].isdigit():
            i += 1
        if i < len(lines) and "-->" in lines[i]:
            i += 1
        dialogue = "\n".join(lines[i:])
        if dialogue:
            lines_out.append(dialogue)
    return "\n".join(lines_out).strip()


def read_subtitle_plain_text(subtitle_path: Path) -> str | None:
    """读取字幕文件并转为纯文本（SRT 去时间轴）；失败/为空返回 None。"""
    fmt = "txt" if subtitle_path.suffix.lower() == ".txt" else "srt"
    try:
        raw = subtitle_path.read_text(encoding="utf-8")
    except OSError:
        return None
    plain = raw.strip() if fmt == "txt" else srt_to_plain(raw)
    plain = (plain or "").strip()
    return plain or None


def convert_to_srt_file(src_path: Path, dst_path: Path) -> None:
    """把外部字幕（srt/vtt）规范成 SRT 落盘。vtt 头部与毫秒点号做最小转换。"""
    raw = src_path.read_text(encoding="utf-8", errors="replace")
    if src_path.suffix.lower() == ".vtt":
        lines = raw.splitlines()
        # 去掉 WEBVTT 头块，时间戳 00:00:01.000 → 00:00:01,000
        out_lines: list[str] = []
        for ln in lines:
            if ln.strip().upper().startswith("WEBVTT"):
                continue
            if "-->" in ln:
                ln = ln.replace(".", ",")
                # vtt 可能带行内位置参数，去掉时间戳后的部分
                parts = ln.split(" --> ")
                if len(parts) == 2 and " " in parts[1]:
                    parts[1] = parts[1].split(" ")[0]
                ln = " --> ".join(parts)
            out_lines.append(ln)
        raw = "\n".join(out_lines).strip()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(raw + "\n", encoding="utf-8")
