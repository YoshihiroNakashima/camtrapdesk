#!/bin/bash
# macOS 用: ダブルクリックで起動。初回は .venv を作ってライブラリを取得する。
cd "$(dirname "$0")"
VENV=".venv"
LOG="camtrapdesk-setup.log"
if [ ! -x "$VENV/bin/python" ]; then
  echo "camtrapdesk 初回セットアップ（数分かかります）"
  PY=""
  for c in python3.12 python3.11 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.10 以上が見つかりません。https://www.python.org/downloads/ からインストールしてください。"
    read -r -p "Enter で閉じます"; exit 1
  fi
  "$PY" -m venv "$VENV" > "$LOG" 2>&1 || { echo "venv の作成に失敗"; cat "$LOG"; read -r -p "Enter で閉じます"; exit 1; }
  "$VENV/bin/python" -m pip install --upgrade pip >> "$LOG" 2>&1
  "$VENV/bin/python" -m pip install -e . >> "$LOG" 2>&1 || { echo "ライブラリの取得に失敗"; cat "$LOG"; read -r -p "Enter で閉じます"; exit 1; }
  echo "セットアップ完了"
fi
exec "$VENV/bin/python" -m camtrapdesk
