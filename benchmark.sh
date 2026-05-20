#!/usr/bin/env bash
# パラメータ総当たりベンチマーク: channel_sleep × limit の全組み合わせを試してスコアを比較

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="tiny"
TEST_CHANNELS=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)     MODEL="$2"; shift 2 ;;
    --channels)  TEST_CHANNELS="$2"; shift 2 ;;
    *) echo "Usage: $0 [--model tiny|small|...] [--channels N]"; exit 1 ;;
  esac
done

LOG_DIR="$SCRIPT_DIR/logs/benchmark"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date '+%Y%m%d_%H%M%S')_benchmark.log"

# パラメータグリッド（遅い→速い順で実行：ユーザー指定）
SLEEP_VALUES=(300 180 120 60 30 10)
LIMIT_VALUES=(10 5 3)

# channels.txt から先頭 N チャンネルの名前を取得
CHANNELS=()
while IFS= read -r line; do
  [[ "$line" =~ ^#|^[[:space:]]*$ ]] && continue
  name="${line%%|*}"
  name="${name%"${name##*[! ]}"}"  # trim trailing spaces
  CHANNELS+=("$name")
  [[ ${#CHANNELS[@]} -ge $TEST_CHANNELS ]] && break
done < "$SCRIPT_DIR/channels.txt"

tee_log() { echo "$1" | tee -a "$LOG_FILE"; }

tee_log "# benchmark.sh — $(date '+%Y-%m-%d %H:%M:%S')"
tee_log "# model=$MODEL, test_channels=${#CHANNELS[@]}"
tee_log "# channels: ${CHANNELS[*]}"
tee_log "#"
tee_log "# sleep	limit	ok_channels	rl_channels	total_videos	elapsed_sec	videos_per_hour"

BEST_VPH=0
BEST_PARAMS=""

for ch_sleep in "${SLEEP_VALUES[@]}"; do
  for limit in "${LIMIT_VALUES[@]}"; do
    combo_start=$(date +%s)
    ok_ch=0
    rl_ch=0
    total_videos=0

    tee_log ""
    tee_log "## sleep=${ch_sleep}s limit=${limit} ----------------------------------------"

    tmpout=$(mktemp)
    for idx in "${!CHANNELS[@]}"; do
      name="${CHANNELS[$idx]}"
      ch_start=$(date +%s)
      python "$SCRIPT_DIR/transcribe.py" channel "$name" \
        --sort popular --limit "$limit" --model "$MODEL" 2>&1 \
        | tee -a "$LOG_FILE" > "$tmpout"

      # [done] 行から件数抽出
      count=$(grep '^\[done\]' "$tmpout" | grep -oP '\d+(?= 件処理)' | head -1)
      count=${count:-0}

      ch_elapsed=$(( $(date +%s) - ch_start ))
      em=$(( ch_elapsed / 60 )); es=$(( ch_elapsed % 60 ))

      if [[ "$count" -gt 0 ]]; then
        (( ok_ch++ ))
        total_videos=$(( total_videos + count ))
        tee_log "  -> ${name}: ${count}件 / ${em}m${es}s"
      else
        (( rl_ch++ ))
        tee_log "  -> ${name}: 0件 (rate-limited?) / ${em}m${es}s"
      fi

      # 最終チャンネルの後はスリープ不要
      if [[ "$ch_sleep" -gt 0 && "$idx" -lt $(( ${#CHANNELS[@]} - 1 )) ]]; then
        tee_log "  sleep ${ch_sleep}s"
        sleep "$ch_sleep"
      fi
    done
    rm -f "$tmpout"

    combo_elapsed=$(( $(date +%s) - combo_start ))
    if [[ "$combo_elapsed" -gt 0 ]]; then
      vph=$(echo "scale=1; $total_videos * 3600 / $combo_elapsed" | bc)
    else
      vph="0.0"
    fi

    result="${ch_sleep}	${limit}	${ok_ch}	${rl_ch}	${total_videos}	${combo_elapsed}	${vph}"
    tee_log "$result"

    # ベスト更新チェック（vph が最大のもの）
    if (( $(echo "$vph > $BEST_VPH" | bc -l) )); then
      BEST_VPH="$vph"
      BEST_PARAMS="sleep=${ch_sleep}s limit=${limit}"
    fi
  done
done

tee_log ""
tee_log "# ===== 結果サマリー（videos/hour 降順） ====="
grep -v '^#\|^$\|^##\|^ ' "$LOG_FILE" \
  | sort -t$'\t' -k7 -rn \
  | head -10 \
  | while IFS=$'\t' read -r s l ok rl tv es vph; do
      printf "  sleep=%-4ss limit=%-3s  ok=%-2s  rl=%-2s  videos=%-3s  v/h=%s\n" \
        "$s" "$l" "$ok" "$rl" "$tv" "$vph"
    done | tee -a "$LOG_FILE"

tee_log ""
tee_log "# 最高スコア: ${BEST_PARAMS} → ${BEST_VPH} videos/hour"
tee_log "# ログ: $LOG_FILE"
