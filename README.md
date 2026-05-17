# yt-learn

YouTubeチャンネルの動画を自動文字起こしし、Geminiでサマリーを蓄積するツール。

## ディレクトリ構造

```
yt-learn/
├── channels.txt         # 追跡するチャンネルのリスト
├── transcribe.py        # 文字起こしスクリプト
├── summarize.py         # AI要約スクリプト（手動実行 or run_summarize.sh 経由）
├── run_transcribe.sh    # 文字起こし自動実行ラッパー（launchd 用）
├── run_summarize.sh     # 要約自動実行ラッパー（launchd 用）
├── youtube.png          # 通知アイコン
├── .env                 # 環境変数（GEMINI_API_KEY など）※ git管理外
├── cookies.txt          # YouTube クッキー（yt-dlp が書き出し）※ git管理外
├── cache/               # 再生数キャッシュ（チャンネル別）
├── log/                 # 実行ログ（transcribe_YYYYMMDD.log, summarize.log）
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
# .env を作成してAPIキーを設定
echo "GEMINI_API_KEY=your_key_here" > .env

# ローカルLLM（Ollama）を使う場合は追加で設定
# Mac: TailscaleでWindowsのOllamaに直接接続
echo "LOCAL_LLM_URL=http://<Windows-TailscaleIP>:11434" >> .env
# WSL: localhostでWindowsのOllamaに接続
echo "LOCAL_LLM_URL=http://localhost:11434" >> .env
echo "LOCAL_LLM_MODEL=qwen3.5:9b" >> .env
```

### ローカルLLM（Ollama）の利用

Gemini APIのレート制限を回避するため、Windows上のOllamaをローカルLLMとして使える。`LOCAL_LLM_URL` が設定されていればOllama優先、失敗時はGeminiにフォールバックする。

**Macから実行する場合**

WindowsのOllamaをTailscale経由で直接参照する。SSHトンネル不要。

```bash
# .env
LOCAL_LLM_URL=http://<Windows-TailscaleIP>:11434
```

**WSLから実行する場合**

WindowsのOllamaにlocalhost経由で接続できる。トンネル不要。

```bash
# .env
LOCAL_LLM_URL=http://localhost:11434
```

### 処理フロー

| | Mac | WSL |
|---|---|---|
| **文字起こし** | whisper.cpp（Metal GPU） | faster-whisper（CUDA） |
| **要約** | Ollama → Gemini フォールバック | Ollama → Gemini フォールバック |

```
URL入力
  └─ yt-dlp でダウンロード（m4a）
       └─ 文字起こし
            ├─ Mac:  whisper.cpp (whisper-cli / Metal)
            └─ WSL:  faster-whisper (CUDA / CPU)
       └─ .md として保存
       └─ ポイント生成
            ├─ Ollama（LOCAL_LLM_URLが設定済みの場合）
            └─ Gemini（Ollama失敗 or 未設定）
```

### Google Drive 同期（rclone）

`transcripts/` と `summaries/` を Google Drive に同期するために rclone を使う。

```bash
# Mac
brew install rclone

# WSL
sudo apt install rclone
```

初回のみ Google Drive のリモートを設定する：

```bash
rclone config
```

対話形式で以下のように進める：

```
n) New remote → n
name> gdrive
Storage> Google Drive の番号を入力
client_id>            （空のままEnter）
client_secret>        （空のままEnter）
scope> 1              （Full access all files）
root_folder_id>       （空のままEnter）
service_account_file> （空のままEnter）
Edit advanced config? → n
Use auto config? → y  （Mac: ブラウザが開く / WSL: n を選んで表示されたURLをMacで開く）
Configure as Shared Drive? → n
y) Yes this is OK → y
```

接続確認：

```bash
rclone lsd gdrive:
# マイドライブ直下のフォルダ一覧が表示されればOK
```

### 使い方

同期先は Google Drive マイドライブ直下の `yt-learn/` フォルダ。

```bash
# transcripts/ と summaries/ を両方同期
python transcribe.py sync

# transcripts/ だけ同期
python transcribe.py sync --only transcripts

# summaries/ だけ同期
python transcribe.py sync --only summaries
```

Mac・WSL どちらから実行しても同じ Drive フォルダに集約される。

## 使い方

### チャンネル管理

```bash
# チャンネル追加（言語省略時は ja）
python transcribe.py add "メンタリスト DaiGo" https://www.youtube.com/@mentalistdaigo
python transcribe.py add 3Blue1Brown https://www.youtube.com/@3blue1brown en

# 登録チャンネル一覧
python transcribe.py list
```

### 単発処理（特定URLを文字起こし）

```bash
# 単発URL（--model で軽量モデルを指定して高速化）
python transcribe.py process https://youtu.be/xxx --model tiny

# チャンネル指定あり → transcripts/メンタリスト DaiGo/ に保存
python transcribe.py process https://youtu.be/xxx --channel "メンタリスト DaiGo" --model tiny

# 複数URL同時
python transcribe.py process https://youtu.be/aaa https://youtu.be/bbb --channel "メンタリスト DaiGo" --model small

# URLファイルから読み込み（URL | en で言語指定可、# はコメント）
python transcribe.py process -f urls.txt --channel ひろゆき

# 出力先を完全に指定
python transcribe.py process https://youtu.be/xxx -o ~/Desktop/output --model tiny
```

