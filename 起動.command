#!/bin/bash
# macOS 用: ダブルクリックで起動。初回は .venv を作ってライブラリを取得する。
cd "$(dirname "$0")"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 以上が見つかりません。https://www.python.org/downloads/ からインストールしてください。"
  open "https://www.python.org/downloads/"
  read -r -p "Enter キーで閉じます "
  exit 1
fi
exec "$PY" tools/launch.py
