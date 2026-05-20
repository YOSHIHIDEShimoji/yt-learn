# yt-learn — Claude Code 引き継ぎガイド

## このプロジェクトは何をするか

channels.txt に登録した YouTube チャンネルの動画を、人気順に自動的に音声ダウンロード → 文字起こし → 要約して Google Drive に蓄積するパイプライン。WSL（CUDA GPU）で常時稼働させることを想定。

---

## 主要スクリプト

### `transcribe.py` — コアエンジン（触るときは慎重に）

```bash
python transcribe.py channel "チャンネル名" --sort popular --limit 10 --model large-v3
python transcribe.py all --sort popular --limit 10 --model large-v3
python transcribe.py sync --only transcripts
```

- 音声ダウンロード（yt-dlp）と文字起こし（faster-whisper）を直列に処理
- `[done] チャンネル名: N 件処理` で正常完了
- `[warn] ... レートリミット → 中断` でrate-limit検知、次チャンネルへ
- `[error]` 行が出たら本物のエラー

### `summarize.py` — 要約エンジン

```bash
python summarize.py all --threshold 20
```

Ollama（ローカルLLM）か Gemini（クラウド）で要約。

### `loop_transcribe.sh` — 常時稼働ループ（issue #13 で実装）

```bash
./loop_transcribe.sh             # optimal プリセットで起動（推奨）
./loop_transcribe.sh optimal     # sleep=300s, limit=10, model=large-v3
./loop_transcribe.sh moderate    # sleep=120s, limit=5
./loop_transcribe.sh --sleep 300 --limit 10 --model large-v3  # カスタム
# Ctrl+C で安全停止 → [session-end] 行を logs/loop/*.log に追記
```

**推奨パラメータ: `optimal`（sleep=300s, limit=10）**
2026-05-20 のベンチマーク（17チャンネル×18通り）で最高スコア **22.2 videos/hour** を記録。

### `benchmark.sh` — パラメータ最適化ツール（issue #13 で実装）

```bash
./benchmark.sh                # tiny モデル, 先頭5チャンネル
./benchmark.sh --channels 17  # 全チャンネル（精度高・時間長）
```

channel_sleep × limit の18通り（6×3）を総当たりで実行し、videos/hour でスコアリング。結果は `logs/benchmark/*.log`。

---

## ベンチマーク結果（2026-05-20 実測）

| sleep | limit | 成功/17ch | rate-limited | 件数 | v/hour |
|---|---|---|---|---|---|
| **300s** | **10** | **10** | **7** | **96件** | **22.2** ← 採用 |
| 180s | 10 | 7 | 10 | 51件 | 21.6 |
| 300s | 3 | 8 | 9 | 24件 | 12.1 |
| 300s | 5 | 5 | 12 | 21件 | 8.0 |

**知見**: limit=10 が limit=3/5 に対して圧倒的に優位。sleep=300s と 180s の差は小さい。ひろゆき・REWIRE は構造的にレートリミットが厳しく常時0件。

---

## ログの見方

```bash
# ループセッションの実績比較
grep '\[session-end\]' logs/loop/*.log

# 直近のループ状態
tail -30 logs/loop/*.log

# ベンチマーク結果一覧（v/hour 降順）
grep -v '^#\|^$\|^##\|^ ' logs/benchmark/*.log | sort -t$'\t' -k7 -rn | head -10
```

---

## 環境

- Python: `/home/wsl-yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python`（`.python-version` で自動選択）
- GPU: CUDA（WSL）
- LLM: Ollama（localhost）or Gemini（フォールバック）
- `cookies.txt`: gitignore 対象。`python transcribe.py refresh-cookies` で Windows Firefox から自動更新（autonomous.sh 起動時に自動実行）

---

## 現在の未解決 issue

| # | タイトル |
|---|---|
| #13 | feat: 常時稼働ループスクリプト → **実装済み（このコミット）** |
| #14 | feat: 自律型DL/文字起こし分離スクリプト → **次の実装候補** |
| #3 | channels.txt にチャンネルを追加する |
| #2 | 文字起こしの活用方法を検討する |

---

## 次に実装すべきこと（issue #14）

DLと文字起こしを非同期分離する自律型スクリプト:
- rate-limit 検知 → DL停止、文字起こしは継続
- rate-limit 回復検知 → DL自動再開
- GPUを常にフル稼働させ、現状比スループット大幅向上
- 詳細は issue #14 を参照
