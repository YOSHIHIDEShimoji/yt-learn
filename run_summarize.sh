#!/bin/bash
cd /Users/yoshihide/my-projects/yt-learn
mkdir -p log

LOG=log/summarize.log
PYTHON=/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
NOTIFY=~/Applications/Notifiers/yt-learn.app/Contents/MacOS/yt-learn

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# LOCAL_LLM_URL が localhost を指している場合、SSHトンネルを起動
TUNNEL_PID=""
if [[ "${LOCAL_LLM_URL}" == http://localhost:* ]]; then
    PORT="${LOCAL_LLM_URL#http://localhost:}"
    PORT="${PORT%%/*}"
    if ! nc -z localhost "$PORT" 2>/dev/null; then
        ssh -N -L "${PORT}:localhost:${PORT}" win &
        TUNNEL_PID=$!
        sleep 1
    fi
fi
trap '[ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null' EXIT

if ! nc -zw3 google.com 443 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ネットワーク未接続のためスキップ" >> "$LOG"
    exit 0
fi

output=$("$PYTHON" summarize.py all --threshold 20 2>&1)
exit_code=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] run exit=$exit_code" >> "$LOG"
echo "$output" >> "$LOG"

if [ $exit_code -ne 0 ]; then
    "$NOTIFY" -title "yt-learn" \
        -message "  要約でエラーが発生しました。log/summarize.log を確認してください"
fi

sync_output=$("$PYTHON" transcribe.py sync --only summaries 2>&1)
sync_exit=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync exit=$sync_exit" >> "$LOG"
echo "$sync_output" >> "$LOG"
