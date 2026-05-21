#!/usr/bin/env bash
# portal.sh — yt-learn Portal 起動スクリプト
# Mac から実行: WSL でサーバー起動 → WSL の Tailscale IP でブラウザを開く
# WSL から実行: サーバーをローカルで起動 + Windows ブラウザを開く

set -euo pipefail

PORT=8080
PROJECT="yt-learn"
TMUX_SESSION="yt-portal"

# ──────────────────────────────────────────
# Mac モード
# ──────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  echo "[portal] Mac モード"

  # WSL の IP を取得（Tailscale ミラーネットワーク）
  WSL_IP=$(ssh win "wsl -- bash -c 'hostname -I | cut -d\" \" -f1'" 2>/dev/null | tr -d '\r')
  if [[ -z "$WSL_IP" ]]; then
    echo "[portal] エラー: WSL の IP を取得できませんでした"
    exit 1
  fi
  echo "[portal] WSL IP: ${WSL_IP}"

  # すでにサーバーが動いていればブラウザだけ開く
  if curl -s --max-time 1 "http://${WSL_IP}:${PORT}/" > /dev/null 2>&1; then
    echo "[portal] サーバーはすでに起動中です"
    echo "[portal] ブラウザを開きます: http://${WSL_IP}:${PORT}"
    open "http://${WSL_IP}:${PORT}"
    echo "[portal] サーバー停止: ssh win \"wsl -- bash -c 'tmux kill-session -t ${TMUX_SESSION}'\""
    exit 0
  fi

  # WSL 側でサーバーを tmux セッションで起動
  echo "[portal] WSL: tmux セッション '$TMUX_SESSION' を起動中…"
  ssh win "wsl -- bash -c 'cd ~/my-projects/${PROJECT} && tmux kill-session -t ${TMUX_SESSION} 2>/dev/null; tmux new-session -d -s ${TMUX_SESSION} ./portal-server.sh'" 2>/dev/null

  # サーバー起動待機（最大 15 秒）
  echo "[portal] サーバー起動待機中…"
  for i in $(seq 1 15); do
    if curl -s --max-time 1 "http://${WSL_IP}:${PORT}/" > /dev/null 2>&1; then
      echo "[portal] サーバー起動確認 (${i}秒)"
      break
    fi
    sleep 1
  done

  echo "[portal] ブラウザを開きます: http://${WSL_IP}:${PORT}"
  open "http://${WSL_IP}:${PORT}"

  echo "[portal] 完了"
  echo "[portal] サーバー停止: ssh win \"wsl -- bash -c 'tmux kill-session -t ${TMUX_SESSION}'\""

# ──────────────────────────────────────────
# WSL モード
# ──────────────────────────────────────────
elif grep -qi microsoft /proc/version 2>/dev/null; then
  cd "$(dirname "$(realpath "$0")")"

  # すでにサーバーが動いていればブラウザだけ開く
  if curl -s --max-time 1 "http://localhost:${PORT}/" > /dev/null 2>&1; then
    echo "[portal] サーバーはすでに起動中です"
    cmd.exe /c start "http://localhost:${PORT}" 2>/dev/null
    echo "[portal] ブラウザを開きました: http://localhost:${PORT}"
    echo "[portal] サーバー停止: tmux kill-session -t ${TMUX_SESSION}"
    exit 0
  fi

  echo "[portal] WSL モード — uvicorn を 0.0.0.0:${PORT} で起動します"

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
