# Portal Phase 1 ハンドオフ

**最終更新**: 2026-05-21  
**対象ブランチ**: `feat/portal`  
**HEAD**: `38a49dd`

---

## Phase 1 で完成したもの

### アクセス構成

```
Mac ──Tailscale──▶ WSL (100.85.4.93:8080)   ← Mac から直接アクセス
Windows            ──▶ localhost:8080         ← Windows ブラウザ
```

`portal.sh` が実行環境（Mac / WSL / Linux）を自動判定して起動。  
WSL 側の uvicorn は `--reload` で動いているので Python ファイル変更は即反映。  
静的ファイル（JS/CSS）はブラウザリロードで反映。

### 実装済みタブ・機能

| タブ | 実装内容 |
|------|----------|
| HOME | channels.txt から登録チャンネル一覧を表示。ロゴクリックで HOME に戻る |
| HOME | 各チャンネルに YouTube リンク + Drive フォルダリンク（ぽんアニメ付き、遅延取得）|
| STATUS | autonomous.sh の稼働状況（稼働中/停止/rate-limit）をバッジ表示 |
| STATUS | 今セッションの処理済み動画一覧（running: 水色 / done: 緑）+ Drive 個別リンク |
| STATUS | 統計パネル（queue件数・完了・警告・エラー・rate-limit回数・フェーズ） |
| STATUS | GPU placeholder（Phase 3 予定）|
| STATUS | 15 秒ポーリング（タブ表示中のみ）。Drive リンクはぴょんアニメで出現 |
| LIBRARY | placeholder のみ |
| LOGS | ログファイル一覧（live: 水色 / done: グレー、live が先頭） |
| LOGS | ファイルクリックでログビューア表示（カラーハイライト付き） |
| README | README.md をマークダウンレンダリング（ローカル marked.min.js 使用） |
| README | ↻ 再読み込みボタン |
| 共通 | Apple liquid glass ダークテーマ CSS |
| 共通 | URL ハッシュ対応（リロードしても同タブに戻る） |

### Drive リンク取得の仕組み

- **フォルダ URL**（HOME/STATUS の Google Drive ボタン）: `rclone link` でキャッシュ付き取得
- **ファイル個別 URL**（STATUS done 動画の ↗ Drive）: `rclone lsjson --files-only` でチャンネルごとに一括取得。バックグラウンド非同期（タイムアウトなし）。失敗時はキャッシュせず次回リトライ
- **HOME チャンネル Drive リンク**: `/api/channel-drive-urls` エンドポイント経由。`rclone link` でチャンネルフォルダ URL を取得

---

## 既知の不具合・やり残し（Phase 1 完了に必要なもの）

### 1. チャンネル一覧・LOGS が再訪問時に再ロードされない（最重要）

**現象**: HOME タブを一度開いて別タブに移動し、再度 HOME を開くと `dataset.loaded` ガードにより API を再フェッチしない。通常は DOM が保持されているので見えているが、**サーバー起動直後や初回アクセスで API が遅いとき**に「読み込み中…」のまま止まって見えることがある。

**根本原因**: `loadChannels()` と `loadLogs()` は `dataset.loaded` ガードを持ち、一度成功したら再フェッチしない設計。成功前に `dataset.loaded` がセットされることはないが、初回フェッチが遅いと「読み込み中」が長く表示される。

**修正案**:
```javascript
// ① loadChannels / loadLogs にも再読み込みボタンを追加（README と同様）
// ② または dataset.loaded ガードを外して毎回フェッチ（軽量 API なので問題なし）
// ③ または 10 秒タイムアウト後に「読み込み失敗」を確実に表示する
```

現在の `api()` 関数は 10 秒タイムアウト付き（commit 38a49dd で追加）。タイムアウト後は catch → `placeholder("⚠️", "読み込み失敗")` が表示されるので、**永遠に「読み込み中」にはならないはず**。ただし WSL サーバーが起動直後で重いときは 10 秒超えることがある。

### 2. LOGS タブのリロードボタンがない

一度読み込んだログ一覧は `dataset.loaded` により更新されない。STATUS タブの ↻ 更新や README の ↻ 再読み込みと同様のボタンが必要。

**修正**: LOGS タブのカードタイトルを `card-title-row` に変えてボタン追加 + `window.reloadLogs` を実装。

### 3. HOME チャンネル一覧のリロードボタンがない

channels.txt を更新したとき、ブラウザリロードなしに反映する手段がない。

---

## ファイル構成（現在）

```
portal/
├── main.py           # FastAPI アプリ
│   ├── GET /                    → index.html
│   ├── GET /api/channels        → channels.txt パース
│   ├── GET /api/channel-drive-urls  → rclone link でチャンネルフォルダ URL
│   ├── GET /api/readme          → README.md テキスト
│   ├── GET /api/logs            → ログファイル一覧（live/done 判定）
│   ├── GET /api/log-content     → ログファイル内容
│   └── GET /api/status-summary  → 最新ログ解析結果
├── templates/
│   └── index.html    # 5タブ HTML
└── static/
    ├── style.css     # CSS
    ├── app.js        # JS（ポーリング・Drive リンク・アニメ）
    ├── logo.png      # ロゴ画像（暗背景版）
    └── marked.min.js # ローカル bundled（CDN なし）
portal.sh             # 起動スクリプト（Mac/WSL 自動判定）
```

---

## Phase 2 以降の予定（CLAUDE.md 参照）

| Phase | 内容 |
|-------|------|
| 2 | HOME タブ機能化（チャンネル追加/削除・実行パネル・URL 単発処理） |
| 3 | STATUS WebSocket リアルタイム更新・GPU グラフ |
| 4 | LIBRARY 全文検索 |
| 5 | デザイン精緻化 |

---

## 次セッションでやること（Phase 1 完了）

1. `loadChannels()` にリロードボタン追加（または `dataset.loaded` ガードを外す）
2. `loadLogs()` にリロードボタン追加
3. 上記修正後、全タブの動作を一通り確認して Phase 1 クローズ → `main` にマージ

---

## セッション内で発生したトラブルメモ

- **このセッションで app.js を Write で上書きしてしまいリグレッションが発生**。`git checkout HEAD -- <files>` で復旧。次セッションでは既存ファイルは必ず `Edit` で差分変更すること。Write を使う場合は `git show HEAD:<path>` で HEAD の内容を確認してから書くこと。
