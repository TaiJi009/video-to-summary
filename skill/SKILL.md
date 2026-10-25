---
name: video-to-summary
description: 将视频 URL（B站/小红书/YouTube/通用站点）或本地音频/视频文件（录音、会议、视频音轨）批量转写为逐字稿（真时间轴 SRT + 纯文本 TXT，支持多语言字幕轨）。三种 ASR 来源：平台字幕轨捷径（零成本）、云端 DashScope paraformer-v2（单文件最长 2 小时）、本地 whisper（--asr-engine whisper，免额度离线）。含百炼首次注册开通引导（references/bailian-onboarding.md）与 verify-key 零成本验 key。总结由调用方 agent 的模型完成，本 skill 不内置任何 LLM 调用。触发词：视频转文字、逐字稿、字幕、多语言字幕、翻译字幕、批量转写、录音转写、本地文件转写、B站视频总结、小红书视频转写、YouTube 视频总结、开通百炼。
version: 0.6.0
---

# video-to-summary

将视频 URL 或本地音视频文件转写为逐字稿。三种 ASR 来源：平台字幕轨捷径（优先零成本）、云端 DashScope paraformer-v2（异步任务 API，句级时间戳）、本地 whisper（可选引擎，免额度）。**总结由你（调用方 agent）的模型完成**——skill 只负责到逐字稿为止。支持多语言字幕轨拉取（`--languages`）。

## 前置检查（每次会话首次使用时）

skill 根目录 = 本 SKILL.md 所在目录。

1. 若 `../.venv` 不存在（即 skill 的上一级目录下没有 `.venv`），执行：`bash <skill上级目录>/setup.sh`
2. 确认 `ffmpeg` 可用：`which ffmpeg`（不可用时提示用户安装，或在 skill/.env 设 FFMPEG_PATH）
3. 确认 `skill/.env` 含 `DASHSCOPE_API_KEY`。**若缺失或调用报欠费（Arrearage/Access denied），向用户提供以下选项，不要静默降级**：
   - ① 配置/充值 DashScope key——若用户**从未开通过百炼**，改走《[references/bailian-onboarding.md](references/bailian-onboarding.md)》开通引导（你陪跑：读官方文档、逐步指路，注册/验证/开通模型等关键步骤由人完成）
   - ② 若视频平台有现成字幕轨（YouTube 自动字幕、B站 AI 字幕），走字幕轨捷径零成本完成
   - ③ 本地 whisper 引擎免额度离线转写：`--asr-engine whisper`（首次需 `./setup.sh --with-whisper` 安装）
4. 不确定 key 是否有效时先跑 `video_batch_cli.py verify-key`（零额度，不提交转写任务），按输出决定引导方向。

## 输入场景澄清（run 前）

与产物目录、多语言询问**合并一次问完**（不在 CLI 运行中打断）：

1. **URL 批量**（用户粘贴视频网址）→ `run --url`，流程见下文。
2. **本地文件**（用户说"我有个录音/音频/视频文件"，给出路径）→ `run --file <路径>`（可多条、可与 URL 混填）。文件直接抽音轨转写，无下载步骤；**此场景不问多语言**（ASR 只识别原语，无翻译）。
3. **直播流 URL**（正在直播的房间链接）→ 明确告知暂不支持，建议先用 yt-dlp 把直播落地为文件后走 `run --file`。
4. **用户从未开通过百炼且想用云端引擎** → 走《references/bailian-onboarding.md》引导。

## 输出目录解析（每次 run 前）

CLI 不会自行猜目录。解析顺序：

1. 查偏好：`cat <skill>/preferences.json`，若含 `default_output_dir` → 直接调用，不带 `--output-dir`
2. 无偏好文件 → **必须先询问用户**希望把产物存放到哪里（用 AskUserQuestion 或对话均可，给 cwd/video-text-runs 与自定义两个选项），拿到路径后调用时带 `--output-dir <用户路径> --remember`，CLI 会把该路径存为默认
3. 用户某次明示想换目录 → 带 `--output-dir <新路径> --remember` 覆盖

