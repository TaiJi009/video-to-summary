#!/usr/bin/env python3
"""视频批量转写 CLI：URL → 逐字稿（SRT 时间轴 + TXT）。

用法：
  run   --url <URL> [--url ...] | --urls-file urls.txt
        [--output-dir DIR] [--remember] [--max-workers N] [--verbose]
  status <run_id> [--output-dir DIR]

产物：<output-dir>/<run_id>/manifest.json + tasks/NNN/（*.srt、*.txt、*.mp4）。
总结由调用方 agent 的模型完成（读 transcript.txt 写 summary.md），CLI 不调用任何 LLM。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from videotext_core import config, manifest  # noqa: E402
from videotext_core.asr_paraformer import ASRError, NoSpeechError, transcribe  # noqa: E402
from videotext_core.asr_whisper import (  # noqa: E402
    WhisperError,
    WhisperNotInstalledError,
    transcribe as whisper_transcribe,
)
from videotext_core.downloader import (  # noqa: E402
    allocate_unique_stem,
    download_with_ytdlp,
    effective_download_url,
    find_downloaded_video,
    strip_ansi,
    try_platform_subtitles,
)
from videotext_core.filename_safe import sanitize_filename  # noqa: E402
from videotext_core.media import (  # noqa: E402
    MediaPrepareError,
    extract_audio_track,
    probe_media_duration_seconds,
)
from videotext_core.metadata import (  # noqa: E402
    detect_platform,
    display_title_with_publish_date,
    extract_duration_seconds,
    extract_info_metadata_only,
    format_video_publish_label,
    normalize_urls,
    preferred_title,
    upload_date_prefix_with_fallback,
)
from videotext_core.subtitles import write_srt_with_timestamps, write_txt  # noqa: E402


def _progress_printer(task_desc: str, stage: str):
    def _print(pct: int, detail: str = "") -> None:
        line = f"[{task_desc}] {stage} {pct}%"
        if detail:
            line += f" - {detail[:160]}"
        print(line, flush=True)

    return _print


def _classify_asr_error(err: str) -> str:
    if "Arrearage" in err or "Access denied" in err or "欠费" in err:
        return "ASR_ARREARAGE"
    if "403" in err or "InvalidApiKey" in err or "未配置" in err:
        return "ASR_AUTH"
    if "429" in err or "Throttling" in err or "限流" in err:
        return "ASR_QUOTA"
    return "ASR_FAILED"


def process_single_task(run_dir: Path, entry: dict) -> None:
    """处理单条任务：字幕轨捷径 → 下载 → 抽音轨 → paraformer 转写 → 双产物落盘。"""
    url = entry["url"]
    idx = entry["index"]
    task_dir = run_dir / "tasks" / f"{idx:03d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    entry["output_dir"] = str(task_dir.resolve())
    entry["platform"] = detect_platform(url)
    manifest.write_manifest(run_dir, RUN_STATE["manifest"])

    log = _progress_printer(f"#{idx:03d}", "task")

    try:
        # 1. 元数据（不下载）
        display_title = None
        meta = extract_info_metadata_only(url)
        if meta and preferred_title(meta):
            raw_title = preferred_title(meta)
            date_prefix = upload_date_prefix_with_fallback(meta, url)
            display_title = display_title_with_publish_date(raw_title, date_prefix)[:2000]
        entry["display_title"] = display_title
        stem = sanitize_filename(display_title or "video")
        log(10, f"元数据: {display_title or url}")

        # 2. 平台字幕轨捷径（零成本零等待；--languages 控制多语言拉取）
        languages = RUN_STATE.get("languages")  # "all" | "zh,en,es" | None
        if languages == "all" or languages is None:
            lang_arg = languages
        else:
            lang_arg = [x.strip() for x in str(languages).split(",") if x.strip()]
        subtitle_path = try_platform_subtitles(url, task_dir, stem, lang_arg)
        if subtitle_path is not None:
            entry["asr_source"] = "platform_subtitle"
            entry["subtitle_path"] = str(subtitle_path.resolve())
            from videotext_core.downloader import get_subtitle_languages

            entry["subtitle_languages"] = get_subtitle_languages(task_dir, stem)
            from videotext_core.subtitles import read_subtitle_plain_text

            transcript_txt = task_dir / f"{stem}.txt"
            write_txt(transcript_txt, read_subtitle_plain_text(subtitle_path) or "")
            entry["transcript_path"] = str(transcript_txt.resolve())
            entry["status"] = "completed"
            extra = f"（含 {len(entry['subtitle_languages'])} 个语言轨）" if entry["subtitle_languages"] else ""
            log(100, f"使用平台现成字幕: {subtitle_path.name}{extra}")
            return

        # 3. 下载
        fetch_url = effective_download_url(url)
        dl_stem = allocate_unique_stem(task_dir, stem)
        outtmpl = str(task_dir / f"{dl_stem}.%(ext)s")
        dl_info = download_with_ytdlp(fetch_url, outtmpl)
        video_path = find_downloaded_video(task_dir, dl_stem)
        if video_path is None:
            entry["status"] = "failed"
            entry["error_code"] = "DOWNLOAD_FAILED"
            entry["error_detail"] = "下载结束但未找到视频文件（可能需要 ffmpeg 合并音视频）"
            return
        duration = extract_duration_seconds(dl_info) if isinstance(dl_info, dict) else None
        if duration is None:
            duration = probe_media_duration_seconds(video_path)
        entry["duration_seconds"] = duration
        log(25, f"下载完成: {video_path.name} ({duration or '?'}s)")

        # 4. 时长上限检查（云端引擎单文件 2 小时；whisper 无此限制）
        engine = RUN_STATE.get("engine", "dashscope")
        if engine == "dashscope" and duration and duration > config.ASR_MAX_FILE_SECONDS:
            entry["status"] = "failed"
            entry["error_code"] = "OVER_LENGTH"
            entry["error_detail"] = (
                f"音频时长约 {int(duration // 60)} 分钟，超过云端引擎单文件上限 120 分钟。"
                "建议 --asr-engine whisper 本地转写，或人工分段。"
            )
            return

        # 5. 抽音轨 → ASR 引擎转写
        audio_path = task_dir / "audio.16k.mp3"
        extract_audio_track(video_path, audio_path)
        log(40, f"音轨就绪: {audio_path.name}")

        if engine == "whisper":
            result = whisper_transcribe(audio_path)
            entry["asr_source"] = "whisper"
        else:
            result = transcribe(config.dashscope_api_key(), audio_path)
            entry["asr_source"] = "paraformer"
        log(85, f"转写完成: {len(result['transcript'])} 字")

        # 6. 双产物：真时间轴 SRT + 纯文本 TXT
        srt_path = task_dir / f"{stem}.srt"
        write_srt_with_timestamps(srt_path, result["sentences"])
        transcript_txt = task_dir / f"{stem}.txt"
        write_txt(transcript_txt, result["transcript"])
        entry["subtitle_path"] = str(srt_path.resolve())
        entry["transcript_path"] = str(transcript_txt.resolve())
        entry["status"] = "completed"
        log(100, "全部完成")

    except ASRError as e:
        entry["status"] = "failed"
        entry["error_code"] = _classify_asr_error(str(e))
        entry["error_detail"] = str(e)[:800]
    except WhisperNotInstalledError as e:
        entry["status"] = "failed"
        entry["error_code"] = "WHISPER_NOT_INSTALLED"
        entry["error_detail"] = str(e)[:800]
    except WhisperError as e:
        entry["status"] = "failed"
        entry["error_code"] = "ASR_FAILED"
        entry["error_detail"] = str(e)[:800]
    except NoSpeechError as e:
        # 静音/纯音乐：任务本身成功，按 completed 收尾但标注说明
        entry["status"] = "completed"
        entry["asr_source"] = "paraformer"
        entry["error_code"] = "NO_SPEECH_DETECTED"
        entry["error_detail"] = str(e)[:300]
        log(100, str(e))
    except MediaPrepareError as e:
        entry["status"] = "failed"
        entry["error_code"] = "AUDIO_EXTRACT_FAILED"
        entry["error_detail"] = str(e)[:800]
    except Exception as e:  # noqa: BLE001
        entry["status"] = "failed"
        if entry.get("error_code") is None:
            entry["error_code"] = (
                "DOWNLOAD_FAILED" if entry.get("subtitle_path") is None else "ASR_FAILED"
            )
        entry["error_detail"] = (strip_ansi(str(e)) or type(e).__name__)[:800]
        if RUN_STATE.get("verbose"):
            traceback.print_exc()


RUN_STATE: dict = {}


def cmd_run(args: argparse.Namespace) -> int:
    urls: list[str]
    if args.urls_file:
        raw = Path(args.urls_file).read_text(encoding="utf-8")
        urls = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    elif args.url:
        urls = list(args.url)
    else:
        print(json.dumps({"error": "必须提供 --url 或 --urls-file"}), file=sys.stderr)
        return 2

    urls, err = normalize_urls(urls)
    if err:
        print(json.dumps({"error": f"URL 非法（{err}）"}), file=sys.stderr)
        return 2

    engine = args.asr_engine
    RUN_STATE["engine"] = engine
    if engine == "dashscope" and config.dashscope_api_key() is None:
        print(
            json.dumps({
                "error": "未配置 DASHSCOPE_API_KEY（云端引擎必需）。请询问用户选择："
                "① 在 skill/.env 配置有效 key 后重试；② 若平台有现成字幕轨，单独走字幕轨捷径；"
                "③ --asr-engine whisper 本地转写（免额度，需先 ./setup.sh --with-whisper）。",
            }, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    saved_pref = False
    if args.output_dir:
        output_root = Path(args.output_dir).expanduser().resolve()
        if args.remember:
            config.save_default_output_dir(str(output_root))
            saved_pref = True
    else:
        pref = config.read_default_output_dir()
        if pref:
            output_root = Path(pref).expanduser().resolve()
        else:
            print(
                json.dumps({
                    "error": "未设置默认产物目录。请询问用户希望把产物存放到哪里，"
                    "然后用 --output-dir <路径> --remember 调用以记住该选择。",
                }, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
    output_root.mkdir(parents=True, exist_ok=True)
    if saved_pref:
        print(f"default_output_dir saved: {output_root}", flush=True)
    run_id = manifest.new_run_id()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    m = manifest.create_manifest(run_dir, run_id, urls)
    RUN_STATE["manifest"] = m
    RUN_STATE["verbose"] = args.verbose
    RUN_STATE["languages"] = args.languages

    max_workers = max(1, min(int(args.max_workers), len(urls)))

    print(f"run_id: {run_id}", flush=True)
    print(f"run_dir: {run_dir}", flush=True)

    def _refresh(entry: dict) -> None:
        manifest.refresh_aggregates(m)
        manifest.write_manifest(run_dir, m)

    if max_workers <= 1:
        for entry in m["tasks"]:
            process_single_task(run_dir, entry)
            _refresh(entry)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(process_single_task, run_dir, entry): entry
                for entry in m["tasks"]
            }
            for fut in as_completed(futures):
                _refresh(futures[fut])

    manifest.finalize_manifest(run_dir, m)
    print(json.dumps(manifest.status_summary(m), ensure_ascii=False))
    return 0 if m["status"] == "completed" else 1


def cmd_status(args: argparse.Namespace) -> int:
    if args.output_dir:
        output_root = Path(args.output_dir).expanduser().resolve()
    else:
        pref = config.read_default_output_dir()
        if pref:
            output_root = Path(pref).expanduser().resolve()
        else:
            print(json.dumps({"error": "未设置默认产物目录，请用 --output-dir 指定"}), file=sys.stderr)
            return 2
    run_dir = output_root / args.run_id
    m = manifest.load_manifest(run_dir)
    if m is None:
        print(json.dumps({"error": f"未找到 run: {args.run_id}"}), file=sys.stderr)
        return 2
    print(json.dumps(manifest.status_summary(m), ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="视频批量转写 CLI（逐字稿）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="批量转写")
    p_run.add_argument("--url", action="append", help="视频 URL（可多次）")
    p_run.add_argument("--urls-file", help="每行一个 URL 的文本文件")
    p_run.add_argument("--output-dir", help="产物根目录（首次使用时必须提供，配合 --remember 记住）")
    p_run.add_argument("--remember", action="store_true", help="把 --output-dir 保存为默认，后续省略")
    p_run.add_argument("--max-workers", type=int, default=3)
    p_run.add_argument("--asr-engine", choices=("dashscope", "whisper"), default="dashscope",
                       help="ASR 引擎：dashscope 云端（默认，需 key）或 whisper 本地（免额度）")
    p_run.add_argument("--languages", default=None, metavar="all|zh,en,..",
                       help="多语言字幕拉取：all=全部轨；逗号列表=指定语言；缺省仅主语言（仅字幕轨来源有效，ASR 引擎只出音视频原语）")
    p_run.add_argument("--verbose", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="查询 run 状态")
    p_status.add_argument("run_id")
    p_status.add_argument("--output-dir", help="产物根目录（省略时读 preferences.json 默认）")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
