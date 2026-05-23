#!/usr/bin/env bash
# autonomous.sh — DL/文字起こし/要約 自律ループ
#
# 使い方:
#   ./autonomous.sh                          # デフォルト設定で起動
#   ./autonomous.sh --limit 20 --model large-v3
#
# 動作:
#   - DLワーカー（バックグラウンド）: チャンネルを巡回して queue/ に音声を蓄積
#     キュー200件超でDL一時停止 → 100件未満で再開（バックプレッシャー）
#     rate-limit 検知 → プローブループで解除を能動検知 → 自動再開
#     全チャンネル一周ごとに summarize.py all を実行
#   - 文字起こしワーカー（フォアグラウンド）: queue/ を常時ドレイン（GPU常時稼働）
#   - Ctrl+C で両ワーカーを安全停止 → [session-end] を logs/autonomous/*.log に追記

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DL_SLEEP=60          # チャンネル間DLスリープ(s)
LIMIT=20             # チャンネルあたりDL上限
MODEL=large-v3
PROBE_INTERVAL=60    # rate-limit中の復帰チェック間隔(s)
QUEUE_HIGH=200       # キューがこの件数以上でDL一時停止
QUEUE_LOW=100        # キューがこの件数を下回ったらDL再開

usage() {
  cat <<EOF
使い方: $0 [OPTIONS]

DL（バックグラウンド）と文字起こし（フォアグラウンド）を並列起動。
rate-limit 検知時は DL を自動停止・回復。キュー ${QUEUE_HIGH} 件超で DL 一時停止、${QUEUE_LOW} 件未満で再開。
DL が全チャンネルを一周するたびに summarize.py を実行。

オプション:
  --limit N            チャンネルあたりのDL上限 (default: ${LIMIT})
  --model MODEL        Whisper モデル名 (default: ${MODEL})
  --dl-sleep N         チャンネル間DLスリープ秒数 (default: ${DL_SLEEP}s)
  --probe-interval N   rate-limit 復帰チェック間隔秒数 (default: ${PROBE_INTERVAL}s)
  -h, --help           このヘルプを表示

例:
  $0
  $0 --limit 20 --model large-v3
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)        usage; exit 0 ;;
    --limit)          LIMIT="$2";          shift 2 ;;
    --model)          MODEL="$2";          shift 2 ;;
    --dl-sleep)       DL_SLEEP="$2";       shift 2 ;;
    --probe-interval) PROBE_INTERVAL="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; echo "Usage: $0 [--limit N] [--model MODEL] [--dl-sleep N] [--probe-interval N]"; exit 1 ;;
  esac
done

LOG_DIR="$SCRIPT_DIR/logs/autonomous"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date '+%Y%m%d_%H%M%S')_autonomous.log"

SESSION_START=$(date +%s)
DL_PID=""

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] ${WORKER:+$WORKER }$1"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

stamp() {
  awk -v w="${WORKER:-[??]}" '{ printf "[%s] %s %s\n", strftime("%Y-%m-%d %H:%M:%S"), w, $0; fflush() }'
}

queue_size() {
  find "$SCRIPT_DIR/queue" \( -name "*.m4a" -o -name "*.webm" -o -name "*.opus" -o -name "*.mp4" \) 2>/dev/null | wc -l
}

git_push_cache() {
  local idx_files=()
  while IFS= read -r f; do
    idx_files+=("$f")
  done < <(find "$SCRIPT_DIR/transcripts" -name "_index.json" 2>/dev/null)

  local changed
  changed=$(git -C "$SCRIPT_DIR" status --porcelain cache/ "${idx_files[@]}" 2>/dev/null)
  if [[ -z "$changed" ]]; then
    log "[git] 変更なし、スキップ"
    return
  fi

  git -C "$SCRIPT_DIR" add cache/ "${idx_files[@]}"
  git -C "$SCRIPT_DIR" commit -m "chore: update cache ($(date '+%Y-%m-%d'))" 2>&1 \
    | stamp | tee -a "$LOG_FILE"
  git -C "$SCRIPT_DIR" pull --rebase -X ours 2>&1 \
    | stamp | tee -a "$LOG_FILE" || { log "[git] rebase失敗"; return; }
  git -C "$SCRIPT_DIR" push 2>&1 \
    | stamp | tee -a "$LOG_FILE" \
    && log "[git] push完了" || log "[git] push失敗"
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
trap cleanup SIGINT SIGTERM SIGHUP

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
  WORKER="[DL]"
  while true; do
    rate_limited=false

    # チャンネルを1周するごとに cookies を更新
    python "$SCRIPT_DIR/transcribe.py" refresh-cookies 2>&1 | stamp | tee -a "$LOG_FILE"

    for name in "${CHANNELS[@]}"; do
      # キューが上限を超えていたら下限を下回るまで待機
      qs=$(queue_size)
      if [[ "$qs" -ge "$QUEUE_HIGH" ]]; then
        log "キュー${qs}件 ≥ ${QUEUE_HIGH} → 文字起こしに専念（${QUEUE_LOW}件を下回ったら再開）"
        while [[ $(queue_size) -ge "$QUEUE_LOW" ]]; do
          sleep 60
        done
        log "キュー$(queue_size)件 < ${QUEUE_LOW} → DL再開"
      fi

      tmpout=$(mktemp)
      python "$SCRIPT_DIR/transcribe.py" channel "$name" \
        --download-only --sort popular --limit "$LIMIT" 2>&1 \
        | stamp | tee -a "$LOG_FILE" | tee "$tmpout"

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

    # 全チャンネルを一周したら要約・git pushを実行（rate-limit で中断した場合はスキップ）
    if ! $rate_limited; then
      WORKER="[SUM]"
      log "DL 1周完了 → summarize.py 実行"
      python "$SCRIPT_DIR/summarize.py" all 2>&1 \
        | stamp | tee -a "$LOG_FILE"

      WORKER="[GIT]"
      log "cache/index を push（WSL優先）"
      git_push_cache

      WORKER="[DL]"
    fi

    # rate-limit 中はプローブして解除を能動的に検知する
    if $rate_limited; then
      while true; do
        log "[dl] rate-limit 中: ${PROBE_INTERVAL}s 後に復帰チェック..."
        sleep "$PROBE_INTERVAL"

        probe_out=$(mktemp)
        python "$SCRIPT_DIR/transcribe.py" channel "${CHANNELS[0]}" \
          --download-only --limit 1 2>&1 \
          | stamp | tee -a "$LOG_FILE" | tee "$probe_out"

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
  WORKER="[TX]"
  while true; do
    python "$SCRIPT_DIR/transcribe.py" drain-queue \
      --model "$MODEL" 2>&1 \
      | stamp | tee -a "$LOG_FILE"
    exit_code=${PIPESTATUS[0]}

    if [[ "$exit_code" -ne 0 && "$exit_code" -ne 2 ]]; then
      log "[transcribe] エラー (exit=${exit_code}): 30s 待機後にリトライ..."
      sleep 30
    else
      sleep 10
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
