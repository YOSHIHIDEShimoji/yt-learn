#!/bin/bash
cd /Users/yoshihide/my-projects/yt-learn
mkdir -p log

LOG=log/run.log
PYTHON=/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

if ! nc -zw3 youtube.com 443 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ネットワーク未接続のためスキップ" >> "$LOG"
    exit 0
fi

output=$("$PYTHON" yt_learn.py all 2>&1)
exit_code=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] run exit=$exit_code" >> "$LOG"
echo "$output" >> "$LOG"
