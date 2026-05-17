# yt-learn 設計メモ

## やりたいこと

channels.txt に登録したYouTubeチャンネルの動画を、**人気順で上位から**自動的に文字起こし・要約してGoogle Driveに蓄積する。

Mac（Metal GPU）とWSL（CUDA GPU）の両方で同じコマンドが動く構成。

```
python transcribe.py all --sort popular --limit 100
```

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

---

## cache/ をgit管理している理由

`cache/*.json`（チャンネル別の再生数キャッシュ）はgit管理対象。

**理由：再生数の取得コストが高いから。**

- YouTubeは1動画あたり数秒かかり、レートリミットもある
- 一度取得した再生数は半永久的に有効（人気順ランキングの判断に使う）
- Mac↔WSL の両環境で共有することで、片方で取得した再生数をもう片方でも使い回せる
- git管理することでpushすれば両環境に自動同期できる

---

## --sort popular の動作フロー

```
1. チャンネルの全動画リストを取得（yt-dlp extract_flat）
2. キャッシュ済みでない動画の再生数を取得（_sort_by_popularity）
3. 再生数降順にソート
4. 未処理の動画のうち上位 --limit 件を処理
```

### なぜ毎回「N件の再生数を取得中」が出るのか

`to_fetch = [v for v in videos if video_id not in cache]`

**キャッシュにない動画が残っていれば、毎回全部取得しようとする。**

両学長（2554動画）でキャッシュが514件しかない場合、残り2040件を毎回取得しようとして詰まる。

### popular_sample の役割

```python
sample = to_fetch if sample_size == 0 else to_fetch[:sample_size]
```

- `popular_sample=0`（旧デフォルト）→ 未キャッシュを**全件**取得
- `popular_sample=200`（現デフォルト）→ 未キャッシュのうち**最大200件**だけ取得

チャンネルに動画が何千本あっても、上位200件の再生数が分かれば人気順の判断には十分。

---

## 現在の問題点

### 1. ~~YouTube bot検知（解決済み）~~
- **症状**：`Sign in to confirm you're not a bot`
- **原因A**：古い`cookies.txt`がChrome由来の新しいクッキーに干渉（Mac）
- **原因B**：`player_client`未指定でbot検知されやすいクライアントが使われていた
- **対処**：Mac初回起動時に`cookies.txt`削除、`player_client=web`（deno使用）に統一

### 2. ~~WSLでyt-dlp動かない（解決済み）~~
- **症状**：`Requested format is not available`
- **原因**：denoがWSLに未インストール → `web`クライアントが使えなかった
- **対処**：denoをWSLにインストール、`run_transcribe.sh`にPATH追加

### 3. ~~view_cache保存タイミング（解決済み）~~
- **症状**：途中でkillするとキャッシュが全消え → 毎回同じ件数を取得し直す
- **原因**：ループ終了後のみ`_save_view_cache`を呼んでいた
- **対処**：10件ごとに逐次保存

### 4. メンバーシップ限定動画（対処不要）
- **症状**：`This video is available to this channel's members on level: ...`
- 購読なしでは取得不可。ERRORログは出るが処理続行するため実害なし。

---

## 運用フロー

### 通常実行（Mac）

```bash
python transcribe.py all --sort popular --limit 100
```

### 通常実行（WSL）

```bash
bash run_transcribe.sh   # launchd から呼ばれるか手動で実行
```

### クッキー同期（Mac→WSL）

```bash
python transcribe.py sync-cookies
```

Mac のChromeクッキーをWSLの`cookies.txt`に転送する。WSLはyt-dlpがchromeブラウザに直接アクセスできないため。

### キャッシュのみ構築（新チャンネル追加時）

```bash
python transcribe.py channel "チャンネル名" --sort popular --cache-only
```

---

## cookies.txt の扱い

| 環境 | 取得方法 | 保存先 |
|------|----------|--------|
| Mac | `cookiesfrombrowser=chrome`（実行時にChromeから直接読む） | `cookies.txt`（次回WSL同期用） |
| WSL | `cookiefile=cookies.txt`（Macからscpで転送されたファイル） | そのまま使用 |

`cookies.txt`はgit管理外（`.gitignore`）。sync-cookiesコマンドで手動同期。

Mac側は実行開始時に古い`cookies.txt`を削除してから新しいものを生成する（古いファイルの混在によるbot検知を防ぐため）。
