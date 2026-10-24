---
name: video-to-summary
description: 将粘贴的视频 URL（B站/小红书/YouTube/通用站点）批量下载并转写为逐字稿（真时间轴 SRT + 纯文本 TXT）。三种 ASR 来源：平台字幕轨捷径（零成本）、云端 DashScope paraformer-v2（单文件最长 2 小时）、本地 whisper（--asr-engine whisper，免额度离线）。总结由调用方 agent 的模型完成，本 skill 不内置任何 LLM 调用。触发词：视频转文字、逐字稿、字幕、批量转写、B站视频总结、小红书视频转写。
version: 0.4.0
---

# video-to-summary

将视频 URL 转写为逐字稿。三种 ASR 来源：平台字幕轨捷径（优先零成本）、云端 DashScope paraformer-v2（异步任务 API，句级时间戳）、本地 whisper（可选引擎，免额度）。**总结由你（调用方 agent）的模型完成**——skill 只负责到逐字稿为止。

## 前置检查（每次会话首次使用时）

skill 根目录 = 本 SKILL.md 所在目录。

1. 若 `../.venv` 不存在（即 skill 的上一级目录下没有 `.venv`），执行：`bash <skill上级目录>/setup.sh`
2. 确认 `ffmpeg` 可用：`which ffmpeg`（不可用时提示用户安装，或在 skill/.env 设 FFMPEG_PATH）
3. 确认 `skill/.env` 含 `DASHSCOPE_API_KEY`。**若缺失或调用报欠费（Arrearage/Access denied），向用户提供三选一，不要静默降级**：
   - ① 配置/充值 DashScope key（paraformer-v2 约 0.29 元/小时音频，新用户有免费额度）
   - ② 若视频平台有现成字幕轨（YouTube 自动字幕、B站 AI 字幕），走字幕轨捷径零成本完成
   - ③ 本地 whisper 引擎免额度离线转写：`--asr-engine whisper`（首次需 `./setup.sh --with-whisper` 安装）

## 输出目录解析（每次 run 前）

CLI 不会自行猜目录。解析顺序：

1. 查偏好：`cat <skill>/preferences.json`，若含 `default_output_dir` → 直接调用，不带 `--output-dir`
2. 无偏好文件 → **必须先询问用户**希望把产物存放到哪里（用 AskUserQuestion 或对话均可，给 cwd/video-text-runs 与自定义两个选项），拿到路径后调用时带 `--output-dir <用户路径> --remember`，CLI 会把该路径存为默认
3. 用户某次明示想换目录 → 带 `--output-dir <新路径> --remember` 覆盖

**禁止**在用户未表态时自行挑选目录跳过询问——首次报错（退出码 2）是设计行为，提示里已含补救指令。

## 两段式流程

### 第 1 段：CLI 产逐字稿

```bash
# ≤5 条且非超长视频：前台直接跑
<skill>/scripts/video_batch_cli.py run --url <URL1> [--url N ...] \
  [--output-dir <路径> --remember] [--asr-engine dashscope|whisper] --max-workers 3

# 更多条 / 长视频：Bash 后台运行 + 轮询
#   1) 后台: run ... （记下打印的 run_id）
#   2) 轮询: video_batch_cli.py status <run_id> [--output-dir <路径>]
#      输出一行 JSON {status, completed, failed, total, failed_urls}
#      status 为 running 时隔 30-60s 再查；终态为 completed / partial_failed / failed
```

每条任务自动依次：元数据 → 字幕轨捷径（命中即零成本完成，manifest 记 `asr_source: platform_subtitle`）→ 下载 → 抽 16kHz 音轨 → ASR 引擎转写（默认 `dashscope`，`--asr-engine whisper` 走本地）→ `*.srt`（真时间轴）+ `*.txt`（纯文本）双产物（`asr_source: paraformer` 或 `whisper`）。

### 第 2 段：agent 总结（你来做）

**总结前先问一句**：需要带时间戳的「时间线」小节吗？（长视频 >30 分钟时主动推荐选"要"——时间锚点对回看定位很实用；短视频默认建议不要）。用户答复决定读取策略：

- **不要时间线（默认）**：读 `*.txt` 逐字稿——干净省 token，产出概要/要点/关键词
- **要时间线**：读 `*.srt`——按时间轴抽锚点，时间线小节格式 `[MM:SS] 该时段要点`；正文理解仍可参考 txt，不必整篇重读

然后写 `summary.md` 到任务目录：文首加「视频信息」区块（标题用 manifest 的 display_title、发布时间）+ 结构化 Markdown（概要/要点/关键词；时间线仅用户要求时含）。

**对话里只回最简短的结构化摘要**（概要一两句 + 要点三五个），附产物路径；绝不把逐字稿或总结全文粘进对话。

## 硬性规则

1. **绝不把逐字稿或总结全文粘进对话**。只报告：产物路径 + 每条 ≤3 行预览。
2. **失败不中断**：batch 中某条失败时其余继续。最终必须给出成功/失败清单（失败条附 error_code 与原因）。
3. **不泄露 key**：不读取、不回显 .env 内容。
4. 退出码：0 全成功；1 有失败（partial_failed/failed）；2 参数或环境错误。
5. 字幕轨命中时**明示用户**「使用了平台现成字幕，非 ASR 转写」。

## 产物结构

```
<output-dir>/<run_id>/
├── manifest.json          # 状态清单（snake_case，含 asr_source / transcript_path / error_code）
└── tasks/NNN/
    ├── <标题>（YYYY-MM-DD）.mp4
    ├── audio.16k.mp3      # 送 paraformer 的 16kHz 音轨
    ├── <标题>.srt         # 真时间轴逐字稿字幕
    ├── <标题>.txt         # 纯文本逐字稿（总结的输入）
    └── summary.md         # AI 总结（agent 第 2 段写入）
```

## 错误码与处理

| error_code | 含义 | 给用户的建议 |
|---|---|---|
| ASR_ARREARAGE | DashScope 欠费 | 见前置检查三选一 |
| ASR_AUTH | key 无效 | 检查 skill/.env |
| ASR_QUOTA | 限流/429 | 稍后重试或降低并发 |
| WHISPER_NOT_INSTALLED | 本地引擎未安装 | `./setup.sh --with-whisper` 后重试 |
| OVER_LENGTH | 音频 >120 分钟（仅云端引擎） | 换 whisper 引擎或人工分段 |
| AUDIO_EXTRACT_FAILED | ffmpeg 抽轨失败 | 检查 ffmpeg 安装 |
| DOWNLOAD_FAILED | 下载失败 | 看 error_detail（常见：B站 412 需 Cookie） |

## 经验坑

- **时长边界**：云端引擎文档上限单文件 2 小时（实测验证前按 ≤2h 报告用户）；whisper 引擎无此限制。
- **whisper 首次运行**会下载模型（small 约 460MB）到用户缓存目录，国内网络建议先 `export HF_ENDPOINT=https://hf-mirror.com` 再跑。
- 小红书短链（xhslink.com）无 Cookie 必 403：提示用户在 skill/.env 配 `LINK_PREVIEW_XIAOHONGSHU_COOKIE`，或改粘贴 explore 长链。
- B 站 412 风控：配 `LINK_PREVIEW_BILIBILI_COOKIE`（浏览器整串 Cookie）。
- B站 AI 字幕需登录态 Cookie 才能拉到；YouTube 自动字幕无需登录。
- 同名文件自动 `(1)` 递增，不会覆盖旧产物。
- DashScope 报 `Arrearage` / "Access denied"：账号欠费，与 skill 本身无关。
