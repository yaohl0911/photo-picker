#!/usr/bin/env bash
# photo-picker 一键启动：起后端（FastAPI + 前端静态托管）并自动打开浏览器。
#
# 前提：.venv 已存在并装好 requirements.txt（见 README「快速开始」）：
#   uv venv && uv pip install -r requirements.txt
# 服务地址 http://127.0.0.1:8765；浏览器由 backend/app/main.py 的 __main__
# 在启动 1.5 秒后自动打开。停止服务：Ctrl-C。
set -euo pipefail

# 始终从脚本所在目录（项目根）运行，保证 data/ 与 frontend/dist 相对路径正确
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "错误：未找到 .venv/bin/python。请先在项目根目录创建虚拟环境并安装依赖：" >&2
  echo "  uv venv && uv pip install -r requirements.txt" >&2
  echo "（或等价：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt）" >&2
  exit 1
fi

exec .venv/bin/python -m backend.app.main
