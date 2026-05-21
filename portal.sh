#!/usr/bin/env bash
# portal.sh — yt-learn Portal 起動スクリプト
# Mac から実行: WSL でサーバー起動 + SSH トンネル + Mac ブラウザを開く
# WSL から実行: サーバーをローカルで起動 + Windows ブラウザを開く

set -euo pipefail

PORT=8080
PROJECT="yt-learn"
TMUX_SESSION="yt-portal"

# ──────────────────────────────────────────
# Mac モード
# ──────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  echo "[portal] Mac モード — WSL でサーバーを起動してトンネルを張ります"

  # WSL 側でサーバーを tmux セッションで起動
  echo "[portal] WSL: tmux セッション '$TMUX_SESSION' を起動中…"
  ssh win "wsl -- bash -c '
    cd ~/my-projects/${PROJECT}
    tmux kill-session -t ${TMUX_SESSION} 2>/dev/null || true
    tmux new-session -d -s ${TMUX_SESSION} \"zsh -ic \\\"uvicorn portal.main:app --host 0.0.0.0 --port ${PORT}\\\"\"
  '"

  # サーバーが起動するまで少し待つ
  echo "[portal] サーバー起動待機中…"
  sleep 3

  # SSH トンネルをバックグラウンドで張る
  # 既存トンネルがあれば先に kill する
  existing=$(pgrep -f "ssh -L ${PORT}:localhost:${PORT} win" || true)
  if [[ -n "$existing" ]]; then
    kill "$existing" 2>/dev/null || true
    echo "[portal] 既存トンネルを終了しました"
  fi

  echo "[portal] SSH トンネル (Mac:${PORT} → Windows → WSL:${PORT}) を起動中…"
  ssh -L "${PORT}:localhost:${PORT}" win -N -f

  echo "[portal] ブラウザを開きます: http://localhost:${PORT}"
  open "http://localhost:${PORT}"

  echo "[portal] 完了。トンネルはバックグラウンドで動いています。"
  echo "[portal] 停止するには: pkill -f 'ssh -L ${PORT}:localhost:${PORT} win'"
  echo "[portal] WSL サーバー停止: ssh win \"wsl -- bash -c 'tmux kill-session -t ${TMUX_SESSION}'\""

# ──────────────────────────────────────────
# WSL モード
# ──────────────────────────────────────────
elif grep -qi microsoft /proc/version 2>/dev/null; then
  echo "[portal] WSL モード — ローカルでサーバーを起動します"

  cd "$(dirname "$(realpath "$0")")"

  # tmux セッションで起動する場合のラッパーとして使われることもある
  # 直接 uvicorn を起動
  echo "[portal] uvicorn を 0.0.0.0:${PORT} で起動します"

  # Windows ブラウザを非同期で開く（起動後3秒後）
  (sleep 3 && cmd.exe /c start "http://localhost:${PORT}" 2>/dev/null) &

  exec uvicorn portal.main:app --host 0.0.0.0 --port "${PORT}" --reload

# ──────────────────────────────────────────
# その他 Linux
# ──────────────────────────────────────────
else
  echo "[portal] Linux モード"
  cd "$(dirname "$(realpath "$0")")"
  exec uvicorn portal.main:app --host 0.0.0.0 --port "${PORT}" --reload
fi
