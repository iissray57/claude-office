#!/usr/bin/env bash
# Claude Office 실행 (Linux / macOS / WSL) — Python 3.8+ 만 있으면 됩니다.
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
URL="http://localhost:${PORT:-8765}"
if command -v xdg-open >/dev/null 2>&1; then (sleep 1; xdg-open "$URL" >/dev/null 2>&1) &
elif command -v open >/dev/null 2>&1; then (sleep 1; open "$URL") &
fi
exec "$PY" server.py "$@"
