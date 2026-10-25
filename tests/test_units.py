"""单元测试：URL 识别 / 文件名安全化 / SRT 时间戳 / manifest 原子写。"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from videotext_core import manifest
from videotext_core.filename_safe import sanitize_filename
from videotext_core.metadata import (
    detect_platform,
    display_title_with_publish_date,
    normalize_inputs,
    normalize_urls,
    preferred_title,
    strip_topic_hashtags,
    upload_date_prefix,
)
from videotext_core.subtitles import (
    ms_to_srt_time,
    read_subtitle_plain_text,
    srt_to_plain,
    write_srt_with_timestamps,
    write_txt,
)


# --- detect_platform ---

@pytest.mark.parametrize(
    "url,expect",
    [
        ("https://www.bilibili.com/video/BV1xxx", "bilibili"),
        ("https://b23.tv/abc123", "bilibili"),
        ("https://www.xiaohongshu.com/explore/abc", "xiaohongshu"),
        ("https://xhslink.com/xyz", "xiaohongshu"),
        ("https://example.com/video", "unknown"),
    ],
)
def test_detect_platform(url, expect):
    assert detect_platform(url) == expect


# --- normalize_urls ---

def test_normalize_urls_strips_and_filters():
    urls, err = normalize_urls(["  https://a.com/1 ", "", "https://b.com/2"])
    assert err is None
    assert urls == ["https://a.com/1", "https://b.com/2"]


def test_normalize_urls_rejects_illegal():
    urls, err = normalize_urls(["ftp://a.com/1"])
    assert urls == []
    assert err == "illegal_url"


def test_normalize_urls_rejects_empty():
    _, err = normalize_urls(["", "   "])
    assert err == "empty_urls"


# --- normalize_inputs（URL + 本地文件混填）---

def test_normalize_inputs_url_and_file(tmp_path):
    f = tmp_path / "rec.mp3"
    f.write_bytes(b"x")
    specs, err = normalize_inputs(["  https://a.com/1 ", str(f)])
    assert err is None
    assert specs[0] == {"kind": "url", "value": "https://a.com/1"}
    assert specs[1]["kind"] == "file"
    assert specs[1]["value"] == str(f.resolve())


def test_normalize_inputs_missing_file():
    specs, err = normalize_inputs(["/nonexistent/path/rec.mp3"])
    assert specs == []
    assert err is not None and err.startswith("file_not_found")


def test_normalize_inputs_rejects_directory(tmp_path):
    _, err = normalize_inputs([str(tmp_path)])
    assert err is not None and err.startswith("not_a_file")


def test_normalize_inputs_rejects_other_scheme():
    _, err = normalize_inputs(["ftp://a.com/1"])
    assert err == "illegal_url"


def test_normalize_inputs_empty():
    _, err = normalize_inputs(["", "   "])
    assert err == "empty_inputs"


# --- sanitize_filename ---

@pytest.mark.parametrize(
    "raw,expect",
    [
        ("a/b\\c:d*e?f\"g<h>i|j", "a_b_c_d_e_f_g_h_i_j"),
        ("正常标题（2024-01-01）", "正常标题（2024-01-01）"),
        ("", "untitled"),
    ],
)
def test_sanitize_filename(raw, expect):
    assert sanitize_filename(raw) == expect


# --- ms_to_srt_time ---

@pytest.mark.parametrize(
    "ms,expect",
    [
        (0, "00:00:00,000"),
        (1500, "00:00:01,500"),
        (61_023, "00:01:01,023"),
        (3_600_000, "01:00:00,000"),
        (3_723_456_789, "1034:17:36,789"),
        (-5, "00:00:00,000"),
        (999.6, "00:00:01,000"),
    ],
)
def test_ms_to_srt_time(ms, expect):
    assert ms_to_srt_time(ms) == expect


# --- SRT 生成 / 转换 ---

SENTENCES = [
    {"begin_ms": 0, "end_ms": 1500, "text": "你好"},
    {"begin_ms": 2000, "end_ms": 3600, "text": "第二句"},
    {"begin_ms": 4000, "end_ms": 5000, "text": ""},
]


def test_write_srt_with_timestamps(tmp_path):
    p = tmp_path / "a.srt"
    write_srt_with_timestamps(p, SENTENCES)
    content = p.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:01,500\n你好\n" in content
    assert "2\n00:00:02,000 --> 00:00:03,600\n第二句\n" in content
    # 空文本句被跳过，序号不占用
    assert content.count("-->") == 2


def test_write_srt_empty_sentences(tmp_path):
    p = tmp_path / "b.srt"
    write_srt_with_timestamps(p, [])
    assert p.read_text(encoding="utf-8") == ""


def test_write_txt_appends_newline(tmp_path):
    p = tmp_path / "c.txt"
    write_txt(p, "正文")
    assert p.read_text(encoding="utf-8") == "正文\n"


def test_srt_to_plain_strips_timeline():
    srt = "1\n00:00:01,000 --> 00:00:03,000\n你好世界\n\n2\n00:00:03,500 --> 00:00:06,000\n第二行\n"
    assert srt_to_plain(srt) == "你好世界\n第二行"


def test_srt_to_plain_handles_crlf():
    srt = "1\n00:00:01,000 --> 00:00:03,000\n你好\n"
    assert srt_to_plain(srt.replace("\n", "\r\n")) == "你好"


def test_read_subtitle_plain_text_txt(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("纯文本内容\n", encoding="utf-8")
    assert read_subtitle_plain_text(p) == "纯文本内容"


def test_read_subtitle_plain_text_missing(tmp_path):
    assert read_subtitle_plain_text(tmp_path / "nope.srt") is None


# --- metadata helpers ---

def test_preferred_title_fallbacks():
    assert preferred_title({"title": "  标题  "}) == "标题"
    assert preferred_title({"fulltitle": "备用"}) == "备用"
    assert preferred_title(None) is None


def test_strip_topic_hashtags():
    assert strip_topic_hashtags("今天#美食[话题]#探店#日常#") == "今天探店"


def test_upload_date_prefix_from_yyyymmdd():
    assert upload_date_prefix({"upload_date": "20240102"}) == "2024-01-02"


def test_upload_date_prefix_from_timestamp():
    # UTC 时区固定，避免本机时区影响断言
    assert upload_date_prefix({"timestamp": 1704038400}) in ("2024-01-01", "2023-12-31")


def test_display_title_with_publish_date():
    assert display_title_with_publish_date("标题", "2024-01-01") == "标题（2024-01-01）"
    assert display_title_with_publish_date("标题", None) == "标题"


# --- manifest ---

class _FakeStaticResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeStaticClient:
    """所有 GET 返回固定 status；记录收到的 params。"""
    status_code = 200
    seen_params: list = []

    def __init__(self, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        type(self).seen_params.append(params)
        return _FakeStaticResponse(type(self).status_code, "body")


def test_verify_key_ok_covers_both_models(monkeypatch):
    import videotext_core.asr_verify as av

    _FakeStaticClient.status_code = 200
    _FakeStaticClient.seen_params = []
    monkeypatch.setattr(av.httpx, "Client", _FakeStaticClient)
    res = av.verify_key("sk-test")
    assert res["status"] == "ok"
    assert set(res["checks"]) == {"paraformer-v2", "paraformer-realtime-v2"}
    assert all(c["status"] == "ok" for c in res["checks"].values())
    models = {p.get("model") for p in _FakeStaticClient.seen_params}
    assert models == {"paraformer-v2", "paraformer-realtime-v2"}


def test_verify_key_invalid_on_401(monkeypatch):
    import videotext_core.asr_verify as av

    _FakeStaticClient.status_code = 401
    _FakeStaticClient.seen_params = []
    monkeypatch.setattr(av.httpx, "Client", _FakeStaticClient)
    res = av.verify_key("sk-bad")
    assert res["status"] == "invalid"
    assert all(c["status"] == "invalid" for c in res["checks"].values())


def test_verify_key_error_on_500(monkeypatch):
    import videotext_core.asr_verify as av

    _FakeStaticClient.status_code = 500
    _FakeStaticClient.seen_params = []
    monkeypatch.setattr(av.httpx, "Client", _FakeStaticClient)
    res = av.verify_key("sk-x")
    assert res["status"] == "error"

def test_manifest_create_write_load(tmp_path):
    m = manifest.create_manifest(tmp_path, "20260101-000000-abcd", ["https://a.com/1", "https://b.com/2"])
    loaded = manifest.load_manifest(tmp_path)
    assert loaded == m
    assert loaded["total"] == 2
    t = loaded["tasks"][0]
    assert t["status"] == "pending"
    assert t["asr_source"] is None
    assert t["transcript_path"] is None
    assert "summary_path" not in t


def test_manifest_finalize_aggregates(tmp_path):
    m = manifest.create_manifest(tmp_path, "r", ["https://a.com/1", "https://b.com/2", "https://c.com/3"])
    m["tasks"][0]["status"] = "completed"
    m["tasks"][1]["status"] = "failed"
    m["tasks"][1]["error_code"] = "ASR_FAILED"
    out = manifest.finalize_manifest(tmp_path, m)
    assert out["status"] == "partial_failed"
    assert out["completed"] == 1
    assert out["failed"] == 1


def test_manifest_status_summary_failed_urls(tmp_path):
    m = manifest.create_manifest(tmp_path, "r", ["https://a.com/1"])
    m["tasks"][0]["status"] = "failed"
    m["tasks"][0]["error_code"] = "DOWNLOAD_FAILED"
    m["tasks"][0]["error_detail"] = "boom"
    s = manifest.status_summary(m)
    assert s["failed_urls"] == [{"url": "https://a.com/1", "error_code": "DOWNLOAD_FAILED", "error_detail": "boom"}]


def test_manifest_load_corrupt_returns_none(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert manifest.load_manifest(tmp_path) is None


# --- verify_key（getPolicy 鉴权，零额度）---


def test_manifest_spec_inputs_record_input_kind(tmp_path):
    """spec dict 输入：manifest 记 input_kind 与 source_path。"""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    specs = [
        {"kind": "url", "value": "https://a.com/1"},
        {"kind": "file", "value": str(f)},
    ]
    m = manifest.create_manifest(tmp_path, "r", specs)
    tu, tf = m["tasks"]
    assert tu["input_kind"] == "url" and tu["source_path"] is None
    assert tf["input_kind"] == "file"
    assert tf["source_path"] == str(f)
    assert tf["url"] == str(f)
