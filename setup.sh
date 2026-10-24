#!/usr/bin/env bash
# 一键自举：创建 .venv 并安装依赖（在仓库根目录执行）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ "${1:-}" = "--with-whisper" ]; then
    .venv/bin/pip install --quiet faster-whisper
    echo "OK: 本地 whisper 引擎已安装（首次运行会下载模型，国内建议 export HF_ENDPOINT=https://hf-mirror.com）"
fi

echo "OK: .venv ready → $(pwd)/.venv"
