# yt-learn ポータル 使い方ガイド

## プロジェクト概要

yt-learn は channels.txt に登録した YouTube チャンネルの動画を、人気順に自動で音声ダウンロード → Whisper 文字起こし → LLM 要約 → Google Drive 保存するパイプライン。WSL（CUDA GPU）で常時稼働させることを想定している。

---

## HOME タブ

### 登録チャンネル
- channels.txt に登録されたチャンネルの一覧を表示
- 各チャンネルには YouTube チャンネルへのリンクがある
- `+ 追加` ボタン: チャンネル名・YouTube URL・言語（ja/en）を入力して追加。次の autonomous.sh の周回から処理対象になる
- `×` ボタン: チャンネルを削除（channels.txt から除去）
- `↻ 更新` ボタン: 一覧を再読み込み

### クイック実行（autonomous.sh）
- **WSL 専用機能**
- `▶ 起動` ボタン: `autonomous.sh` を tmux セッションで起動する
- autonomous.sh の動作:
  - DL ワーカー（バックグラウンド）: 全チャンネルを人気順に limit=10 件ずつダウンロードし `queue/` に蓄積
  - 文字起こしワーカー（フォアグラウンド）: `queue/` の音声を Whisper large-v3 で常時処理
  - キューが 200 件を超えると DL を一時停止し、100 件未満になったら再開（バックプレッシャー制御）
  - rate-limit を検知すると DL を自動停止→復帰チェック→自動再開
  - 全チャンネルを一周するたびに Summarize All を自動実行
- `■ 停止` ボタン: Ctrl+C を送って安全停止（`[session-end]` をログに記録）
- `ログを見る →` ボタン: 現在の autonomous.sh ログを LOGS タブで確認（起動中のみ表示）

### URL 処理
- YouTube の URL を1行1件で複数入力し、個別に文字起こしを処理する
- 保存先チャンネル名: 登録チャンネルから選ぶか `misc` を指定
- 言語: `ja`（日本語）/ `en`（英語）を選択
- 送信すると バックグラウンドで処理が始まり、STATUS タブでリアルタイム確認できる

### Summarize All
- `transcripts/` 以下の未要約トランスクリプトを LLM でまとめて要約し Google Drive に保存
- Ollama（ローカル）または Gemini（クラウド）で処理
- 通常は autonomous.sh が自動実行するため、手動実行は任意のタイミングで強制実行したい場合に使う

### Drive Sync
- 要約済みファイルを Google Drive に同期する
- `transcribe.py sync` を内部で実行

---

## STATUS タブ

- 実行中のプロセスをリアルタイム表示（SSE で自動更新）
- 複数プロセスが同時起動している場合はタブで切り替え
- **統計パネル**:
  - `queue`: 文字起こし待ち音声ファイル数（クリックで一覧表示、idle 時は不可）
  - `done`: 処理済み動画数（クリックでログ内の該当行を表示）
  - `warn`: 警告数
  - `error`: エラー数
  - `rate-limit`: YouTube rate-limit 検知回数
- 経過時間はプロセス開始時刻から自動計算・1分ごと更新
- 「ログを見る →」ボタンで LOGS タブに直接遷移

---

## LOGS タブ

- ログファイルの一覧を表示（autonomous・process・summarize 各種）
- **Live** バッジ付きログ: クリックすると SSE でリアルタイム追従
- **Done** バッジ: セッション終了済み
- ログビューアー内でキーワード検索可能

---

## LIBRARY タブ

- `transcripts/` に蓄積された Markdown 文字起こしファイルを全文検索
- チャンネル絞り込み・キーワード検索・ページネーションに対応
- ヒットしたカードをクリックで全文ビューア表示
- カードを選択（チェックボックス）して右パネルの AI に質問できる
  - ファイル未選択の場合はライブラリ全体を対象に検索・回答
  - AI モデル: Ollama（ローカル）または Gemini（クラウド）を選択可

---

## 主要スクリプト（CLI）

| スクリプト | 用途 |
|---|---|
| `./autonomous.sh` | DL + 文字起こし + 要約の全自動ループ（推奨） |
| `python transcribe.py channel "名前"` | 個別チャンネルの処理 |
| `python transcribe.py drain-queue` | queue/ の音声を一括処理 |
| `python transcribe.py sync` | Google Drive 同期 |
| `python transcribe.py refresh-cookies` | Firefox cookies を更新 |
| `python summarize.py all` | 全チャンネル一括要約 |

---

## よくある質問

**Q: クイック実行とは何ですか？**  
autonomous.sh を起動するボタンです。DL・文字起こし・要約を全自動で行います。WSL 環境でのみ使用できます。

**Q: Summarize All とは？**  
文字起こし済みの動画を AI で要約し、Google Drive に保存します。autonomous.sh が一周ごとに自動実行しますが、手動でも実行できます。

**Q: チャンネルを追加するには？**  
HOME タブの「+ 追加」ボタンからチャンネル名・URL・言語を入力します。次の autonomous.sh の周回から対象になります。

**Q: ログを見たい**  
LOGS タブ、または STATUS タブの「ログを見る →」ボタンから確認できます。autonomous.sh 起動中はクイック実行パネルにも「ログを見る →」が表示されます。

**Q: 文字起こしが止まった / エラーが出た**  
STATUS タブで `error` 統計をクリックするとエラー行を確認できます。GPU メモリ不足・rate-limit・ネットワークエラーなどが主な原因です。

**Q: rate-limit とは？**  
YouTube が短期間に大量のリクエストを検知してアクセスを制限すること。autonomous.sh は自動で検知し、一定時間後に再試行します。

**Q: queue とは？**  
ダウンロード済みで文字起こし待ちの音声ファイルの置き場（`queue/` ディレクトリ）。Whisper が順次処理します。

**Q: cookies.txt とは？**  
YouTube のログイン情報を含むクッキーファイル。`transcribe.py refresh-cookies` で Firefox から自動更新できます。autonomous.sh 起動時に自動実行されます。
