#!/usr/bin/env bash
# 使い方:
#   ./loop_transcribe.sh [optimal|conservative|moderate|aggressive]
#   ./loop_transcribe.sh --sleep 300 --limit 10 --model large-v3
#
# ベンチマーク実測（2026-05-20, 17チャンネル, tinyモデル）で確認した最適パラメータ:
#   optimal = sleep=300s, limit=10 → 22.2 videos/hour (暫定1位、2位の180sとほぼ同等)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHANNEL_SLEEP=""
LIMIT=""
MODEL=""
VARIANT=""

# 引数パース
if [[ $# -gt 0 ]]; then
  case "$1" in
    optimal|conservative|moderate|aggressive)
      VARIANT="$1"; shift ;;
    --sleep|--limit|--model)
      VARIANT="custom" ;;
    *)
      echo "Usage: $0 [optimal|conservative|moderate|aggressive]"
      echo "       $0 --sleep N --limit N [--model MODEL]"
      exit 1 ;;
  esac
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sleep)  CHANNEL_SLEEP="$2"; shift 2 ;;
    --limit)  LIMIT="$2"; shift 2 ;;
    --model)  MODEL="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# プリセット適用（optimal がベンチマーク実測による推奨設定）
case "${VARIANT:-optimal}" in
  optimal)      CHANNEL_SLEEP=${CHANNEL_SLEEP:-300}; LIMIT=${LIMIT:-10}; MODEL=${MODEL:-large-v3} ;;
  conservative) CHANNEL_SLEEP=${CHANNEL_SLEEP:-300}; LIMIT=${LIMIT:-3};  MODEL=${MODEL:-large-v3} ;;
  moderate)     CHANNEL_SLEEP=${CHANNEL_SLEEP:-120}; LIMIT=${LIMIT:-5};  MODEL=${MODEL:-large-v3} ;;
  aggressive)   CHANNEL_SLEEP=${CHANNEL_SLEEP:-60};  LIMIT=${LIMIT:-10}; MODEL=${MODEL:-large-v3} ;;
  custom)       : ;;
esac

# カスタム時のデフォルト
CHANNEL_SLEEP=${CHANNEL_SLEEP:-300}
LIMIT=${LIMIT:-10}
MODEL=${MODEL:-large-v3}
LABEL="${VARIANT:-custom_s${CHANNEL_SLEEP}_l${LIMIT}}"

LOG_DIR="$SCRIPT_DIR/logs/loop"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date '+%Y%m%d_%H%M%S')_${LABEL}.log"

SESSION_START=$(date +%s)
TOTAL=0
TOTAL_CH_RUNS=0
ERRORS=0

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [${LABEL}] $1"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

cleanup() {
  local elapsed=$(( $(date +%s) - SESSION_START ))
  local h=$(( elapsed / 3600 ))
  local m=$(( (elapsed % 3600) / 60 ))
  local summary="[session-end] variant=${LABEL}, total=${TOTAL}件, channels=${TOTAL_CH_RUNS}, elapsed=${h}h${m}m, errors=${ERRORS}"
  echo "$summary"
  echo "$summary" >> "$LOG_FILE"
  exit 0
}
trap cleanup SIGINT SIGTERM

# channels.txt からチャンネル名一覧を読み込み
CHANNELS=()
while IFS= read -r line; do
  [[ "$line" =~ ^#|^[[:space:]]*$ ]] && continue
  name="${line%%|*}"
  name="${name%"${name##*[! ]}"}"  # trim trailing spaces
  CHANNELS+=("$name")
done < "$SCRIPT_DIR/channels.txt"

log "Starting loop: variant=${LABEL}, channels=${#CHANNELS[@]}, sleep=${CHANNEL_SLEEP}s, limit=${LIMIT}, model=${MODEL}"
log "Log: $LOG_FILE"

while true; do
  round_start=$(date +%s)

  for name in "${CHANNELS[@]}"; do
    ch_start=$(date +%s)
    tmpout=$(mktemp)

    python "$SCRIPT_DIR/transcribe.py" channel "$name" \
      --sort popular --limit "$LIMIT" --model "$MODEL" 2>&1 \
      | tee -a "$LOG_FILE" > "$tmpout"

    exit_code=${PIPESTATUS[0]}

    count=$(grep '^\[done\]' "$tmpout" | grep -oP '\d+(?= 件処理)' | head -1)
    count=${count:-0}
    rm -f "$tmpout"

    ch_elapsed=$(( $(date +%s) - ch_start ))
    em=$(( ch_elapsed / 60 )); es=$(( ch_elapsed % 60 ))

    TOTAL=$(( TOTAL + count ))
    (( TOTAL_CH_RUNS++ ))
    [[ "$exit_code" -ne 0 && "$count" -eq 0 ]] && (( ERRORS++ )) || true

    note=""
    [[ "$count" -eq 0 ]] && note=" (rate-limited?)"
    log "${name}: ${count}件処理${note} / elapsed ${em}m${es}s"

    log "sleep ${CHANNEL_SLEEP}s"
    sleep "$CHANNEL_SLEEP"
  done

  round_elapsed=$(( $(date +%s) - round_start ))
  rm=$(( round_elapsed / 60 ))
  log "--- 1周完了: round_total=${TOTAL}件 / round_elapsed=${rm}m ---"
done
