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

### GPU 使用上の注意

`repoint.py` が常時 tmux セッションで動いており、Local LLM（Ollama）が GPU を占有している場合がある。
**WSL 側で GPU を使うテスト（transcribe.py 等）を実行する前に、必ず tmux ls で稼働中セッションを確認すること。**
Mac 側（`./portal.sh --local`）ではGPU を使わないためテストは自由に実施可能。

### tmux セッション起動・再起動

pyenv は `.zshrc` に初期化されているため、tmux は `zsh -ic` で起動すること（`-i` がないと pyenv が読まれず `python: command not found` になる）。

```bash
# Mac から WSL の tmux セッションを起動
ssh win "wsl -- bash -c 'cd ~/my-projects/yt-learn && tmux new-session -d -s yt-learn \"zsh -ic ./autonomous.sh\"'"

# 再起動（kill → git pull → 起動）
ssh win "wsl -- bash -c 'tmux kill-server 2>/dev/null; cd ~/my-projects/yt-learn && git pull && tmux new-session -d -s yt-learn \"zsh -ic ./autonomous.sh\"'"
```

---

## 現在の未解決 issue

| # | タイトル |
|---|---|
| #19 | feat: GUI ポータルアプリ化 → **骨格実装済み（feat/portal ブランチ）** |
| #13 | feat: 常時稼働ループスクリプト → **実装済み** |
| #14 | feat: 自律型DL/文字起こし分離スクリプト → 実装済み |
| #3 | channels.txt にチャンネルを追加する |
| #2 | 文字起こしの活用方法を検討する |

---

## Portal（issue #19）

### 概要

yt-learn を CLI から GUI ポータルへ昇華させる大型 feature（issue #19）。
WSL 上で FastAPI サーバーを動かし、Mac・Windows ブラウザ両方から同じ画面を見られる構成。

```
Mac browser ─────────────────────────┐
                                      ├─ FastAPI サーバー（WSL:8080）
Windows browser ─────────────────────┘
```

変更はリアルタイムで両ブラウザに反映（Phase 3 で WebSocket 実装）。

### 起動方法

**Mac から起動（推奨）**

```bash
cd ~/my-projects/yt-learn
./portal.sh
# → WSL でサーバー起動 + SSH トンネル + Mac ブラウザが自動で開く
```

**WSL から直接起動**

```bash
cd ~/my-projects/yt-learn
./portal.sh
# → uvicorn をローカルで起動 + Windows ブラウザが自動で開く
```

### Mac アクセスの仕組み

WSL は mirrored ネットワークモードで Tailscale IP を共有しているため、
**SSH トンネル不要**。Mac → WSL の Tailscale IP:8080 に直接アクセスする。

```
Mac ──Tailscale──▶ WSL (100.85.4.93:8080)
```

portal.sh が WSL の IP を自動取得して開く URL を決定するため手動設定不要。

WSL サーバーを止めたい場合（Mac から）：

```bash
ssh win "wsl -- bash -c 'tmux kill-session -t yt-portal'"
```

### ディレクトリ構成

```
portal/
├── main.py           # FastAPI アプリ本体
├── templates/
│   └── index.html    # タブ骨格 HTML（Apple liquid glass ダークテーマ）
└── static/
    ├── style.css     # CSS
    └── app.js        # タブ切り替え・API フェッチ
portal.sh             # 起動スクリプト（Mac/WSL 自動判定）
```

### 実装フェーズ

**マージルール**: 各フェーズ完了後、`feat/portal-*` → `main` へのマージは**必ずユーザーの承認を得てから**行うこと。フェーズが終わったら「マージしますか？」と確認し、指示があってからマージする。

| Phase | 内容 | 状態 |
|-------|------|------|
| **1** | 骨格 + チャンネル一覧・STATUS・LOGS・README 表示 + Mac ローカルモード | ✅ 完了（2026-05-21） |
| **2** | HOME タブ機能化（チャンネル追加/削除・実行パネル・URL処理・Summarize/Sync）| ✅ 完了（2026-05-22） |
| **3** | SSE リアルタイム更新・ライブログストリーム・ジョブ中止・ログフィルタ | ✅ 完了（2026-05-22） |
| **3 ext** | マルチプロセス STATUS・summarize per-video 表示・idle 修正・env バッジ | ✅ 完了（2026-05-22） |
| **4** | LIBRARY タブ（トランスクリプト全文検索）| 未着手 |
| 5 | Apple liquid glass デザイン精緻化 | 未着手 |
| 6 | Tailscale direct アクセス（Windows portproxy）| 未着手 |

### Phase 1 でできること

- HOME タブ: channels.txt のチャンネル一覧 + Google Drive フォルダリンク表示
- STATUS タブ: 処理済み動画・統計・Drive リンク（15 秒ポーリング自動更新）
- LOGS タブ: ログファイル一覧 + ビューアー（手動更新）
- README タブ: README.md をレンダリング
- タブ切り替え（URL ハッシュ対応）
- `./portal.sh --local` で Mac 上のログを読むローカルモード
- Drive URL キャッシュ（メモリ + ファイル永続化、サーバー再起動後も即表示）
- ログ終了マーカー `[session-end]` 統一（live/done 判定の信頼性向上）

### Phase 2 で実装したこと

