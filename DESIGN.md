# yt-learn 設計メモ（WSL引き継ぎドキュメント）

> **WSL Claude Code 向け**: このファイルを読んで `/goal` を設定し、
> `python transcribe.py all --sort popular --limit 10 --model tiny` を WSL で実行して
> `[error]` と `ERROR:` 行がログに出ない状態にしてください。

---

## 目標

channels.txt に登録した YouTube チャンネルの動画を、**人気順で上位から**自動的に
文字起こし・要約して Google Drive に蓄積する。

Mac（Metal GPU）と WSL（CUDA GPU）の両方で同じコマンドが動く構成。

**ゴール（WSL 側）**:

```bash
python transcribe.py all --sort popular --limit 10 --model tiny
```

このコマンドが `[error]` / `ERROR:` 行ゼロで完走すること。

---

## アーキテクチャ

```
Mac
  ├─ yt-dlp（音声ダウンロード）← Chromeクッキー直接読み取り
  ├─ whisper.cpp（Metal GPU 文字起こし）
  ├─ Ollama（Windows Tailscale IP経由） → Geminiフォールバック
  └─ autonomous.sh 起動時に refresh-cookies で自動更新

WSL（Windows上）
  ├─ yt-dlp（音声ダウンロード）← cookies.txt を使用
  ├─ faster-whisper（CUDA GPU 文字起こし）
  └─ Ollama（localhost経由でWindows Ollamaに接続）
```

### Python 環境

- WSL: `/home/wsl-yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python`
- `.python-version` が virtualenv を指定しているので `python` コマンドで OK

### 主要ファイル

| ファイル | 役割 |
|----------|------|
| `transcribe.py` | メインスクリプト。全ロジックここに集約 |
| `autonomous.sh` | **自律ループ（推奨）** DL/文字起こし並列、rate-limit自動回復 |
| `loop_transcribe.sh` | 直列ループ（旧方式） |
| `benchmark.sh` | パラメータ最適化ツール（18通り総当たり） |
| `summarize.py` | AI要約エンジン（Ollama / Gemini） |
| `channels.txt` | チャンネル一覧（名前\|URL\|言語） |
| `cache/*.json` | 再生数キャッシュ（git管理・Mac↔WSL共有） |
| `queue/` | DL済み音声の一時置き場（gitignore対象） |
| `cookies.txt` | YouTubeクッキー（gitignore対象・Windows Firefox から自動取得） |
| `.env` | LOCAL_LLM_URL等（gitignore対象） |

---

## これまでに Mac 側 Claude Code が実装・修正した内容

### 1. メンバーシップ動画を sentinel キャッシュ

`view_cache.json` に `-1` を保存 → 次回以降スキップ。198件ループ問題を解消。

### 2. whisper.cpp 失敗の堅牢化

- ffmpeg stderr を `capture_output=True` でキャプチャ → 失敗時に `_err` 出力
- whisper-cli stderr を binary mode (`mode="w+b"`) + `decode(errors="replace")`
- whisper.cpp を `WHISPER_COREML_ALLOW_FALLBACK=ON` でビルド（`.mlmodelc` 不在でも exit 3 にならない）

### 3. `_TqdmLogger` と `_FilteredStderr` で ERROR: 行を抑制

```python
_SUPPRESSED_ERR_MARKERS = (
    "members-only", "members on level", "Join this channel",
    "confirm your age", "age-restricted", "rate-limited",
    "Sign in to confirm you're not a bot",
)
```

- `_TqdmLogger.error()` → 上記パターンを含む場合はサイレント
- `_FilteredStderr` → yt-dlp が `sys.stderr` に直接書く `ERROR:` 行を抑制
  - **重要**: `buffer` 属性を `AttributeError` で隠す。yt-dlp の `write_string` が
    `hasattr(out, 'buffer')` で True になると `out.buffer` に直接書いて
    Python レベルの `write()` を迂回するため

### 4. `_process_channel` 例外ハンドラの整備

