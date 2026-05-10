#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""YouTube動画の文字起こし・チャンネル管理ツール（AI要約なし）"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
CHANNELS_FILE = BASE_DIR / "channels.txt"

WHISPER_MODEL = "large-v3"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


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
        name, url = line.split("|", 1)
        channels[name.strip()] = url.strip()
    return channels


def _add_channel(name: str, url: str) -> None:
    channels = _load_channels()
    if name in channels:
        _err(f"[skip] {name} は既に登録済み: {channels[name]}")
        return
    with CHANNELS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{name} | {url}\n")
    _err(f"[added] {name} | {url}")


def _list_channels() -> None:
    channels = _load_channels()
    if not channels:
        _err("チャンネルが登録されていません。python yt_learn.py add <name> <url> で追加してください。")
        return
    for name, url in channels.items():
        print(f"{name} | {url}")


# ── yt-dlp ヘルパー ────────────────────────────────────────────────────────────

def _apply_sort(channel_url: str, sort: str) -> str:
    """チャンネルURLにソートパラメータを付与する"""
    if sort != "popular":
        return channel_url
    base = channel_url.rstrip("/")
    # /videos タブが含まれていなければ追加
    if "/videos" not in base:
        base += "/videos"
    # すでに?sort=pがついている場合はそのまま
    if "sort=p" not in base:
        sep = "&" if "?" in base else "?"
        base += f"{sep}sort=p"
    return base


def _get_video_title(url: str) -> str:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return (info or {}).get("title", "untitled")


def _get_channel_videos(channel_url: str) -> list:
    import yt_dlp
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False) or {}

    videos = []
    for e in info.get("entries", []) or []:
        if not e:
            continue
        vid_id = e.get("id") or ""
        title = e.get("title") or vid_id
        url = e.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "")
        if not url.startswith("http") and vid_id:
            url = f"https://www.youtube.com/watch?v={vid_id}"
        if url:
            videos.append({"title": title, "url": url})
    return videos


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
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    for f in Path(out_dir).iterdir():
        if f.suffix == ".wav":
            return str(f)
    raise RuntimeError(f"WAVファイルが見つかりません: {out_dir}")


# ── 文字起こし ─────────────────────────────────────────────────────────────────

