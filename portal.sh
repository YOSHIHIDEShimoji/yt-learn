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
  # portal-server.sh が pyenv を自前初期化するので zsh -ic 不要
  echo "[portal] WSL: tmux セッション '$TMUX_SESSION' を起動中…"
  ssh win "wsl -- bash -c 'cd ~/my-projects/${PROJECT} && tmux kill-session -t ${TMUX_SESSION} 2>/dev/null; tmux new-session -d -s ${TMUX_SESSION} ./portal-server.sh'"

  # サーバーが起動するまで待機（最大 15 秒）
  echo "[portal] サーバー起動待機中…"
  for i in $(seq 1 15); do
    if ssh win "wsl -- bash -c 'curl -s http://localhost:${PORT}/ > /dev/null 2>&1'" 2>/dev/null; then
      echo "[portal] サーバー起動確認 (${i}秒)"
      break
    fi
    sleep 1
  done

  # SSH トンネルをバックグラウンドで張る（既存は先に kill）
  existing=$(pgrep -f "ssh -L ${PORT}:localhost:${PORT} win" 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    kill "$existing" 2>/dev/null || true
    echo "[portal] 既存トンネルを終了しました"
  fi

  echo "[portal] SSH トンネル (Mac:${PORT} → Windows → WSL:${PORT}) を起動中…"
  # -o LogLevel=ERROR で Windows バナーを抑制, 2>/dev/null でも抑制
  ssh -o LogLevel=ERROR -L "${PORT}:localhost:${PORT}" win -N -f 2>/dev/null

  # トンネル経由でサーバーに繋がるか確認
  sleep 1
  if curl -s --max-time 3 "http://localhost:${PORT}/" > /dev/null 2>&1; then
    echo "[portal] トンネル疎通確認 OK"
  else
    echo "[portal] 警告: サーバーに繋がりませんでした。WSL側のログを確認してください:"
    echo "  ssh win \"wsl -- bash -c 'tmux attach -t ${TMUX_SESSION}'\""
  fi

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
