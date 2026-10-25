#!/usr/bin/env bash
# v2i 启动器（Linux / macOS）
# 不带参数 = 起 Web GUI；带参数 = 当 CLI 用：./v2i.sh run "https://..." --mode all
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT/vendor:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[v2i] 找不到 $PY，请先安装 Python 3.10+（或用 PYTHON=/path/to/python3 ./v2i.sh）" >&2
  exit 2
fi

if [[ $# -eq 0 ]]; then
  exec "$PY" -m v2i web
fi
exec "$PY" -m v2i "$@"
