#!/usr/bin/env bash
# WSL側専用: pyenv を自前初期化して uvicorn を起動する
# portal.sh (Mac) から tmux 経由で呼ばれる。zsh -ic 不要
set -euo pipefail

export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

cd "$(dirname "$(dirname "$(realpath "$0")")")"

# Kill any existing process on port 8080 before starting
OLD_PIDS=$(lsof -ti :8080 2>/dev/null || true)
if [[ -n "$OLD_PIDS" ]]; then
  echo "[portal-server] ポート 8080 の既存プロセスを終了します"
  echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
  sleep 1
fi

exec uvicorn portal.main:app --host 0.0.0.0 --port 8080
