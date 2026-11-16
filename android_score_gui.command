#!/bin/sh
set -eu
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    exec ".venv/bin/python" "android_score_gui.py"
fi

exec python3 "android_score_gui.py"
