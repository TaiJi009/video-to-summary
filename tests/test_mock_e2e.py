"""mock E2E：monkeypatch paraformer HTTP 链路与 yt-dlp，验证管线，不耗 API 额度。"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import video_batch_cli as cli
from videotext_core import config


# --- paraformer HTTP fake ---

class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeParaformerHTTP:
    """按 URL 分发：getPolicy / OSS form 上传 / 创建任务 / 轮询 / 结果下载。

    poll_sequence 控制轮询状态序列。
    """

    calls: list = []
    poll_sequence: list = []

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.poll_sequence = ["PENDING", "RUNNING", "SUCCEEDED"]

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        FakeParaformerHTTP.calls.append(("GET", url, params))
        if "action=getPolicy" in url or (params or {}).get("action") == "getPolicy":
            return FakeResponse({
                "output": {
                    "oss_access_key_id": "AK",
                    "signature": "SIG",
                    "policy": "POL",
                    "upload_dir": "asr/unstable",
                    "upload_host": "https://oss-upload.example",
                    "x_oss_object_acl": "private",
                    "x_oss_forbid_overwrite": "true",
                }
            })
        if "/tasks/" in url:
            status = FakeParaformerHTTP.poll_sequence.pop(0) if FakeParaformerHTTP.poll_sequence else "SUCCEEDED"
            output = {"task_id": "t-1", "task_status": status}
            if status == "SUCCEEDED":
                output["results"] = [{"transcription_url": "https://result.example/t-1.json"}]
            return FakeResponse({"output": output})
        if url.startswith("https://result.example/"):
            return FakeResponse({
                "transcript": "你好世界第二句",
                "transcripts": [{
                    "text": "你好世界第二句",
                    "sentences": [
                        {"begin_time": 0, "end_time": 1500, "text": "你好"},
                        {"begin_time": 2000, "end_time": 3600, "text": "世界第二句"},
                    ],
                }],
            })
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, data=None, files=None):
        FakeParaformerHTTP.calls.append(("POST", url, None))
        if url == "https://oss-upload.example":
            return FakeResponse(None, status_code=200)
        if url.endswith("/services/audio/asr/transcription"):
            assert json["model"] == "paraformer-v2"
            assert json["input"]["file_urls"][0].startswith("oss://")
            return FakeResponse({"output": {"task_id": "t-1", "task_status": "PENDING"}})
        raise AssertionError(f"unexpected POST {url}")


def patch_all(monkeypatch, tmp_path):
    FakeParaformerHTTP.reset()

    import videotext_core.asr_paraformer as asr_mod
    import videotext_core.media as media_mod

    monkeypatch.setattr(asr_mod.httpx, "Client", FakeParaformerHTTP)
    monkeypatch.setattr(config, "PREFERENCES_PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)

    # 假 ffmpeg：把"视频"（实为字节文件）直接"复制"为 16k mp3
    # 注意 patch cli 模块的绑定（CLI 用 from-import）
    def _fake_extract(video, dst, timeout=1800):
        dst.write_bytes(b"A" * 2000)
        return dst

    monkeypatch.setattr(cli, "extract_audio_track", _fake_extract)
    monkeypatch.setattr(media_mod, "extract_audio_track", _fake_extract)

    # 假 yt-dlp 下载
    import videotext_core.downloader as dl_mod

    def fake_download(url, outtmpl):
        out = Path(outtmpl.replace("%(ext)s", "mp4"))
        out.write_bytes(b"V" * 3000)
        return {"title": "测试视频标题", "duration": 250.0, "timestamp": 1704038400}

    monkeypatch.setattr(dl_mod, "download_with_ytdlp", fake_download)
    monkeypatch.setattr(cli, "download_with_ytdlp", fake_download)
    monkeypatch.setattr(cli, "probe_media_duration_seconds", lambda p: 250.0)
    monkeypatch.setattr(dl_mod, "find_downloaded_video", lambda root, stem: root / f"{stem}.mp4")
    monkeypatch.setattr(cli, "extract_info_metadata_only", lambda url: {"title": "测试视频标题", "duration": 250.0, "timestamp": 1704038400})
    monkeypatch.setattr(cli, "effective_download_url", lambda url: url)
    # 字幕轨捷径默认不命中（专门测试时单独覆盖）
    monkeypatch.setattr(dl_mod, "try_platform_subtitles", lambda url, d, stem: None)
    monkeypatch.setattr(cli, "try_platform_subtitles", lambda url, d, stem: None)


def _run_cli(argv):
    sys_argv = sys.argv
    sys.argv = argv
    try:
        return cli.main()
    finally:
        sys.argv = sys_argv


def test_paraformer_pipeline_full(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runs"
    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVtest1",
        "--output-dir", str(out_root),
        "--max-workers", "1",
    ])
    assert code == 0
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert m["status"] == "completed"
    task = m["tasks"][0]
    assert task["status"] == "completed"
    assert task["asr_source"] == "paraformer"
    assert task["duration_seconds"] == 250.0
    assert task["display_title"] == "测试视频标题（2023-12-31）"
    assert "summary_path" not in task

    # 调用链：getPolicy → OSS 上传 → 创建任务 → 轮询×3 → 结果下载
    kinds = [c[0] for c in FakeParaformerHTTP.calls]
    urls = [c[1] for c in FakeParaformerHTTP.calls]
    assert any("getPolicy" in str(c[2]) for c in FakeParaformerHTTP.calls if c[0] == "GET")
    assert any(u.endswith("/services/audio/asr/transcription") for u in urls)
    assert any(u == "https://result.example/t-1.json" for u in urls)

    task_dir = Path(task["output_dir"])
    srt = next(task_dir.glob("*.srt"))
    srt_text = srt.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:01,500\n你好" in srt_text
    assert "2\n00:00:02,000 --> 00:00:03,600\n世界第二句" in srt_text
    txt = next(task_dir.glob("*.txt"))
    assert txt.read_text(encoding="utf-8").strip() == "你好世界第二句"
    assert (task_dir / "audio.16k.mp3").is_file()


def test_platform_subtitle_shortcut_skips_download_and_asr(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runs2"

    srt_content = "1\n00:00:00,000 --> 00:00:02,000\n平台字幕内容\n"
    fake_srt = tmp_path / "fake.srt"
    fake_srt.write_text(srt_content, encoding="utf-8")

    import videotext_core.downloader as dl_mod
    monkeypatch.setattr(dl_mod, "try_platform_subtitles", lambda url, d, stem: fake_srt)
    monkeypatch.setattr(cli, "try_platform_subtitles", lambda url, d, stem: fake_srt)

    def _no_download(url, outtmpl):
        raise AssertionError("字幕轨命中时不应下载视频")

    monkeypatch.setattr(cli, "download_with_ytdlp", _no_download)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVs",
        "--output-dir", str(out_root),
        "--max-workers", "1",
    ])
    assert code == 0
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = m["tasks"][0]
    assert task["status"] == "completed"
    assert task["asr_source"] == "platform_subtitle"
    assert task["duration_seconds"] is None
    # 产物 txt 从平台字幕转出
    txt = Path(task["transcript_path"])
    assert "平台字幕内容" in txt.read_text(encoding="utf-8")
    # 无 HTTP 调用（未触发 paraformer）
    assert FakeParaformerHTTP.calls == []


def test_over_length_task_fails_with_guidance(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runs3"

    # dl_info 的 duration 优先生效，需在 fake_download 里注入超长时长
    def fake_long_download(url, outtmpl):
        out = Path(outtmpl.replace("%(ext)s", "mp4"))
        out.write_bytes(b"V" * 3000)
        return {"title": "超长视频", "duration": 8000.0, "timestamp": 1704038400}

    monkeypatch.setattr(cli, "download_with_ytdlp", fake_long_download)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVlong",
        "--output-dir", str(out_root),
        "--max-workers", "1",
    ])
    assert code == 1
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = m["tasks"][0]
    assert task["status"] == "failed"
    assert task["error_code"] == "OVER_LENGTH"
    assert "分段" in task["error_detail"]
    # 未触发任何 paraformer 调用
    assert FakeParaformerHTTP.calls == []


def test_no_api_key_fails_with_three_options(monkeypatch, tmp_path, capsys):
    patch_all(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "dashscope_api_key", lambda: None)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVx",
        "--output-dir", str(tmp_path / "runs4"),
        "--max-workers", "1",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "DASHSCOPE_API_KEY" in err
    assert "字幕轨" in err  # 三选一提示


def test_asr_error_classification(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)

    from videotext_core.asr_paraformer import ASRError

    def boom(url, outtmpl):
        raise ASRError("提交转写任务失败：Arrearage - account overdue")

    monkeypatch.setattr(cli, "download_with_ytdlp", boom)

    out_root = tmp_path / "runs5"
    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVerr",
        "--output-dir", str(out_root),
        "--max-workers", "1",
    ])
    assert code == 1
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = m["tasks"][0]
    assert task["error_code"] == "ASR_ARREARAGE"


def test_dead_link_partial_failed(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runs6"

    real_download = cli.download_with_ytdlp

    def selective_download(url, outtmpl):
        if "dead" in url:
            raise RuntimeError("HTTP Error 404")
        return real_download(url, outtmpl)

    monkeypatch.setattr(cli, "download_with_ytdlp", selective_download)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVgood",
        "--url", "https://www.bilibili.com/video/BVdead",
        "--output-dir", str(out_root),
        "--max-workers", "1",
    ])
    assert code == 1
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["status"] == "partial_failed"
    task_ok, task_bad = m["tasks"]
    assert task_ok["status"] == "completed"
    assert task_bad["status"] == "failed"
    assert task_bad["error_code"] == "DOWNLOAD_FAILED"


def test_status_command_with_pref(monkeypatch, tmp_path, capsys):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runs7"
    _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVs",
        "--output-dir", str(out_root),
        "--remember",
        "--max-workers", "1",
    ])
    run_dir = next(out_root.iterdir())
    capsys.readouterr()
    code = _run_cli(["video_batch_cli.py", "status", run_dir.name])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "completed"


# --- whisper 引擎 ---

def test_whisper_engine_full(monkeypatch, tmp_path):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runsW"

    # 无需 DashScope key（本地引擎免额度）
    monkeypatch.setattr(config, "dashscope_api_key", lambda: None)

    # mock faster_whisper 模块（lazy import 发生在 videotext_core.asr_whisper 命名空间）
    import videotext_core.asr_whisper as aw
    import types

    class FakeSegment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **k):
            segs = iter([FakeSegment(0.0, 1.5, " 本地转写"), FakeSegment(2.0, 3.6, " 第二句")])

            class Info:
                duration = 4.0

            return segs, Info()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVw",
        "--output-dir", str(out_root),
        "--asr-engine", "whisper",
        "--max-workers", "1",
    ])
    assert code == 0
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = m["tasks"][0]
    assert task["status"] == "completed"
    assert task["asr_source"] == "whisper"
    task_dir = Path(task["output_dir"])
    srt = next(task_dir.glob("*.srt"))
    assert "00:00:00,000 --> 00:00:01,500\n本地转写" in srt.read_text(encoding="utf-8")
    txt = next(task_dir.glob("*.txt"))
    assert txt.read_text(encoding="utf-8").strip() == "本地转写第二句"
    # whisper 引擎不触发任何云端调用
    assert FakeParaformerHTTP.calls == []


def test_whisper_engine_not_installed_fails_with_guidance(monkeypatch, tmp_path, capsys):
    patch_all(monkeypatch, tmp_path)
    out_root = tmp_path / "runsWI"
    monkeypatch.setattr(config, "dashscope_api_key", lambda: None)

    # 保证 faster_whisper 不可 import
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    code = _run_cli([
        "video_batch_cli.py", "run",
        "--url", "https://www.bilibili.com/video/BVw2",
        "--output-dir", str(out_root),
        "--asr-engine", "whisper",
        "--max-workers", "1",
    ])
    assert code == 1
    run_dir = next(out_root.iterdir())
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = m["tasks"][0]
    assert task["status"] == "failed"
    assert task["error_code"] == "WHISPER_NOT_INSTALLED"
    assert "--with-whisper" in task["error_detail"]
