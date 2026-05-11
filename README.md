# yt-learn

YouTubeチャンネルの動画を自動文字起こしし、Geminiでサマリーを蓄積するツール。

## ディレクトリ構造

```
yt-learn/
├── channels.txt         # 追跡するチャンネルのリスト
├── transcribe.py          # 文字起こしスクリプト
├── summarize.py         # AI要約スクリプト（手動実行 or run_summarize.sh 経由）
├── run_transcribe.sh    # 文字起こし自動実行ラッパー（launchd 用）
├── run_summarize.sh     # 要約自動実行ラッパー（launchd 用）
├── youtube.png          # 通知アイコン
├── .env                 # 環境変数（GEMINI_API_KEY など）※ git管理外
├── cache/               # 再生数キャッシュ（チャンネル別）
├── transcripts/         # チャンネル別の文字起こしファイル ※ git管理外
│   └── {チャンネル名}/
│       ├── _index.json      # 処理済み動画のインデックス
│       ├── _ranking.json    # 人気順ランキング（--sort popular 時に生成）
│       └── {動画タイトル}.md
└── summaries/           # チャンネル別サマリー ※ git管理外
    ├── {チャンネル名}.md
    └── {チャンネル名}_processed.json
```

## セットアップ

```bash
# .env を作成してGemini APIキーを設定
echo "GEMINI_API_KEY=your_key_here" > .env
```

### Google Drive 同期（rclone）

`transcripts/` と `summaries/` を Google Drive に同期するために rclone を使う。

```bash
# Mac
brew install rclone

# WSL
sudo apt install rclone
```

初回のみ Google Drive のリモートを設定する（ブラウザが開く）：

```bash
rclone config
# → n（新規）→ 名前: gdrive → Google Drive を選択 → ブラウザで認証
```

設定後、Drive 上の `yt-learn/` フォルダに同期される：

```bash
# transcripts/ と summaries/ を両方同期
python transcribe.py sync

# transcripts/ だけ同期
python transcribe.py sync --only transcripts

# summaries/ だけ同期
python transcribe.py sync --only summaries
```

## 使い方

### チャンネル管理

```bash
# チャンネルを追加
python transcribe.py add メンタリスト DaiGo https://www.youtube.com/@mentalistdaigo
python transcribe.py add ひろゆき https://www.youtube.com/@hiroyuki_daihyo

# 登録チャンネル一覧
python transcribe.py list
```

### 単発処理（特定URLを文字起こし）

```bash
# チャンネル指定なし → transcripts/misc/ に保存
python transcribe.py process https://youtu.be/xxx --model tiny

# チャンネル指定あり → transcripts/メンタリスト DaiGo/ に保存
python transcribe.py process https://youtu.be/xxx --channel "メンタリスト DaiGo" --model tiny

# 複数URL同時
python transcribe.py process https://youtu.be/aaa https://youtu.be/bbb --channel "メンタリスト DaiGo"

# URLファイルから読み込み → transcripts/ひろゆき/ に保存
python transcribe.py process -f urls.txt --channel ひろゆき

# 出力先を完全に指定（チャンネルディレクトリ無視）
python transcribe.py process https://youtu.be/xxx -o ~/Desktop/output
```

### チャンネル全取得

```bash
# 人気順で上位5本（動作確認用）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 5 --model tiny

# 人気順で上位100本（本番）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 100

# 2回目は自動で101〜200本目になる
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 100

# 全チャンネルを人気順50本ずつ
python transcribe.py all --sort popular --limit 50
```

`--sort popular` は再生数キャッシュ（`cache/`）を使ってソートする。

```bash
# 全チャンネルのキャッシュを一括構築（文字起こしなし）
python transcribe.py all --sort popular --cache-only

# 特定チャンネルのキャッシュのみ構築
python transcribe.py channel "メンタリスト DaiGo" --sort popular --cache-only

# キャッシュ構築済み後の通常処理: キャッシュ済みはスキップ → 即ソート開始
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 5 --model tiny

# 取得件数を絞って動作確認（先頭10件だけ再生数取得）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --popular-sample 10 --limit 3 --model tiny
```

### AI要約（手動実行）

```bash
# 特定チャンネルのサマリー更新
python summarize.py "メンタリスト DaiGo"

# 全チャンネル一括
python summarize.py all

# 処理済みを無視して全件再生成
python summarize.py "メンタリスト DaiGo" --force

# 未処理が N 本未満のチャンネルをスキップ
python summarize.py all --threshold 20
```

### 確認

```bash
# 登録チャンネル一覧
python transcribe.py list

# 文字起こしファイル確認
ls "transcripts/メンタリスト DaiGo/"
cat "transcripts/メンタリスト DaiGo/動画タイトル.md"

# インデックス確認（処理済み動画一覧）
cat "transcripts/メンタリスト DaiGo/_index.json" | python -m json.tool | head -30

# サマリー確認
cat "summaries/メンタリスト DaiGo.md"
```

### Mac → WSL クッキー同期

`ssh win` 経由で Mac の Chrome クッキーを WSL に転送する。WSL 側で YouTube ダウンロードに使われる。

```bash
# 明示的に同期（YouTube に再ログイン後など）
python transcribe.py sync-cookies
```

Mac で `transcribe.py` が yt-dlp を実行するタイミングでも自動でバックグラウンド転送される。

## 自動実行（launchd）

| ラベル | スクリプト | スケジュール | 実行内容 |
|---|---|---|---|
| `com.yoshihide.run_yt-learn` | `run_transcribe.sh` | 毎日 0:00 | `transcribe.py all --sort popular --limit 20` |
| `com.yoshihide.run_yt-summarize` | `run_summarize.sh` | 毎日 1:00 | `summarize.py all --threshold 20` |

plist は `~/dotfiles-mac/LaunchAgents/` で管理し、`~/Library/LaunchAgents/` にシンボリックリンクを張る。

### 通知内容

通知アイコンは `~/Applications/Notifiers/yt-learn.app`（[notifier](../notifier) プロジェクトでビルド）。

```
# ネットワーク未接続でスキップしたとき
[yt-learn]
  ネットワーク未接続のためスキップしました

# 要約ファイルを新規作成したとき
[yt-learn]
  メンタリスト DaiGo の要約を作成しました（20件）

# 要約ファイルを更新したとき
[yt-learn]
  メンタリスト DaiGo の要約を更新しました（20件）

# エラーのとき
[yt-learn]
  文字起こしでエラーが発生しました。log/transcribe.log を確認してください

[yt-learn]
  要約でエラーが発生しました。log/summarize.log を確認してください
```

## 要約の仕組み

- 1動画ずつ既存サマリーに「まだない内容のみ」を追加（重複排除）
- どの動画まで処理済みかを `summaries/{チャンネル名}_processed.json` で管理
- APIコストを抑えるため文字起こしと要約を分離
- 未処理が `--threshold` 未満のチャンネルはスキップ（デフォルト: 0 = 常に実行）
