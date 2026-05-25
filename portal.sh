#!/usr/bin/env bash
# portal.sh — yt-learn Portal 起動スクリプト
# Mac から実行 (デフォルト): WSL でサーバー起動 → WSL の Tailscale IP でブラウザを開く
# Mac から実行 (--local):    Mac でサーバーをローカル起動 → localhost でブラウザを開く
# WSL から実行: サーバーをローカルで起動 + Windows ブラウザを開く

set -euo pipefail

PORT=8080
PROJECT="yt-learn"
TMUX_SESSION="yt-portal"

# --local フラグ解析
LOCAL_MODE=false
for arg in "$@"; do
  [[ "$arg" == "--local" ]] && LOCAL_MODE=true
done

# ──────────────────────────────────────────
# Mac モード
# ──────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  echo "[portal] Mac モード${LOCAL_MODE:+ (--local)}"

  # --local: Mac 自身でサーバーを起動してローカルログを読む
  if [[ "$LOCAL_MODE" == true ]]; then
    cd "$(dirname "$(realpath "$0")")"
    # 旧サーバーを常に停止して最新コードで再起動
    OLD_PID=$(lsof -ti :"${PORT}" 2>/dev/null || true)
    if [[ -n "$OLD_PID" ]]; then
      echo "[portal] 旧サーバー (PID ${OLD_PID}) を停止して再起動します"
      kill "$OLD_PID" 2>/dev/null || true
      sleep 1
    fi
    echo "[portal] Mac ローカルサーバーを起動します (port ${PORT})"
    (sleep 2 && open "http://localhost:${PORT}") &
    exec uvicorn portal.main:app --host 127.0.0.1 --port "${PORT}" --reload --reload-dir portal
  fi

  # WSL の IP を取得（Tailscale ミラーネットワーク）
  WSL_IP=$(ssh win "wsl -- bash -c 'hostname -I | cut -d\" \" -f1'" 2>/dev/null | tr -d '\r')
  if [[ -z "$WSL_IP" ]]; then
    echo "[portal] エラー: WSL の IP を取得できませんでした"
    exit 1
  fi
  echo "[portal] WSL IP: ${WSL_IP}"

  # WSL 側でサーバーを常に最新コードで再起動
  echo "[portal] WSL: tmux セッション '$TMUX_SESSION' を再起動中…"
  if ! ssh win "wsl -- bash -c 'cd ~/my-projects/${PROJECT} && tmux kill-session -t ${TMUX_SESSION} 2>/dev/null; tmux new-session -d -s ${TMUX_SESSION} ./src/portal-server.sh'"; then
    echo "[portal] エラー: WSL サーバー起動に失敗しました"
    exit 1
  fi

  # サーバー起動待機（最大 15 秒）
  echo "[portal] サーバー起動待機中…"
  _server_ready=false
  for i in $(seq 1 15); do
    if curl -s --max-time 1 "http://${WSL_IP}:${PORT}/" > /dev/null 2>&1; then
      echo "[portal] サーバー起動確認 (${i}秒)"
      _server_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$_server_ready" == false ]]; then
    echo "[portal] 警告: サーバー応答なし — ssh win で tmux ${TMUX_SESSION} のログを確認してください"
  fi

  echo "[portal] ブラウザを開きます: http://${WSL_IP}:${PORT}"
  open "http://${WSL_IP}:${PORT}"

  echo "[portal] 完了"
  echo "[portal] サーバー停止: ssh win \"wsl -- bash -c 'tmux kill-session -t ${TMUX_SESSION}'\""

# ──────────────────────────────────────────
# WSL モード
# ──────────────────────────────────────────
elif grep -qi microsoft /proc/version 2>/dev/null; then
  cd "$(dirname "$(realpath "$0")")"

  # Windows ブラウザを開く関数（cmd.exe start 経由で確実に Windows ブラウザを起動）
  open_browser() {
    local url="$1"
    echo "[portal] ブラウザ起動: ${url}"
    /mnt/c/Windows/System32/cmd.exe /c start "" "${url}" 2>/dev/null \
      || echo "[portal] 自動起動失敗 — ブラウザで手動で開いてください: ${url}"
  }

  # 旧セッションを常に停止して最新コードで再起動
  echo "[portal] WSL: tmux セッション '$TMUX_SESSION' を再起動中…"
  tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
  if ! tmux new-session -d -s "${TMUX_SESSION}" ./src/portal-server.sh; then
    echo "[portal] エラー: サーバー起動に失敗しました"
    exit 1
  fi

  # サーバー起動待機（最大 10 秒）
  echo "[portal] サーバー起動待機中…"
  _server_ready=false
  for i in $(seq 1 10); do
    if curl -s --max-time 1 "http://localhost:${PORT}/" > /dev/null 2>&1; then
      echo "[portal] サーバー起動確認 (${i}秒)"
      _server_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$_server_ready" == false ]]; then
    echo "[portal] 警告: サーバー応答なし — tmux attach -t ${TMUX_SESSION} でログを確認してください"
  fi

  open_browser "http://localhost:${PORT}"

  echo "[portal] 完了"
  echo "[portal] サーバー停止: tmux kill-session -t ${TMUX_SESSION}"

# ──────────────────────────────────────────
# その他 Linux
# ──────────────────────────────────────────
else
  echo "[portal] Linux モード"
  cd "$(dirname "$(realpath "$0")")"
  exec uvicorn portal.main:app --host 0.0.0.0 --port "${PORT}" --reload --reload-dir portal
fi