def _transcribe(audio_path: str, lang: str = "ja") -> str:
    from faster_whisper import WhisperModel
    _err(f"[model] {WHISPER_MODEL} をロード中...")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=8)
    _err(f"[transcribe] {Path(audio_path).name}")
    segments_iter, _ = model.transcribe(
        audio_path,
        language=lang,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    return "\n".join(seg.text.strip() for seg in segments_iter if seg.text.strip())


def _save_transcript(channel_name: str, title: str, url: str, text: str) -> Path:
    out_dir = TRANSCRIPTS_DIR / _sanitize(channel_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_sanitize(title)}.md"
    out_path.write_text(
        f"# {title}\n\nチャンネル: {channel_name}\nURL: {url}\n\n---\n\n{text}\n",
        encoding="utf-8",
    )
    return out_path


# ── 処理エントリポイント ───────────────────────────────────────────────────────

def _process_url(url: str, channel_name: str, lang: str = "ja", title: str = None) -> bool:
    vid_id = _extract_video_id(url)
    index = _load_index(channel_name)

    if vid_id in index:
        _err(f"[skip] 処理済み: {index[vid_id]['title']}")
        return False

    if title is None:
        _err(f"[info] タイトル取得中: {url}")
        title = _get_video_title(url)

    tmpdir = tempfile.mkdtemp(prefix="yt_learn_")
    try:
        _err(f"[download] {url}")
        audio_path = _download_audio(url, tmpdir)
        text = _transcribe(audio_path, lang)
        saved = _save_transcript(channel_name, title, url, text)

        index[vid_id] = {
            "title": title,
            "url": url,
            "file": saved.name,
            "transcribed_at": date.today().isoformat(),
        }
        _save_index(channel_name, index)
        _err(f"[saved] {saved}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _process_channel(channel_name: str, channel_url: str, lang: str = "ja", limit: int = 0, sort: str = "date") -> int:
    sorted_url = _apply_sort(channel_url, sort)
    _err(f"[channel] {channel_name}: 動画リスト取得中... (sort={sort})")
    videos = _get_channel_videos(sorted_url)
    _err(f"[channel] {len(videos)} 件の動画を発見")

    if limit > 0:
        videos = videos[:limit]

    index = _load_index(channel_name)
    processed = 0
    for i, v in enumerate(videos, 1):
        vid_id = _extract_video_id(v["url"])
        if vid_id in index:
            _err(f"[{i}/{len(videos)}] [skip] {v['title']}")
            continue
        _err(f"[{i}/{len(videos)}] {v['title']}")
        try:
            if _process_url(v["url"], channel_name, lang, title=v["title"]):
                processed += 1
                index = _load_index(channel_name)
        except Exception as e:
            _err(f"[error] {v['title']}: {e}")

    _err(f"[done] {channel_name}: {processed} 件処理")
    return processed


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="YouTube動画の文字起こし・チャンネル管理ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python yt_learn.py add メンタリストDAIGO https://www.youtube.com/@mentalistdaigo
  python yt_learn.py list
  python yt_learn.py process https://youtu.be/xxx https://youtu.be/yyy --channel メンタリストDAIGO
  python yt_learn.py channel メンタリストDAIGO
  python yt_learn.py channel メンタリストDAIGO --limit 100 --sort popular
  python yt_learn.py channel メンタリストDAIGO --limit 100 --sort popular  # 2回目は101-200本目を処理
  python yt_learn.py all --sort popular --limit 50

AI要約は別スクリプト:
  python summarize.py メンタリストDAIGO
  python summarize.py all
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="チャンネルを channels.txt に追加")
    p_add.add_argument("name", help="チャンネル名（ディレクトリ名になる）")
    p_add.add_argument("url", help="チャンネルURL")

    sub.add_parser("list", help="登録チャンネル一覧を表示")

    p_proc = sub.add_parser("process", help="特定URLを文字起こし（複数可）")
    p_proc.add_argument("urls", nargs="+", help="動画URL")
    p_proc.add_argument("--channel", required=True, help="チャンネル名")
    p_proc.add_argument("--lang", default="ja")

    p_ch = sub.add_parser("channel", help="チャンネルの全動画を処理")
    p_ch.add_argument("name", help="channels.txt のチャンネル名")
    p_ch.add_argument("--lang", default="ja")
    p_ch.add_argument("--limit", type=int, default=0, help="最大処理動画数（0=全件）")
    p_ch.add_argument("--sort", choices=["date", "popular"], default="date",
                      help="取得順序: date=新着順(default), popular=人気順")

    p_all = sub.add_parser("all", help="全チャンネルを処理")
    p_all.add_argument("--lang", default="ja")
    p_all.add_argument("--limit", type=int, default=0)
    p_all.add_argument("--sort", choices=["date", "popular"], default="date")

    args = parser.parse_args()

    if args.cmd == "add":
        _add_channel(args.name, args.url)

    elif args.cmd == "list":
        _list_channels()

    elif args.cmd == "process":
        for url in args.urls:
            _process_url(url, args.channel, args.lang)

    elif args.cmd == "channel":
        channels = _load_channels()
        if args.name not in channels:
            _err(f"[error] '{args.name}' が channels.txt に見つかりません")
            sys.exit(1)
        _process_channel(args.name, channels[args.name], args.lang, args.limit, args.sort)

    elif args.cmd == "all":
        channels = _load_channels()
        if not channels:
            _err("[warn] channels.txt にチャンネルが登録されていません")
            sys.exit(0)
        for name, url in channels.items():
            _process_channel(name, url, args.lang, args.limit, args.sort)


if __name__ == "__main__":
    main()
