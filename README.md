# video-to-summary

Agent skill：将视频 URL（B站 / 小红书 / YouTube / 通用站点）或**本地音频/视频文件**（录音、会议记录、视频音轨）批量转写为逐字稿——
真时间轴 SRT 字幕 + 纯文本 TXT。三种 ASR 来源自动择优或手动指定：

1. **平台字幕轨捷径**：B站 AI 字幕 / YouTube 自动字幕等现成字幕直接取用，零成本零等待（仅 URL 输入）
2. **云端 DashScope paraformer-v2**：录音文件识别异步 API，单文件 ≤2 小时/2GB，句级时间戳，约 0.29 元/小时音频（新用户有免费额度）
3. **本地 whisper（可选）**：faster-whisper 本地转写，零 API 成本，离线可用

另含：`verify-key` 子命令（零成本验证 key 可用性）、百炼首次注册开通引导（`skill/references/bailian-onboarding.md`，agent 陪跑）。

**AI 总结由调用方 agent 的模型完成**——本 skill 只负责到逐字稿为止，不内置任何 LLM 调用。

## 安装

```bash
# 1. 克隆到 skills 目录（Claude Code 与 Codex 同构）
git clone https://github.com/<OWNER>/video-to-summary.git ~/.claude/skills/video-to-summary
# Codex 用户：git clone ... ~/.codex/skills/video-to-summary

# 2. 自举环境
cd ~/.claude/skills/video-to-summary && ./setup.sh
# 可选：附带本地 whisper 引擎
./setup.sh --with-whisper

# 3. 配置密钥
cp skill/.env.example skill/.env   # 然后填入 DASHSCOPE_API_KEY
```

## 配置

| 变量 | 必需性 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 云端引擎必需 | 阿里百炼 key（地域北京）；从未开通百炼可让 agent 按引导文档陪跑开通 |
| `LINK_PREVIEW_BILIBILI_COOKIE` | 可选 | B站登录 Cookie；412 风控与 AI 字幕轨需要 |
| `LINK_PREVIEW_XIAOHONGSHU_COOKIE` | 可选 | 小红书登录 Cookie；短链解析必需（否则 403） |
| `FFMPEG_PATH` | 可选 | ffmpeg 路径（默认从 PATH 查找） |
| `WHISPER_MODEL` | 可选 | 本地 whisper 模型（默认 small） |

## 使用

```bash
# 单条 / 多条（首次使用需 --output-dir + --remember 记住产物目录）
skill/scripts/video_batch_cli.py run --url <URL> [--url ...] \
  --output-dir ./video-text-runs --remember --max-workers 3

# 指定 ASR 引擎
skill/scripts/video_batch_cli.py run --url <URL> --asr-engine whisper   # 本地，零成本
skill/scripts/video_batch_cli.py run --url <URL> --asr-engine dashscope # 云端

# 从文件批量（URL）
skill/scripts/video_batch_cli.py run --urls-file urls.txt

# 本地音频/视频文件转写（录音、会议等；可与 --url 混填）
skill/scripts/video_batch_cli.py run --file ~/recordings/meeting.m4a \
  --output-dir ./video-text-runs --remember

# 验证 key 可用性（零额度，不提交转写任务）
skill/scripts/video_batch_cli.py verify-key

# 查询（输出一行 JSON；省略 --output-dir 时读已记住的偏好）
skill/scripts/video_batch_cli.py status <run_id>
```

产物：`<output-dir>/<run_id>/tasks/NNN/` 下有 `*.mp4`、`audio.16k.mp3`、`*.srt`、`*.txt`；
`<run_id>/manifest.json` 记录每条任务状态与 ASR 来源（`platform_subtitle` / `paraformer` / `whisper`）。
退出码：`0` 全成功，`1` 至少一条失败，`2` 参数/环境错误。

## 错误码

| error_code | 含义 |
|---|---|
| ASR_ARREARAGE / ASR_AUTH / ASR_QUOTA | 云端欠费 / key 无效 / 限流 |
| ASR_MODEL_NOT_ENABLED | 模型未在百炼控制台开通（逐模型开通，见引导文档） |
| WHISPER_NOT_INSTALLED | 本地引擎未安装（见安装节 --with-whisper） |
| OVER_LENGTH | 音频超云端单文件上限 120 分钟（whisper 引擎无此限制） |
| AUDIO_EXTRACT_FAILED / DOWNLOAD_FAILED | ffmpeg 抽轨 / 下载失败（详见 manifest error_detail） |

## 本地开发

```bash
./setup.sh && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/   # 全部为 mock 测试，不消耗 API 额度
```

## License

[MIT](LICENSE)