**禁止**在用户未表态时自行挑选目录跳过询问——首次报错（退出码 2）是设计行为，提示里已含补救指令。

## 多语言询问（run 前）

与产物目录询问同一次问全（batch 场景一次问完，不在 CLI 运行中打断）：

1. **是否需要多语言字幕？** 三选一：
   - **只要主语言**（默认推荐）：不带 `--languages`，产物最干净
   - **全部语言轨**：`--languages all`
   - **指定几种**：`--languages zh,en,ja`（逗号分隔）
2. 用户说不清轨道情况时可直接说明：B站 AI 字幕通常有中文主轨 + 若干机翻轨；YouTube 几乎总是有 100+ 自动机翻轨（`all` 时 skill 自动降级为常用语集，不会全拉）。

**重要事实**：多语言译文**只有字幕轨路径能提供**。ASR 路径（paraformer / whisper）只识别音视频原语、不做翻译——凡走到 ASR 的任务（含所有本地文件任务），产物永远只有原语一份逐字稿，此时不用问多语言。

## 两段式流程

### 第 1 段：CLI 产逐字稿

```bash
# ≤5 条且非超长视频：前台直接跑
<skill>/scripts/video_batch_cli.py run --url <URL1> [--url N ...] \
  [--file <本地路径> ...] \
  [--output-dir <路径> --remember] [--asr-engine dashscope|whisper] --max-workers 3

# 更多条 / 长视频：Bash 后台运行 + 轮询
#   1) 后台: run ... （记下打印的 run_id）
#   2) 轮询: video_batch_cli.py status <run_id> [--output-dir <路径>]
#      输出一行 JSON {status, completed, failed, total, failed_urls}
#      status 为 running 时隔 30-60s 再查；终态为 completed / partial_failed / failed
```

URL 任务自动依次：元数据 → 字幕轨捷径（命中即零成本完成，manifest 记 `asr_source: platform_subtitle`）→ 下载 → 抽 16kHz 音轨 → ASR 引擎转写（默认 `dashscope`，`--asr-engine whisper` 走本地）→ `*.srt`（真时间轴）+ `*.txt`（纯文本）双产物。本地文件任务（`--file`）跳过元数据/字幕/下载，直接抽音轨 → ASR，manifest 记 `input_kind: "file"`、`platform: "local"`，标题取文件名。

### 第 2 段：agent 总结（你来做）

**总结前先问一次（两个问题合并问）**：
1. 需要带时间戳的「时间线」小节吗？（长视频 >30 分钟时主动推荐选"要"——时间锚点对回看定位很实用；短视频默认建议不要）。
2. 若 manifest 里 `subtitle_languages` 非空（多语言轨命中）：**总结读哪种语言？**（主轨 / 某附加轨 / 多轨对照）。仅单轨或 ASR 任务跳过此问。

用户答复决定读取策略：

- **不要时间线（默认）**：读 `*.txt` 逐字稿——干净省 token，产出概要/要点/关键词
- **要时间线**：读 `*.srt`——按时间轴抽锚点，时间线小节格式 `[MM:SS] 该时段要点`；正文理解仍可参考 txt，不必整篇重读
- **多语言轨**：附加轨文件为 `<标题>.<语言>.srt`（如 `.en.srt`）；语言清单看 manifest 的 `subtitle_languages`

然后写 `summary.md` 到任务目录：文首加「视频信息」区块（标题用 manifest 的 display_title、发布时间；本地文件任务写文件名与时长）+ 结构化 Markdown（概要/要点/关键词；时间线仅用户要求时含）。

**对话里只回最简短的结构化摘要**（概要一两句 + 要点三五个），附产物路径；绝不把逐字稿或总结全文粘进对话。

## 硬性规则

