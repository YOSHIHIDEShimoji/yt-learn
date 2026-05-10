#!/bin/bash
cd /Users/yoshihide/my-projects/yt-learn
mkdir -p log

LOG=log/run.log
PYTHON=/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
NOTIFY=~/Applications/Notifiers/yt-learn.app/Contents/MacOS/yt-learn

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

if ! nc -zw3 youtube.com 443 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ネットワーク未接続のためスキップ" >> "$LOG"
    "$NOTIFY" -title "yt-learn" -message "  ネットワーク未接続のためスキップしました"
    exit 0
fi

output=$("$PYTHON" yt_learn.py all --sort popular --limit 20 2>&1)
exit_code=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] run exit=$exit_code" >> "$LOG"
echo "$output" >> "$LOG"

if [ $exit_code -ne 0 ]; then
    "$NOTIFY" -title "yt-learn" \
        -message "  文字起こしでエラーが発生しました。log/run.log を確認してください"
fi
