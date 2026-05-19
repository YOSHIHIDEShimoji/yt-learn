# yt-learn 設計メモ（WSL引き継ぎドキュメント）

> **WSL Claude Code向け**: このファイルを読んで `/goal` を設定し、「WSLで `python transcribe.py all --sort popular --limit 10` を実行して `[error]` と `ERROR:` 行がログに出ない状態にする」を達成してください。

---

## やりたいこと

channels.txt に登録した YouTube チャンネルの動画を、**人気順で上位から**自動的に文字起こし・要約して Google Drive に蓄積する。

Mac（Metal GPU）と WSL（CUDA GPU）の両方で同じコマンドが動く構成。

```bash
python transcribe.py all --sort popular --limit 10
```

**ゴール**: 上記コマンドが Mac・WSL 両環境で `[error]` / `ERROR:` 行なしで完走すること。

---

## アーキテクチャ

```
Mac
  ├─ yt-dlp（音声ダウンロード）← Chromeクッキー直接読み取り
  ├─ whisper.cpp（Metal GPU 文字起こし）
  ├─ Ollama（Windows Tailscale IP経由） → Geminiフォールバック
  └─ cookies.txt を WSL に scp で同期

WSL（Windows上）
  ├─ yt-dlp（音声ダウンロード）← cookies.txt を使用
  ├─ faster-whisper（CUDA GPU 文字起こし）
  └─ Ollama（localhost経由でWindows Ollamaに接続）
```

### Python 環境

- Mac: `/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python`
- WSL: `/home/wsl-yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python`
- コマンド実行: `python transcribe.py ...`（`.python-version` が virtualenv を指定）

### 主要ファイル

| ファイル | 役割 |
|----------|------|
| `transcribe.py` | メインスクリプト。全ロジックここに集約 |
| `channels.txt` | チャンネル一覧（名前\|URL\|言語） |
| `cache/*.json` | 再生数キャッシュ（git管理・Mac↔WSL共有） |
| `transcripts/` | 文字起こし結果（.md形式） |
| `cookies.txt` | YouTubeクッキー（gitignore対象） |
| `.env` | LOCAL_LLM_URL等（gitignore対象） |

---

## cache/ をgit管理している理由

`cache/*.json`（チャンネル別の再生数キャッシュ）はgit管理対象。

- YouTube は 1 動画あたり数秒かかり、レートリミットもある
- 一度取得した再生数は半永久的に有効（人気順ランキングの判断に使う）
- Mac↔WSL の両環境で共有することで、片方で取得した再生数をもう片方でも使い回せる

---

## --sort popular の動作フロー

```
1. チャンネルの全動画リストを取得（yt-dlp extract_flat）
2. キャッシュ済みでない動画の再生数を取得（_sort_by_popularity）
3. 再生数降順にソート
4. 未処理の動画のうち上位 --limit 件を処理
```

---

## cookies.txt の扱い

| 環境 | 取得方法 | 保存先 |
|------|----------|--------|
| Mac | `cookiesfrombrowser=chrome`（実行時にChromeから直接読む） | `cookies.txt`（次回WSL同期用） |
| WSL | `cookiefile=cookies.txt`（Macからscpで転送されたファイル） | そのまま使用 |

`cookies.txt` は git 管理外（`.gitignore`）。Mac で `python transcribe.py sync-cookies` を実行することで WSL に転送される。

---

## これまでに実装・修正済みの内容

### 1. メンバーシップ動画の sentinel キャッシュ（`_fetch_view_count`）

- `view_cache.json` に `-1` を保存 → 次回以降スキップ（198件ループ問題を解消）
- `-1` はソートキーで `max(..., 0)` → 人気度ゼロ扱いで最後尾へ

### 2. whisper.cpp 失敗の堅牢化（`_transcribe_whisper_cpp`）

- ffmpeg の `stderr` を `capture_output=True` でキャプチャ → 失敗時に `_err` 出力
- whisper-cli の `stderr` を binary mode (`mode="w+b"`) で読む → `decode("utf-8", errors="replace")`
- whisper.cpp を `WHISPER_COREML_ALLOW_FALLBACK=ON` でビルド（`.mlmodelc` 不在でも exit 3 にならない）

### 3. bot 検知の 1 回リトライ（`_yt_extract_with_retry`）

- `skip_download=True` の extraction でのみ 3 秒待ってリトライ
- `_download_audio` 内では 3 回リトライ（5 秒インターバル）

### 4. `_FilteredStderr` で ERROR: 行を抑制

- yt-dlp が logger を経由せず直接 stderr に書く `ERROR:` 行を抑制するフィルター
- **重要**: `buffer` 属性を `AttributeError` で隠す → yt-dlp の `write_string` が `out.buffer` に直接書こうとするのを防ぎ、必ず `write()` 経由にする
- 抑制対象: `_SUPPRESSED_ERR_MARKERS` に登録されたパターン（members-only, age-restricted, bot 検知など）

### 5. `_process_channel` 例外ハンドラの整備

- `members-only` → `[warn]` + continue
- `rate-limited` → `[warn]` + break（チャンネルを中断、次チャンネルへ）
- `age-restricted` → `[warn]` + continue
- `bot 検知` → `[warn]` + continue
- それ以外 → `[error]`（本物のエラー）

