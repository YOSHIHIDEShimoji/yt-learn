# yt-learn

YouTubeチャンネルの動画を自動文字起こしし、Geminiでサマリーを蓄積するツール。

## ディレクトリ構造

```
yt-learn/
├── channels.txt         # 追跡するチャンネルのリスト
├── yt_learn.py          # 文字起こしスクリプト（launchd から run.sh 経由で自動実行）
├── summarize.py         # AI要約スクリプト（手動実行、APIコスト管理のため分離）
├── run.sh               # launchd ラッパー
├── .env                 # 環境変数（GEMINI_API_KEY など）※ git管理外
├── transcripts/         # チャンネル別の文字起こしファイル ※ git管理外
│   └── {チャンネル名}/
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

## 使い方

### チャンネル管理

```bash
# チャンネルを追加
python yt_learn.py add メンタリストDAIGO https://www.youtube.com/@mentalistdaigo

# 登録チャンネル一覧
python yt_learn.py list
```

### 文字起こし

```bash
# チャンネルの全動画を処理（既存はスキップ）
python yt_learn.py channel メンタリストDAIGO

# 最初は --limit で本数を絞って試す
python yt_learn.py channel メンタリストDAIGO --limit 5

# 特定URLを指定（複数可）
python yt_learn.py process https://youtu.be/xxx https://youtu.be/yyy --channel メンタリストDAIGO

# 全チャンネルを処理
python yt_learn.py all
```

### AI要約（手動実行）

```bash
# 指定チャンネルの未要約動画をGeminiで要約
python summarize.py メンタリストDAIGO

# 全チャンネルを一括処理
python summarize.py all

# 処理済みを無視して全件再処理
python summarize.py メンタリストDAIGO --force
```

### 要約の仕組み

- 1動画ずつ既存サマリーに「まだない内容のみ」を追加（重複排除）
- どの動画まで処理済みかを `summaries/{チャンネル名}_processed.json` で管理
- APIコストを抑えるため文字起こしと要約を分離

## 自動実行

launchd で毎日0:00に文字起こしのみ自動実行。要約は手動でタイミングを選んで実行。

```bash
# 要約をlaunchdで自動化したくなったら
launchctl load ~/Library/LaunchAgents/com.yoshihide.run_yt-summarize.plist
```