`--model` の選択肢: `tiny` / `base` / `small` / `medium` / `large` / `large-v2` / `large-v3` / `large-v3-turbo`（default: large-v3）

### 処理速度の目安

43分（2589秒）の動画で実測した結果（RTX 5060 Ti / CUDA）：

| モデル | 処理時間 | 倍速 | 品質 |
|---|---|---|---|
| large-v3 | 約91秒 | 約28倍速 | 句読点あり・高精度（**推奨**） |
| large-v3-turbo | 約76秒 | 約34倍速 | 句読点なし・やや劣る |

速度差は約20%だが、large-v3 は句読点・読点が正確で可読性が大きく上回るため large-v3 をデフォルトとしている。

### チャンネル全取得

```bash
# 人気順で上位5本（動作確認用）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 5 --model tiny

# 人気順で上位100本
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 100

# 2回目は自動で未処理の次の100件が対象になる
python transcribe.py channel "メンタリスト DaiGo" --sort popular --limit 100

# 再生数取得を先頭50件に絞ってソート（大規模チャンネルの高速化）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --popular-sample 50 --limit 10 --model tiny

# 再生数キャッシュのみ構築（文字起こしなし）
python transcribe.py channel "メンタリスト DaiGo" --sort popular --cache-only

# 全チャンネルを人気順20本ずつ
python transcribe.py all --sort popular --limit 20

# 全チャンネルのキャッシュのみ一括構築
python transcribe.py all --sort popular --cache-only
```

### AI要約

```bash
# 特定チャンネル（未処理20本未満はスキップ）
python summarize.py "メンタリスト DaiGo" --threshold 20

# 全チャンネル一括
python summarize.py all --threshold 20

# 処理済みを無視して全件再生成
python summarize.py "メンタリスト DaiGo" --force
```

### Google Drive 同期

```bash
# transcripts/ と summaries/ を両方同期
python transcribe.py sync

# どちらか一方だけ
python transcribe.py sync --only transcripts
python transcribe.py sync --only summaries
```

文字起こし完了ごとに該当ファイルが自動で Drive に転送される。末尾の `sync` は取りこぼし補完用。

### Mac → WSL クッキー同期

WSL はブラウザにアクセスできないため、Mac の Chrome クッキーを手動で同期する必要がある。

```bash
# Mac で実行（Chrome からクッキーを取得して WSL に転送）
python transcribe.py sync-cookies
```

WSL 側でクッキー切れ（`Sign in to confirm you're not a bot`）が出たら Mac でこのコマンドを実行する。

### ログ確認

実行のたびに `log/transcribe_YYYYMMDD.log` にリアルタイムで書き出される（日次ローテーション）。

```bash
# 当日のログを確認
cat log/transcribe_$(date +%Y%m%d).log

# エラーのみ抽出
grep '\[error\]' log/transcribe_$(date +%Y%m%d).log
```

WSL 側のログは `/home/wsl-yoshihide/my-projects/yt-learn/log/` に出力される。

### キャッシュ確認

```bash
# 再生数0のエントリ（エラー由来の可能性あり）を確認
./check_cache.sh
```

### メンバー限定動画

人気順ソート（`--sort popular`）で再生数取得時にメンバー限定動画に当たると、`cache/*_view_cache.json` に `-1` を sentinel として保存する。次回以降は再取得せず、ソートでも最下位に回るため `--limit N` の上位 N 件には含まれない。`[error]` / `ERROR:` のログにも出ない。

## WSL での継続実行

Windows の WSL で回し続ける場合のコマンド。キャッシュ取得・ソート・文字起こし・即時 Drive 転送・要約・最終同期まで一括で行う。

```bash
while true; do
    python transcribe.py all --sort popular --limit 20
    python summarize.py all --threshold 20
    python transcribe.py sync
done
```

- `transcribe.py all --sort popular` の中で再生数キャッシュ取得・ランキング更新・文字起こし・Drive への即時転送が順に行われる
- 文字起こし完了ごとに `transcripts/` の該当ファイルが Drive に転送される（途中終了しても済んだ分は保持される）
- 末尾の `transcribe.py sync` で `summaries/` と取りこぼした `transcripts/` を補完同期する

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
  文字起こしでエラーが発生しました。log/ を確認してください

[yt-learn]
  要約でエラーが発生しました。log/ を確認してください
```

## 要約の仕組み

- 1動画ずつ既存サマリーに「まだない内容のみ」を追加（重複排除）
- どの動画まで処理済みかを `summaries/{チャンネル名}_processed.json` で管理
- APIコストを抑えるため文字起こしと要約を分離
- 未処理が `--threshold` 未満のチャンネルはスキップ（デフォルト: 0 = 常に実行）