- `members-only` → `[warn]` + continue
- `rate-limited` → `[warn]` + break（チャンネル中断、次チャンネルへ）
- `age-restricted` → `[warn]` + continue
- `bot 検知` → `[warn]` + continue
- それ以外 → `[error]`（本物のエラー）

### 5. `_sanitize()` のバイト長制限

Linux ext4 は 255 バイト制限。日本語 3 bytes/char のため文字数でなくバイト数で切る。

```python
encoded = name.encode("utf-8")
if len(encoded) > 200:
    name = encoded[:200].decode("utf-8", errors="ignore")
```

### 6. deno PATH 自動追加（WSL 向け）

`run_transcribe.sh` 経由でない直接 `python` 実行では `~/.deno/bin` が PATH に入らず、
yt-dlp の web クライアントが n-challenge 解決に失敗する。
transcribe.py 起動時に自動追加するよう修正済み（コミット `963a7db`）。

```python
_deno_bin = str(Path.home() / ".deno" / "bin")
if Path(_deno_bin).is_dir() and _deno_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _deno_bin + os.pathsep + os.environ.get("PATH", "")
```

### 7. `_web_client_args()` を全環境 web クライアントに統一

deno が PATH に入れば WSL でも web クライアントが使える。

```python
def _web_client_args() -> dict:
    return {"player_client": ["web"]}
```

---

## WSL でのテスト手順

### Step 1: 最新コードに更新

```bash
cd /home/wsl-yoshihide/my-projects/yt-learn
git pull
```

最新コミットは `963a7db`（deno PATH 自動追加）。

### Step 2: cookies.txt を確認

```bash
ls -la cookies.txt
```

古い場合は手動で更新:
```bash
python transcribe.py refresh-cookies
```
autonomous.sh 起動時は自動実行される。

### Step 3: yt-dlp が動くか確認（任意）

```bash
python -c "
import yt_dlp, os
print('deno in PATH:', 'deno' in os.environ.get('PATH',''))
url = 'https://www.youtube.com/watch?v=ewLmP32aTQg'
opts = dict(quiet=True, cookiefile='cookies.txt', skip_download=True,
            extractor_args={'youtube': {'player_client': ['web']}})
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=False)
    print('formats:', len(info.get('formats', [])))
"
```

期待結果: `formats: 数字`（1以上）

### Step 4: 実機テスト

```bash
python transcribe.py all --sort popular --limit 10 --model tiny 2>&1 | tee /tmp/yt-wsl-test.log
```

### Step 5: 合格確認

```bash
grep -E '^\[error\]|^ERROR:' /tmp/yt-wsl-test.log
# → 出力ゼロが合格

grep '\[done\]' /tmp/yt-wsl-test.log
# → 各チャンネルが "[done] チャンネル名: N 件処理" で終わること
```

---

## ループスクリプト運用

### ベンチマーク（初回・パラメータ最適化時）

channel_sleep × limit の18通りを自動実行して `videos/hour` を比較する。

```bash
./benchmark.sh                        # デフォルト: model=tiny, 先頭5チャンネル
./benchmark.sh --channels 17          # 全チャンネル（精度高・時間長）
```

結果確認:

```bash
grep -v '^#\|^$\|^##\|^ ' logs/benchmark/*.log | sort -t$'\t' -k7 -rn | head -5
```

**実測結果（2026-05-20, 17チャンネル）**:

| sleep | limit | ok/17 | rl | 件数 | v/hour |
|---|---|---|---|---|---|
| **300s** | **10** | **10** | **7** | **96件** | **22.2** ← 採用 |
| 180s | 10 | 7 | 10 | 51件 | 21.6 |
| 300s | 3 | 8 | 9 | 24件 | 12.1 |

→ `optimal` プリセット（sleep=300s, limit=10）を推奨パラメータとして採用。

### 本番稼働（自律型・推奨）