- HOME > チャンネル管理: `+` 追加 / `×` 削除（channels.txt 編集）、カスタム確認ダイアログ
- HOME > クイック実行: `autonomous.sh` 起動/停止（WSL 専用）
- HOME > URL 処理: 複数 URL 一括処理、チャンネル名 select、ログ出力
- HOME > その他: Summarize All / Drive Sync
- STATUS テキストを英語統一（running / stopped / rate-limit / idle 等）
- tmux セッション名を `yt-learn_YYYYMMDD_HHMMSS` 自動生成
- favicon 204、docs/ 静的配信
- **マージルール**: 各フェーズ完了後、`feat/portal-*` → `main` へのマージは**必ずユーザーの承認を得てから**行うこと

### Phase 3 で実装したこと（2026-05-22 完了）

**リアルタイム更新（SSE）**
- `GET /api/events`: 5秒ごとにステータスデータをプッシュ（STATUS タブのポーリング廃止）
- `GET /api/log-stream?path=...`: live ログを 2秒間隔で tail -f 相当配信

**ジョブ管理**
- `_active_jobs` dict で PID 追跡（process/summarize/sync/transcribe）
- `GET /api/jobs`: 実行中ジョブ一覧
- `POST /api/jobs/{job_id}/stop`: ジョブを terminate

**STATUS タブ改善**
- `session_type` フィールドで autonomous/process/summarize/sync/idle を動的判別
- STATUS ヘッダーに実行中スクリプト名と中止ボタンを表示
- 統計（done/warn/error/rate-limit）をクリックしてログ行フィルタモーダル表示

**LOGS タブ改善**
- live ログを選択すると EventSource で自動追従（done 検知でバッジ更新）

### Phase 3 ext で実装したこと（2026-05-22 完了、feat/portal-multi-process）

**マルチプロセス STATUS**
- `/proc` スキャンで手動起動プロセス（transcribe.py / summarize.py 直接実行）を自動検出
- プロセスタブ UI: 複数プロセスを切り替えて各ログ・統計を個別表示
- `_find_log_for_pid()`: `/proc/<pid>/fd` fd スキャン + `[session-end]` なしログへフォールバック
- idle 時（active processes なし）に過去ログを表示しない

**summarize 対応**
- `summarize.py` にログ書き出し追加（`logs/summarize/summarize_YYYYMMDD.log`）
- `_parse_summarize_videos()`: `[N/M]` 行で per-video リアルタイム追跡（running 動画タイトル表示）
- ログなし summarize プロセス用 `/api/summarize-session`: `summaries/*_processed.json` mtime から処理済みチャンネル + Drive リンクを返す
- `[drive]` 正規表現をスペース含みファイル名に対応（`\S+` → `.+?`）
- done_count を `[done]` 行カウント（チャンネル単位）に統一

**UI**
- ヘッダー env バッジ: WSL（Tux）/ Mac（Apple）アイコン + frosted pill デザイン
- favicon: `logo_transparent.png`

### Phase 4 で実装すべきこと（LIBRARY タブ — トランスクリプト全文検索）

**ブランチ**: `feat/portal-phase4`（`main` から切る）

#### 目標
`transcripts/` 以下に蓄積された Markdown トランスクリプトを全文検索し、
動画タイトル・チャンネル・内容で絞り込んで閲覧できる UI を作る。

#### データ構造
```
transcripts/
├── チャンネル名/
│   ├── 動画タイトル.md   ← 各動画の文字起こし（## ポイント セクション含む）
│   └── _index.json       ← チャンネルのメタデータ（title / url / count 等）
transcripts/misc/
│   └── _index.json
```

`_index.json` は `transcribe.py sync` で更新される。

#### 実装方針

**バックエンド（`portal/main.py`）**

```python
GET /api/library/search?q=<query>&channel=<ch>&page=<n>&per_page=20
```
- `q`: 全文検索クエリ（空なら全件）
- `channel`: チャンネル名フィルタ（空なら全チャンネル）
- `page`: ページ番号（1-based）
- レスポンス: `{results: [{channel, title, excerpt, path}], total, pages}`

検索実装: Python の `re.search` で十分（件数が多くなったら後で SQLite FTS に移行）。
`## ポイント` セクションのみ検索対象にすることで高速化。

```python
GET /api/library/channels
```
チャンネル名一覧と動画数を返す。

```python
GET /api/library/transcript?path=<relative_path>
```
指定 `.md` ファイルの内容を返す（ビューアー用）。

**フロントエンド（`portal/static/app.js`, `portal/templates/index.html`）**

LIBRARY タブの現在の「Phase 4 で実装予定」プレースホルダーを実装に置き換える。

```
┌──────────────────────────────────────────────────────┐
│ 🔍 [検索ボックス]   チャンネル: [select ▼]  [検索]   │
├──────────────────────────────────────────────────────┤
│ 結果: 234件                              ← ページネーション → │
│                                                      │
│ ┌────────────────────────────────────────────────┐   │
│ │ チャンネル名 / 動画タイトル                       │   │
│ │ ...マッチ前後のテキスト抜粋...                    │   │
│ └────────────────────────────────────────────────┘   │
│ ┌──────────────────┐                                 │
│ │ (クリックで全文表示)│                                │
│ └──────────────────┘                                 │
└──────────────────────────────────────────────────────┘
```

クリックでビューアーモーダルを開き、全文（Markdown レンダリング）を表示。

#### 検証手順
1. `./portal.sh --local` で起動
2. LIBRARY タブを開く
3. 検索ボックスに語句を入力 → 結果が表示される
4. チャンネルフィルタで絞り込み
5. 結果カードをクリック → ビューアーモーダルで全文確認

### 依存関係

`requirements.txt` に追加済み：

```
fastapi>=0.115
uvicorn[standard]>=0.30
jinja2>=3.1
```

WSL 側で未インストールの場合：

```bash
pip install fastapi "uvicorn[standard]" jinja2
```
