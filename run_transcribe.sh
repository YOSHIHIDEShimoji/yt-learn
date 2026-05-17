#!/bin/bash
cd /Users/yoshihide/my-projects/yt-learn
mkdir -p log

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

if ! nc -zw3 youtube.com 443 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ネットワーク未接続のためスキップ" >> "$LOG"
    "$NOTIFY" -title "yt-learn" -message "  ネットワーク未接続のためスキップしました"
    exit 0
fi

"$PYTHON" transcribe.py all --sort popular --limit 20
exit_code=$?

if [ $exit_code -ne 0 ]; then
    "$NOTIFY" -title "yt-learn" \
        -message "  文字起こしでエラーが発生しました。log/ を確認してください"
fi

"$PYTHON" transcribe.py sync --only transcripts
