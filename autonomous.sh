#!/usr/bin/env bash
# autonomous.sh — DL/文字起こし自律ループ（rate-limit自動回復）
#
# 使い方:
#   ./autonomous.sh                          # デフォルト設定で起動
#   ./autonomous.sh --limit 20 --model large-v3
#   ./autonomous.sh --dl-sleep 60 --probe-interval 60
#
# 動作:
#   - DLワーカー（バックグラウンド）: チャンネルを巡回して queue/ に音声を蓄積
#     rate-limit 検知 → プローブループで解除を能動検知 → 自動再開
#   - 文字起こしワーカー（フォアグラウンド）: queue/ を常時ドレイン（GPU常時稼働）
#   - Ctrl+C で両ワーカーを安全停止 → [session-end] を logs/autonomous/*.log に追記

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DL_SLEEP=60          # チャンネル間DLスリープ(s)
LIMIT=20             # チャンネルあたりDL上限
MODEL=large-v3
PROBE_INTERVAL=60    # rate-limit中の復帰チェック間隔(s)
DRAIN_POLL=10        # drain-queue終了後の待機(s)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)          LIMIT="$2";          shift 2 ;;
    --model)          MODEL="$2";          shift 2 ;;
    --dl-sleep)       DL_SLEEP="$2";       shift 2 ;;
    --probe-interval) PROBE_INTERVAL="$2"; shift 2 ;;
    --drain-poll)     DRAIN_POLL="$2";     shift 2 ;;
    *) echo "Unknown option: $1"; echo "Usage: $0 [--limit N] [--model MODEL] [--dl-sleep N] [--probe-interval N] [--drain-poll N]"; exit 1 ;;
  esac
done

LOG_DIR="$SCRIPT_DIR/logs/autonomous"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date '+%Y%m%d_%H%M%S')_autonomous.log"

SESSION_START=$(date +%s)
DL_PID=""

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

cleanup() {
  local elapsed=$(( $(date +%s) - SESSION_START ))
  local h=$(( elapsed / 3600 ))
  local m=$(( (elapsed % 3600) / 60 ))
  [ -n "$DL_PID" ] && kill "$DL_PID" 2>/dev/null
  local total; total=$(grep -c '^\[saved\]' "$LOG_FILE" 2>/dev/null || echo 0)
  local summary="[session-end] mode=autonomous, transcribed=${total}件, elapsed=${h}h${m}m"
  echo "$summary" | tee -a "$LOG_FILE"
  exit 0
}
trap cleanup SIGINT SIGTERM

# channels.txt からチャンネル名一覧を読み込み
CHANNELS=()
while IFS= read -r line; do
  [[ "$line" =~ ^#|^[[:space:]]*$ ]] && continue
  name="${line%%|*}"
  name="${name%"${name##*[! ]}"}"
  CHANNELS+=("$name")
done < "$SCRIPT_DIR/channels.txt"

if [[ ${#CHANNELS[@]} -eq 0 ]]; then
  echo "[error] channels.txt にチャンネルが登録されていません"
  exit 1
fi

# ──────────────────────────────────────────────────────────────
# DLワーカー（バックグラウンドで起動）
# rate-limit 検知 → プローブループで解除を能動検知 → 自動再開
# ──────────────────────────────────────────────────────────────
dl_worker() {
  while true; do
    rate_limited=false

    for name in "${CHANNELS[@]}"; do
      tmpout=$(mktemp)
      python "$SCRIPT_DIR/transcribe.py" channel "$name" \
        --download-only --sort popular --limit "$LIMIT" 2>&1 \
        | tee -a "$LOG_FILE" > "$tmpout"

      if grep -q '\[rate-limit\]' "$tmpout"; then
        rate_limited=true
        log "[dl] rate-limit 検知 → 文字起こしは継続しながら復帰待機..."
        rm -f "$tmpout"
        break
      fi

      added=$(grep '\[queue-added\]' "$tmpout" | grep -oP '\d+(?= 件)' | head -1)
      log "[dl] ${name}: ${added:-0}件キュー追加"
      rm -f "$tmpout"

      sleep "$DL_SLEEP"
    done

    # rate-limit 中はプローブして解除を能動的に検知する
    if $rate_limited; then
      while true; do
        log "[dl] rate-limit 中: ${PROBE_INTERVAL}s 後に復帰チェック..."
        sleep "$PROBE_INTERVAL"

        probe_out=$(mktemp)
        python "$SCRIPT_DIR/transcribe.py" channel "${CHANNELS[0]}" \
          --download-only --limit 1 > "$probe_out" 2>&1

        if ! grep -q '\[rate-limit\]' "$probe_out"; then
          log "[dl] rate-limit 解除を検知！DL 再開"
          rm -f "$probe_out"
          break
        fi
        log "[dl] まだ rate-limit 中..."
        rm -f "$probe_out"
      done
    fi
  done
}

# ──────────────────────────────────────────────────────────────
# 文字起こしワーカー（フォアグラウンド）
# queue/ を常時ドレイン。GPU を常時稼働させる。
# ──────────────────────────────────────────────────────────────
transcribe_worker() {
  while true; do
    python "$SCRIPT_DIR/transcribe.py" drain-queue \
      --model "$MODEL" --idle-polls 3 --idle-sleep 10 2>&1 \
      | tee -a "$LOG_FILE" > /dev/null
    exit_code=${PIPESTATUS[0]}

    if [[ "$exit_code" -ne 0 && "$exit_code" -ne 2 ]]; then
      log "[transcribe] エラー (exit=${exit_code}): 30s 待機後にリトライ..."
      sleep 30
    else
      sleep "$DRAIN_POLL"
    fi
  done
}

# ──────────────────────────────────────────────────────────────
# 起動
# ──────────────────────────────────────────────────────────────
log "Starting autonomous: channels=${#CHANNELS[@]}, limit=${LIMIT}, model=${MODEL}, dl_sleep=${DL_SLEEP}s, probe=${PROBE_INTERVAL}s"
log "Log: $LOG_FILE"

dl_worker &
DL_PID=$!

transcribe_worker