### 6. `_sanitize()` のバイト長制限（Linux 255 バイト制限対応）

- `encoded[:200].decode("utf-8", errors="ignore")` → 日本語 3 bytes/char を考慮

### 7. `_web_client_args()` - Mac のみ web クライアント

```python
def _web_client_args() -> dict:
    if sys.platform == "darwin":
        return {"player_client": ["web"]}
    return {}
```

---

## 現在の問題点と未解決事項

### ⚠️ WSL: 「Requested format is not available」（最優先）

**症状**: v5 テストで全 DaiGo 動画が `[error]` になる

```
WARNING: [youtube] ewLmP32aTQg: n challenge solving failed: Some formats may be missing.
WARNING: Only images are available for download.
[error] 注意！カップルが最も分かれやすい時期とは？: ERROR: ...Requested format is not available
```

**根本原因**:
- `_web_client_args()` が WSL では `{}` を返す → yt-dlp のデフォルトクライアント（web_creator を含む）を使用
- `web_creator` クライアントは n-challenge（JS runtime）が必要
- deno は `~/.deno/bin/deno` にインストール済みだが、`run_transcribe.sh` 経由でないと PATH に入らない
- `python transcribe.py all` を直接実行すると deno が PATH にない → n-challenge 失敗 → フォーマット取得不可

**対処方針（2択）**:

**Option A（推奨）**: `_web_client_args()` を修正して WSL でも明示的に player_client を指定する

```python
def _web_client_args() -> dict:
    if sys.platform == "darwin":
        return {"player_client": ["web"]}
    # WSL: android クライアントは n-challenge 不要・フォーマット安定
    return {"player_client": ["android"]}
```

**Option B**: Python 起動時に deno を PATH に追加

```python
# transcribe.py 冒頭または _web_client_args() 内で
import os
deno_path = os.path.expanduser("~/.deno/bin")
if os.path.isdir(deno_path) and deno_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = deno_path + os.pathsep + os.environ.get("PATH", "")
```

Option A のほうがシンプルで依存が少ない。android クライアントは JS 不要で安定している。

### ⚠️ Mac: `all` コマンドの全チャンネル完走が未確認

- DaiGo（10件処理）は確認済み
- 残り 15 チャンネルは未テスト（ユーザーが外出したため中断）
- Mac 側はユーザーが手動で確認予定

### ✅ `_FilteredStderr` の buffer fix（コミット済み、WSL 未テスト）

`b6cdf62` でコミット済み。WSL v5 は fix 前に起動したため未検証。

---

## WSL での実装手順

### Step 1: コードを最新にする

```bash
cd /home/wsl-yoshihide/my-projects/yt-learn
git pull
```

### Step 2: `_web_client_args()` を修正（Option A）

`transcribe.py` の `_web_client_args()` を以下に変更:

```python
def _web_client_args() -> dict:
    """Mac: web クライアント（deno必要）。WSL: android クライアント（JS不要）。"""
    import sys
    if sys.platform == "darwin":
        return {"player_client": ["web"]}
    return {"player_client": ["android"]}
```

### Step 3: テスト実行

```bash
# pytest で回帰確認
/home/wsl-yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python -m pytest tests/test_transcribe.py -x

# 実機テスト（全チャンネル・上位10件）
python transcribe.py all --sort popular --limit 10 2>&1 | tee /tmp/yt-wsl-v6.log
```

### Step 4: 合格確認

```bash
# [error] と ERROR: がゼロであること（pytest ノイズは除外）
grep -E '\[error\]|^ERROR' /tmp/yt-wsl-v6.log

# 全チャンネルが [done] で終わること
grep '\[done\]' /tmp/yt-wsl-v6.log
```

**合格基準**:
- `[error]` 行: ゼロ（本物のエラーなし）
- `ERROR:` 行: ゼロ（`_FilteredStderr` が抑制）
- 各チャンネルが `[done] チャンネル名: N 件処理` で終了
- 少なくとも 1 チャンネルで N > 0 であること

---

## 既知の無害なパターン（合格として扱う）

- `[warn] xxx: bot検知 → スキップ` — 一時的な YouTube 制限、処理継続
- `[warn] xxx: 年齢制限 → スキップ` — 認証不足、スキップして続行
- `[warn] xxx: レートリミット → このチャンネルの処理を中断` — 次チャンネルへ移行
- `[retry] bot検知 → 3秒待って再試行` — リトライログ、正常動作

---

## 運用フロー

### 通常実行（Mac）

```bash
python transcribe.py all --sort popular --limit 100
```

### 通常実行（WSL）

```bash
python transcribe.py all --sort popular --limit 10
# または launchd から呼ばれる:
bash run_transcribe.sh
```

### クッキー同期（Mac→WSL）

```bash
# Mac 側で実行:
python transcribe.py sync-cookies
```

Mac の Chrome クッキーを WSL の `cookies.txt` に転送する。WSL は yt-dlp が Chrome に直接アクセスできないため。

### キャッシュのみ構築（新チャンネル追加時）

```bash
python transcribe.py channel "チャンネル名" --sort popular --cache-only
```