1. **绝不把逐字稿或总结全文粘进对话**。只报告：产物路径 + 每条 ≤3 行预览。
2. **失败不中断**：batch 中某条失败时其余继续。最终必须给出成功/失败清单（失败条附 error_code 与原因）。
3. **不泄露 key**：不读取、不回显 .env 内容。
4. 退出码：0 全成功；1 有失败（partial_failed/failed）；2 参数或环境错误。
5. 字幕轨命中时**明示用户**「使用了平台现成字幕，非 ASR 转写」。
6. **首次开通必须走引导文档**（references/bailian-onboarding.md）：先 WebFetch 官方文档取最新流程，不凭记忆给步骤；关键授权动作（注册、验证、开通模型、创建 key）由人完成，agent 只指路。

## 产物结构

```
<output-dir>/<run_id>/
├── manifest.json          # 状态清单（snake_case，含 input_kind / asr_source / transcript_path / subtitle_languages / error_code）
└── tasks/NNN/
    ├── <标题>（YYYY-MM-DD）.mp4   # URL 任务；本地文件任务无此项（源文件原地不动）
    ├── audio.16k.mp3      # 送 paraformer 的 16kHz 音轨
    ├── <标题>.srt         # 真时间轴逐字稿字幕（主轨）
    ├── <标题>.<语言>.srt  # 多语言附加轨（仅 --languages 命中时）
    ├── <标题>.txt         # 纯文本逐字稿（总结的输入，主轨）
    └── summary.md         # AI 总结（agent 第 2 段写入）
```

本地文件任务的 `<标题>` 为文件名 stem；manifest 的 `source_path` 记录源文件绝对路径。

## 错误码与处理

| error_code | 含义 | 给用户的建议 |
|---|---|---|
| ASR_ARREARAGE | DashScope 欠费 | 见前置检查选项 |
| ASR_AUTH | key 无效 | `verify-key` 诊断；检查 skill/.env |
| ASR_MODEL_NOT_ENABLED | 模型未开通 | 到百炼控制台逐模型开通 paraformer-v2（见引导文档第 5 步） |
| ASR_QUOTA | 限流/429 | 稍后重试或降低并发 |
| WHISPER_NOT_INSTALLED | 本地引擎未安装 | `./setup.sh --with-whisper` 后重试 |
| OVER_LENGTH | 音频 >120 分钟（仅云端引擎） | 换 whisper 引擎或人工分段 |
| AUDIO_EXTRACT_FAILED | ffmpeg 抽轨失败 | 检查 ffmpeg 安装；确认文件是可解码的音视频 |
| DOWNLOAD_FAILED | 下载失败 | 看 error_detail（常见：B站 412 需 Cookie） |

## 经验坑

- **时长边界**：云端引擎文档上限单文件 2 小时（实测验证前按 ≤2h 报告用户；本地文件同限）；whisper 引擎无此限制。
- **whisper 首次运行**会下载模型（small 约 460MB）到用户缓存目录，国内网络建议先 `export HF_ENDPOINT=https://hf-mirror.com` 再跑。
- 小红书短链（xhslink.com）无 Cookie 必 403：提示用户在 skill/.env 配 `LINK_PREVIEW_XIAOHONGSHU_COOKIE`，或改粘贴 explore 长链。
- B 站 412 风控：配 `LINK_PREVIEW_BILIBILI_COOKIE`（浏览器整串 Cookie）。
- B站 AI 字幕需登录态 Cookie 才能拉到；YouTube 无 Cookie 常被人机验证拦截（`Sign in to confirm you're not a bot`）——配 `YOUTUBE_COOKIE_BROWSER=chrome`（或 edge/firefox 等）借浏览器登录态。
- **机翻质量警告**：B站 AI 附加轨与 YouTube 自动字幕绝大多数是机器翻译，仅主语言轨（B站 ai-zh / YouTube 原语）接近原声质量。总结引用译文轨时向用户提示此局限。
- 同名文件自动 `(1)` 递增，不会覆盖旧产物。
- DashScope 报 `Arrearage` / "Access denied"：账号欠费，与 skill 本身无关。
- **verify-key 只验 key 不验模型开通**：getPolicy 鉴权通过≠paraformer-v2 已开通；模型是否开通以首次真实转写为准（报 Model.NotExists/未开通 → 控制台补开通）。

