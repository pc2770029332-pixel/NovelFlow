#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  NovelFlow 一键启动（macOS / Linux）"
echo "============================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 没有检测到 python3，请先安装 Python 3.9 或更高版本。"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "第一次运行，正在创建虚拟环境..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "正在安装依赖（第一次会慢一点，请耐心等待）..."
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo
echo "启动成功！请用浏览器打开： http://127.0.0.1:8021"
echo "注意：本窗口不要关闭，关闭就是停止程序。"
echo
python run.py
