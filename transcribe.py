#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""YouTube動画の文字起こし・チャンネル管理ツール（AI要約なし）"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
CACHE_DIR = BASE_DIR / "cache"
CHANNELS_FILE = BASE_DIR / "channels.txt"
COOKIES_FILE = BASE_DIR / "cookies.txt"

WHISPER_MODEL = "large-v3"
WSL_HOST = "win"
WSL_COOKIES_DEST = "/home/wsl-yoshihide/my-projects/yt-learn/cookies.txt"

_cookies_pushed = False

def _push_cookies_to_wsl() -> None:
    global _cookies_pushed
    import sys, subprocess
    if sys.platform != "darwin" or _cookies_pushed or not COOKIES_FILE.exists():
        return
    subprocess.Popen(
        ["scp", str(COOKIES_FILE), f"{WSL_HOST}:{WSL_COOKIES_DEST}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _cookies_pushed = True


def _err(msg: str) -> None:
    from tqdm import tqdm
    tqdm.write(msg, file=sys.stderr)


class _TqdmLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): _err(msg)


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip()[:200]


def _extract_video_id(url: str) -> str:
    """YouTube URLからvideo IDを抽出。非YouTubeはURLをそのまま返す"""
    parsed = urlparse(url)
    if "youtube.com" in parsed.netloc:
        vid = parse_qs(parsed.query).get("v", [None])[0]
        if vid:
            return vid
    elif "youtu.be" in parsed.netloc:
        return parsed.path.lstrip("/")
    return url


# ── 動画インデックス ──────────────────────────────────────────────────────────

def _index_path(channel_name: str) -> Path:
    return TRANSCRIPTS_DIR / _sanitize(channel_name) / "_index.json"


def _load_index(channel_name: str) -> dict:
    p = _index_path(channel_name)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_index(channel_name: str, index: dict) -> None:
    p = _index_path(channel_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _ranking_path(channel_name: str) -> Path:
    return TRANSCRIPTS_DIR / _sanitize(channel_name) / "_ranking.json"


def _update_ranking(channel_name: str, sorted_videos: list) -> None:
    index = _load_index(channel_name)
    cache = _load_view_cache(channel_name)
    ranking = []
    rank = 1
    for v in sorted_videos:
        vid_id = _extract_video_id(v["url"])
        if vid_id not in index:
            continue
        ranking.append({
            "rank": rank,
            "video_id": vid_id,
            "title": index[vid_id]["title"],
            "views": cache.get(vid_id, 0),
            "file": index[vid_id]["file"],
        })
        rank += 1
    p = _ranking_path(channel_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "updated_at": date.today().isoformat(),
        "ranking": ranking,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


# ── channels.txt 操作 ──────────────────────────────────────────────────────────

def _load_channels() -> dict:
    if not CHANNELS_FILE.exists():
        return {}
    channels = {}
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name, url = parts[0], parts[1]
        lang = parts[2] if len(parts) >= 3 and parts[2] else "ja"
        channels[name] = {"url": url, "lang": lang}
    return channels


def _add_channel(name: str, url: str, lang: str = "ja") -> None:
    channels = _load_channels()
    if name in channels:
        _err(f"[skip] {name} は既に登録済み: {channels[name]['url']}")
        return
    with CHANNELS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{name} | {url} | {lang}\n")
    _err(f"[added] {name} | {url} | {lang}")


def _list_channels() -> None:
    channels = _load_channels()
    if not channels:
        _err("チャンネルが登録されていません。python transcribe.py add <name> <url> で追加してください。")
        return
    for name, info in channels.items():
        print(f"{name} | {info['url']} | {info['lang']}")


# ── yt-dlp ヘルパー ────────────────────────────────────────────────────────────

def _cookie_opts() -> dict:
    """Mac: Chromeから読んでcookies.txtに書き出す。それ以外: cookies.txtを使う。"""
    import sys
    opts = {
        "cookiefile": str(COOKIES_FILE),
        "remote_components": ["ejs:github"],
    }
    if sys.platform == "darwin":
        opts["cookiesfrombrowser"] = ("chrome",)
    return opts

def _get_video_title(url: str) -> str:
    import yt_dlp
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestaudio/best",
        "ignore_no_formats_error": True,
        **_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    _push_cookies_to_wsl()
    return (info or {}).get("title", "untitled")


def _normalize_channel_url(channel_url: str) -> str:
    """チャンネルURLを /videos タブに正規化する（タブ指定がない場合）"""
    base = channel_url.rstrip("/")
    if not any(tab in base for tab in ["/videos", "/shorts", "/streams", "/live"]):
        base += "/videos"
    return base


def _get_channel_videos(channel_url: str) -> list:
    import yt_dlp
    url = _normalize_channel_url(channel_url)
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        **_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    videos = []
    for e in info.get("entries", []) or []:
        if not e:
            continue
        vid_id = e.get("id") or ""
        # YouTube video IDは常に11文字。チャンネルIDや他のエントリを除外する
        if len(vid_id) != 11:
            continue
        title = e.get("title") or vid_id
        vid_url = e.get("url") or ""
        if not vid_url.startswith("http"):
            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
        videos.append({"title": title, "url": vid_url})
    return videos


def _fetch_view_count(video_id: str) -> int:
    import yt_dlp
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _TqdmLogger(), **_cookie_opts()}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return info.get("view_count") or 0


def _view_cache_path(channel_name: str) -> Path:
    return CACHE_DIR / f"{_sanitize(channel_name)}_view_cache.json"


def _load_view_cache(channel_name: str) -> dict:
    p = _view_cache_path(channel_name)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_view_cache(channel_name: str, cache: dict) -> None:
    p = _view_cache_path(channel_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _sort_by_popularity(videos: list, channel_name: str, sample_size: int) -> list:
    from tqdm import tqdm
    cache = _load_view_cache(channel_name)
    to_fetch = [v for v in videos if _extract_video_id(v["url"]) not in cache]
    sample = to_fetch if sample_size == 0 else to_fetch[:sample_size]

    if sample:
        _err(f"[popular] {len(sample)} 件の再生数を取得中...")
        for v in tqdm(sample, desc="view count", file=sys.stderr, dynamic_ncols=True):
            vid_id = _extract_video_id(v["url"])
            try:
                cache[vid_id] = _fetch_view_count(vid_id)
            except Exception:
                cache[vid_id] = 0
        _save_view_cache(channel_name, cache)

    def _key(v):
        return cache.get(_extract_video_id(v["url"]), 0)

    return sorted(videos, key=_key, reverse=True)


def _download_audio(url: str, out_dir: str) -> str:
    import yt_dlp
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        **_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    for f in Path(out_dir).iterdir():
        if f.suffix == ".wav":
            return str(f)
    raise RuntimeError(f"WAVファイルが見つかりません: {out_dir}")


# ── 文字起こし ─────────────────────────────────────────────────────────────────

def _transcribe(audio_path: str, lang: str = "ja", model_size: str = WHISPER_MODEL) -> str:
    from faster_whisper import WhisperModel
    from tqdm import tqdm
    _err(f"[model] {model_size} をロード中...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=8)
    _err(f"[transcribe] {Path(audio_path).name}")
    segments_iter, info = model.transcribe(
        audio_path,
        language=lang,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    duration = info.duration or 0.0
    bar_fmt = "{l_bar}{bar}| {n:.0f}/{total:.0f}s [{elapsed}<{remaining}]"
    texts = []
    with tqdm(total=duration, unit="s", bar_format=bar_fmt, file=sys.stderr, dynamic_ncols=True) as pbar:
        for seg in segments_iter:
            if seg.text.strip():
                texts.append(seg.text.strip())
            pbar.update(seg.end - pbar.n)
    return "\n".join(texts)


def _save_transcript(channel_name: str, title: str, url: str, text: str,
                     output_dir: Path = None, model_size: str = WHISPER_MODEL) -> Path:
    out_dir = output_dir if output_dir is not None else TRANSCRIPTS_DIR / _sanitize(channel_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_sanitize(title)}.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_path.write_text(
        f"# {title}\n\nチャンネル: {channel_name}\nURL: {url}\nモデル: {model_size}\n処理日時: {now}\n\n---\n\n{text}\n",
        encoding="utf-8",
    )
    return out_path


# ── 処理エントリポイント ───────────────────────────────────────────────────────

def _process_url(url: str, channel_name: str, lang: str = "ja", title: str = None,
                 output_dir: Path = None, model_size: str = WHISPER_MODEL) -> bool:
    vid_id = _extract_video_id(url)
    index = _load_index(channel_name)

    if vid_id in index:
        _err(f"[skip] 処理済み: {index[vid_id]['title']}")
        return False

    if title is None:
        _err(f"[info] タイトル取得中: {url}")
        title = _get_video_title(url)

    tmpdir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        _err(f"[download] {url}")
        audio_path = _download_audio(url, tmpdir)
        text = _transcribe(audio_path, lang, model_size=model_size)
        saved = _save_transcript(channel_name, title, url, text, output_dir=output_dir, model_size=model_size)

        index[vid_id] = {
            "title": title,
            "url": url,
            "file": str(saved),
            "transcribed_at": date.today().isoformat(),
        }
        _save_index(channel_name, index)
        _err(f"[saved] {saved}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _process_channel(channel_name: str, channel_url: str, lang: str = "ja", limit: int = 0,
                     sort: str = "date", popular_sample: int = 0,
                     model_size: str = WHISPER_MODEL, cache_only: bool = False) -> int:
    _err(f"[channel] {channel_name}: 動画リスト取得中... (sort={sort})")
    videos = _get_channel_videos(channel_url)
    _err(f"[channel] {len(videos)} 件の動画を発見")

    if sort == "popular":
        videos = _sort_by_popularity(videos, channel_name, popular_sample)
        _update_ranking(channel_name, videos)

    if cache_only:
        _err(f"[cache-only] {channel_name}: キャッシュ構築のみ完了\n")
        return 0

    if limit > 0:
        videos = videos[:limit]

    index = _load_index(channel_name)
    processed = 0
    for i, v in enumerate(videos, 1):
        vid_id = _extract_video_id(v["url"])
        if vid_id in index:
            _err(f"[{i}/{len(videos)}] [skip] 処理済み: {v['title']}")
            continue
        _err(f"\n[{i}/{len(videos)}] {v['title']}")
        try:
            if _process_url(v["url"], channel_name, lang, title=v["title"], model_size=model_size):
                processed += 1
                index = _load_index(channel_name)
        except Exception as e:
            _err(f"[error] {v['title']}: {e}")

    if sort == "popular" and processed > 0:
        _update_ranking(channel_name, videos)
    _err(f"[done] {channel_name}: {processed} 件処理\n")
    return processed


def _sync_cookies() -> None:
    import subprocess
    import sys
    if sys.platform != "darwin":
        _err("[error] sync-cookies は Mac からのみ実行できます")
        sys.exit(1)
    _err("[info] Chrome からクッキーを取得中...")
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **_cookie_opts()}) as ydl:
        ydl.extract_info("https://www.youtube.com/", download=False)
    _err(f"[info] {WSL_HOST} に転送中...")
    wsl_cmd = f"wsl -- bash -c 'mkdir -p {Path(WSL_COOKIES_DEST).parent} && cat > {WSL_COOKIES_DEST}'"
    with open(COOKIES_FILE, "rb") as f:
        result = subprocess.run(["ssh", WSL_HOST, wsl_cmd], stdin=f, capture_output=True)
    if result.returncode != 0:
        _err(f"[error] 転送失敗:\n{result.stderr.decode()}")
        sys.exit(1)
    _err(f"[done] cookies.txt を {WSL_HOST}:{WSL_COOKIES_DEST} に送信しました")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="YouTube動画の文字起こし・チャンネル管理ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python transcribe.py add メンタリストDAIGO https://www.youtube.com/@mentalistdaigo
  python transcribe.py list
  python transcribe.py process https://youtu.be/xxx https://youtu.be/yyy --channel メンタリストDAIGO
  python transcribe.py channel メンタリストDAIGO
  python transcribe.py channel メンタリストDAIGO --limit 100 --sort popular
  python transcribe.py channel メンタリストDAIGO --limit 100 --sort popular  # 2回目は101-200本目を処理
  python transcribe.py all --sort popular --limit 50

AI要約は別スクリプト:
  python summarize.py メンタリストDAIGO
  python summarize.py all
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="チャンネルを channels.txt に追加")
    p_add.add_argument("name", help="チャンネル名（ディレクトリ名になる）")
    p_add.add_argument("url", help="チャンネルURL")
    p_add.add_argument("lang", nargs="?", default="ja", help="文字起こし言語 (default: ja)")

    sub.add_parser("list", help="登録チャンネル一覧を表示")

    p_proc = sub.add_parser("process", help="特定URLを文字起こし（複数可）")
    p_proc.add_argument("urls", nargs="*", help="動画URL（複数可、省略時は --file が必須）")
    p_proc.add_argument("--channel", default="misc", help="チャンネル名（省略時は misc）")
    p_proc.add_argument("--lang", default="ja")
    p_proc.add_argument("--model", default=WHISPER_MODEL,
                        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
                        help=f"Whisperモデル (default: {WHISPER_MODEL})")
    p_proc.add_argument("-f", "--file", help="URLを1行1件で記述したテキストファイル（#はコメント）")
    p_proc.add_argument("-o", "--output", help="出力ディレクトリ（省略時は transcripts/{channel}/）")

    p_ch = sub.add_parser("channel", help="チャンネルの全動画を処理")
    p_ch.add_argument("name", help="channels.txt のチャンネル名")
    p_ch.add_argument("--model", default=WHISPER_MODEL,
                      choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"])
    p_ch.add_argument("--limit", type=int, default=0, help="最大処理動画数（0=全件）")
    p_ch.add_argument("--sort", choices=["date", "popular"], default="date",
                      help="取得順序: date=新着順(default), popular=人気順")
    p_ch.add_argument("--popular-sample", type=int, default=0,
                      help="人気順ソート時に再生数を取得する動画数（0=上限なし、default: 0）")
    p_ch.add_argument("--cache-only", action="store_true",
                      help="再生数キャッシュの構築のみ行い、文字起こしはしない（--sort popular と併用）")

    p_all = sub.add_parser("all", help="全チャンネルを処理")
    p_all.add_argument("--model", default=WHISPER_MODEL,
                       choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"])
    p_all.add_argument("--limit", type=int, default=0)
    p_all.add_argument("--sort", choices=["date", "popular"], default="date")
    p_all.add_argument("--popular-sample", type=int, default=0)
    p_all.add_argument("--cache-only", action="store_true")

    sub.add_parser("sync-cookies", help="Mac の Chrome クッキーを WSL に転送")

    args = parser.parse_args()

    if args.cmd == "add":
        _add_channel(args.name, args.url, args.lang)

    elif args.cmd == "list":
        _list_channels()

    elif args.cmd == "process":
        urls = list(args.urls)
        if args.file:
            try:
                for line in Path(args.file).read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
            except FileNotFoundError:
                _err(f"[error] ファイルが見つかりません: {args.file}")
                sys.exit(1)
        if not urls:
            _err("[error] URLを引数で渡すか、--file でテキストファイルを指定してください")
            sys.exit(1)
        output_dir = Path(args.output) if args.output else None
        for i, url in enumerate(urls):
            if i > 0:
                _err("")
            _process_url(url, args.channel, args.lang, output_dir=output_dir, model_size=args.model)

    elif args.cmd == "channel":
        channels = _load_channels()
        if args.name not in channels:
            _err(f"[error] '{args.name}' が channels.txt に見つかりません")
            sys.exit(1)
        info = channels[args.name]
        _process_channel(args.name, info["url"], info["lang"], args.limit, args.sort,
                         args.popular_sample, args.model, args.cache_only)

    elif args.cmd == "sync-cookies":
        _sync_cookies()

    elif args.cmd == "all":
        channels = _load_channels()
        if not channels:
            _err("[warn] channels.txt にチャンネルが登録されていません")
            sys.exit(0)
        for name, info in channels.items():
            _process_channel(name, info["url"], info["lang"], args.limit, args.sort, args.popular_sample, args.model, args.cache_only)


if __name__ == "__main__":
    main()