```bash
./autonomous.sh                   # デフォルト設定（limit=20, model=large-v3）
./autonomous.sh --limit 10 --model large-v3
./autonomous.sh --probe-interval 120  # rate-limit復帰チェック間隔を調整
# Ctrl+C で安全停止 → [session-end] 行を logs/autonomous/*.log に追記
```

**動作**: DL（バックグラウンド）と文字起こし（フォアグラウンド）を並列実行。
rate-limit 検知 → `--probe-interval` 秒ごとに YouTube に疎通チェック → 解除を検知したら DL 自動再開。
その間も文字起こしワーカーは queue/ をドレインし続けるため GPU はアイドルにならない。

```
./autonomous.sh を叩くだけ。あとは全自動。
```

**ログ確認**:
```bash
tail -f logs/autonomous/*.log
grep '\[session-end\]' logs/autonomous/*.log
```

### 本番稼働（直列ループ・旧方式）

```bash
./loop_transcribe.sh              # optimal プリセット（推奨・デフォルト）
./loop_transcribe.sh optimal      # sleep=300s, limit=10, model=large-v3
./loop_transcribe.sh conservative # sleep=300s, limit=3（安全優先）
./loop_transcribe.sh moderate     # sleep=120s, limit=5
./loop_transcribe.sh aggressive   # sleep=60s, limit=10
# Ctrl+C で即座に安全停止 → [session-end] 行をログに追記して終了
```

DL と文字起こしが直列のため、rate-limit 中は GPU がアイドルになる。
→ 新規稼働は `autonomous.sh` を推奨。

### 効率比較（複数セッション後）

```bash
grep '\[session-end\]' logs/loop/*.log
grep '\[session-end\]' logs/autonomous/*.log
```

`total件数 / elapsed時間` が最大で rate-limited が少ないセッションのパラメータが最適。

---

## 合格基準

| 確認項目 | 合格条件 |
|----------|----------|
| `[error]` 行 | ゼロ |
| `ERROR:` 行 | ゼロ（`_FilteredStderr` が抑制） |
| 各チャンネル | `[done] チャンネル名: N 件処理` で終了 |
| 少なくとも1チャンネル | N > 0 |

---

## 既知の無害パターン（合格として扱う）

| パターン | 意味 |
|----------|------|
| `[warn] xxx: bot検知 → スキップ` | 一時的な YouTube 制限。処理継続 |
| `[warn] xxx: 年齢制限 → スキップ` | 認証不足。スキップして続行 |
| `[warn] xxx: レートリミット → 中断` | 次チャンネルへ移行 |
| `[retry] bot検知 → 3秒待って再試行` | リトライログ。正常動作 |

---

## cookies.txt の扱い

| 項目 | 内容 |
|------|------|
| 取得方法 | Windows Firefox の `cookies.sqlite` を直接読んで Netscape 形式に変換 |
| 自動更新 | `autonomous.sh` 起動時に `refresh-cookies` を自動実行 |
| 手動更新 | `python transcribe.py refresh-cookies` |
| git 管理 | `.gitignore` 対象（認証情報のため）|

Chrome 127+ の App-Bound Encryption で Chrome/Edge は外部復号不可のため Firefox を採用。
Firefox 起動中・停止中どちらでも動作する。

---

## 過去の WSL エラー履歴（参考）

| エラー | 原因 | 対処（済み） |
|--------|------|-------------|
| `Requested format is not available` | deno が PATH にない → web_creator クライアントが n-challenge 失敗 | 起動時に `~/.deno/bin` を PATH 追加 |
| `Only images are available for download` | 同上 | 同上 |
| `Sign in to confirm you're not a bot` | cookies 期限切れ / YouTube の一時的な制限 | `[warn]` + スキップで継続 |
| `[Errno 36] File name too long` | Linux 255 バイト制限を文字数で計算していた | バイト数で切るよう修正 |
| `ERROR:` 行がログに残る | `_FilteredStderr.__getattr__` が `buffer` を委譲 → raw buffer 直接書き込み | `buffer` を `AttributeError` で隠す |
