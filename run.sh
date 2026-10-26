#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
    command -v uv >/dev/null 2>&1 || { echo '请先安装 uv'; exit 1; }
    uv venv --python 3.12 venv
fi
if ! venv/bin/python -c 'import requests, fake_useragent, matplotlib, PIL' >/dev/null 2>&1; then
    command -v uv >/dev/null 2>&1 || { echo '项目依赖不完整，请先安装 uv'; exit 1; }
    uv pip install --python venv/bin/python -r requirements.txt
fi
exec venv/bin/python main.py "$@"
