#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""YouTube動画の文字起こし・チャンネル管理ツール（AI要約なし）"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent.parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
CACHE_DIR = BASE_DIR / "cache"
QUEUE_DIR = BASE_DIR / "queue"
DELIVER_DIR = BASE_DIR / "deliver"
CHANNELS_FILE = BASE_DIR / "channels.txt"
COOKIES_FILE = BASE_DIR / "cookies.txt"

WHISPER_MODEL = "large-v3"
WHISPER_CLI = Path.home() / "my-projects/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODELS_DIR = Path.home() / "my-projects/whisper.cpp/models"
OLLAMA_GENERATE_PATH = "/api/generate"
RCLONE_REMOTE = "gdrive"
RCLONE_DEST = f"{RCLONE_REMOTE}:yt-learn"

_log_file = None

# WSL: deno は ~/.deno/bin にあるが run_transcribe.sh 経由以外は PATH に入らない
# yt-dlp の web クライアントが n-challenge 解決に deno を使うため起動時に追加
_deno_bin = str(Path.home() / ".deno" / "bin")
if Path(_deno_bin).is_dir() and _deno_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _deno_bin + os.pathsep + os.environ.get("PATH", "")


def _setup_log() -> None:
    import atexit
    global _log_file
    log_dir = BASE_DIR / "logs" / "transcribe"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"transcribe_{date.today().strftime('%Y%m%d')}.log"
    _log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    atexit.register(_teardown_log)
    _log_write(f"=== 開始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {'=' * 30}")


def _teardown_log() -> None:
    global _log_file
    if _log_file:
        _log_write(f"[session-end] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        _log_file.close()
        _log_file = None


def _log_write(msg: str) -> None:
    if _log_file:
        print(msg, file=_log_file)



def _err(msg: str) -> None:
    from tqdm import tqdm
    tqdm.write(msg, file=sys.stderr)
    _log_write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


_MEMBERS_ERR_MARKERS = ("members-only", "members on level", "Join this channel")
# yt-dlp が ERROR: として出力するが _process_channel 側で [warn] 扱いにするメッセージ
_SUPPRESSED_ERR_MARKERS = (
    *_MEMBERS_ERR_MARKERS,
    "confirm your age",   # 年齢制限動画（cookies不足で恒久的に失敗）
    "age-restricted",
    "rate-limited",       # レートリミット（_process_channel で break する）
    "Sign in to confirm you’re not a bot",  # U+2019（yt-dlpが使う右シングルクォート）
    "Sign in to confirm you're not a bot",   # ASCII apostrophe フォールバック
    "not a bot",                              # 両方をカバーする短縮マーカー
    "Got error",          # ネットワーク切断（一時的）
    "Read timed out",     # ソケットタイムアウト（一時的）
    "Connection reset",   # 接続リセット（一時的）
    "HTTP Error 403",     # アクセス禁止（geo制限・非公開等、恒久的に失敗）
)


def _is_members_only_error(msg: str) -> bool:
    return any(marker in msg for marker in _MEMBERS_ERR_MARKERS)


def _is_suppressed_error(msg: str) -> bool:
    return any(m in msg for m in _SUPPRESSED_ERR_MARKERS)


class _TqdmLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        if _is_suppressed_error(msg):
            return
        _err(msg)


class _FilteredStderr:
    """yt-dlp が logger を経由せず直接 stderr に書く ERROR: 行を抑制するフィルター。

    buffer 属性を意図的に隠す: yt-dlp の write_string が hasattr(out, 'buffer') を
    チェックして buffer に直接書こうとするのを防ぎ、必ず write() 経由にする。
    """
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name == 'buffer':
            raise AttributeError('buffer')
        return getattr(self._real, name)

    def write(self, data: str):
        if _is_suppressed_error(data):
            return len(data)
        return self._real.write(data)

    def flush(self):
        self._real.flush()


def _install_stderr_pipe_filter() -> None:
    """fd 2 (stderr) をパイプ経由にして _is_suppressed_error 行を OS レベルで除去する。

    yt-dlp が Python の sys.stderr を迂回して fd 2 に直接書き込む場合もフィルタできる。
    バックグラウンドスレッドがパイプを読んでフィルタし、元の stderr に転送する。
    """
    import threading

    read_fd, write_fd = os.pipe()
    original_fd2 = os.dup(2)
    os.dup2(write_fd, 2)
    os.close(write_fd)

    original_out = os.fdopen(original_fd2, 'w', buffering=1, errors='replace')
    sys.stderr = os.fdopen(2, 'w', buffering=1, errors='replace')

    def _filter_thread():
        with os.fdopen(read_fd, 'r', buffering=1, errors='replace') as pipe_in:
            for line in pipe_in:
                if not _is_suppressed_error(line):
                    original_out.write(line)
                    original_out.flush()

    t = threading.Thread(target=_filter_thread, daemon=True)
    t.start()


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip()
    # Linux ext4 limit: 255 bytes/filename; Japanese chars are 3 bytes each in UTF-8
    # Truncate to 200 bytes (leaves room for ".md" and safety margin)
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        name = encoded[:200].decode("utf-8", errors="ignore")
    return name


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


def _repair_index() -> None:
    """_index.json から対応する .md ファイルが存在しないエントリを削除する。"""
    if not TRANSCRIPTS_DIR.exists():
        _err("[warn] transcripts/ が存在しません")
        return
    total_removed = 0
    for index_path in sorted(TRANSCRIPTS_DIR.glob("*/_index.json")):
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as e:
            _err(f"[warn] {index_path} 読み込み失敗: {e}")
            continue
        to_remove = [
            vid_id for vid_id, info in index.items()
            if info.get("file") and not Path(info["file"]).exists()
        ]
        if not to_remove:
            continue
        for vid_id in to_remove:
            title = index[vid_id].get("title", vid_id)
            _err(f"[repair] 削除: {index_path.parent.name} / {title}")
            del index[vid_id]
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        total_removed += len(to_remove)
    _err(f"[repair] 完了: {total_removed} 件削除")


def _is_globally_processed(vid_id: str) -> tuple[bool, str, str]:
    """全チャンネルの _index.json を横断して vid_id を検索。
    Returns (found, channel_name, title)"""
    if not TRANSCRIPTS_DIR.exists():
        return False, "", ""
    for index_path in TRANSCRIPTS_DIR.glob("*/_index.json"):
        channel_name = index_path.parent.name
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if vid_id in index:
            return True, channel_name, index[vid_id].get("title", "")
    return False, "", ""


def _queue_dir(channel_name: str) -> Path:
    return QUEUE_DIR / _sanitize(channel_name)


def _queued_video_ids(channel_name: str) -> set[str]:
    d = _queue_dir(channel_name)
    if not d.exists():
        return set()
    audio_exts = {".m4a", ".webm", ".opus", ".mp4"}
    return {f.stem for f in d.iterdir() if f.suffix in audio_exts}


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


def _remove_channel(name: str) -> None:
    lines = CHANNELS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = []
    found = False
    for line in lines:
        entry = line.split("|")[0].strip()
        if entry == name:
            found = True
        else:
            new_lines.append(line)
    if not found:
        _err(f"[error] '{name}' が channels.txt に見つかりません")
        sys.exit(1)
    CHANNELS_FILE.write_text("".join(new_lines), encoding="utf-8")
    _err(f"[removed] {name}")


def _list_channels() -> None:
    channels = _load_channels()
    if not channels:
        _err("チャンネルが登録されていません。python transcribe.py add <name> <url> で追加してください。")
        return
    for name, info in channels.items():
        print(f"{name} | {info['url']} | {info['lang']}")


# ── yt-dlp ヘルパー ────────────────────────────────────────────────────────────

_FIREFOX_COOKIES_WSL = Path(
    "/mnt/c/Users/gyshi/AppData/Roaming/Mozilla/Firefox"
    "/Profiles/7sswib5o.default-release/cookies.sqlite"
)


def _refresh_cookies_from_windows_chrome() -> bool:
    """Firefox の cookies.sqlite を直接コピーして Netscape 形式の cookies.txt に変換する。
    Chrome 127+ の App-Bound Encryption で Chrome/Edge は外部復号不可のため Firefox を採用。
    ネットワークアクセス不要・Firefox 起動中でも動作する。
    """
    import sqlite3
    if not _FIREFOX_COOKIES_WSL.exists():
        _err("[cookies] Firefox の cookies.sqlite が見つかりません → スキップ")
        return False

    # Firefox 起動中でも読めるよう一時コピー
    tmp_db = Path(tempfile.mktemp(suffix=".sqlite"))
    try:
        shutil.copy2(_FIREFOX_COOKIES_WSL, tmp_db)
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            "SELECT host, path, isSecure, expiry, name, value, isHttpOnly FROM moz_cookies"
        ).fetchall()
        conn.close()
    except Exception as e:
        _err(f"[cookies] SQLite 読み込み失敗: {e}")
        tmp_db.unlink(missing_ok=True)
        return False
    finally:
        tmp_db.unlink(missing_ok=True)

    if not rows:
        _err("[cookies] クッキーが0件 → スキップ")
        return False

    lines = ["# Netscape HTTP Cookie File", "# Generated by yt-learn refresh-cookies", ""]
    for host, path, secure, expiry, name, value, http_only in rows:
        if not host.startswith("."):
            host = "." + host
        flag = "TRUE" if host.startswith(".") else "FALSE"
        secure_str = "TRUE" if secure else "FALSE"
        lines.append(f"{host}\t{flag}\t{path}\t{secure_str}\t{expiry}\t{name}\t{value}")

    COOKIES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _err(f"[cookies] {len(rows)} 件のクッキーを cookies.txt に書き出しました")
    return True


def _cookie_opts() -> dict:
    """cookies.txt を使用する yt-dlp オプションを返す。"""
    return {"cookiefile": str(COOKIES_FILE)}


def _web_client_args() -> dict:
    """YouTube の player_client 指定を返す。

    以前は web を固定していたが、YouTube 側の変更で web/ios/mweb が音声形式を
    返さなくなり「Requested format is not available」で全件失敗するようになった
    （2026-08-02 実測 / yt-dlp 2026.07.04。android_vr と yt-dlp の既定選択のみ成功）。

    クライアントの優先順位は yt-dlp 側が追随して更新するため、ここで固定せず
    **既定選択に委ねる**。特定クライアントに固定したい場合だけ値を返すこと。
    なお n-challenge の解決には deno（JS ランタイム）が PATH に必要。
    """
    return {}

def _yt_extract_with_retry(opts: dict, url: str, download: bool = False) -> dict:
    """yt-dlp の extract_info を実行。bot検知エラーで1度だけ3秒待ってリトライ。"""
    import yt_dlp
    import time
    for attempt in range(2):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download) or {}
        except yt_dlp.utils.DownloadError as e:
            if attempt == 0 and "Sign in to confirm" in str(e):
                _err("[retry] bot検知 → 3秒待って再試行")
                time.sleep(3)
                continue
            raise


def _get_video_title(url: str) -> str:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestaudio/best",
        "ignore_no_formats_error": True,
        "extractor_args": {"youtube": {"lang": ["ja"], **_web_client_args()}},
        "http_headers": {"Accept-Language": "ja,ja-JP;q=0.9"},
        **_cookie_opts(),
    }
    info = _yt_extract_with_retry(opts, url, download=False)
    return info.get("title", "untitled")


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
        "extractor_args": {"youtube": {"lang": ["ja"]}},
        "http_headers": {"Accept-Language": "ja,ja-JP;q=0.9"},
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
    """再生数を取得。メンバー限定動画は -1（sentinel）を返し、次回スキップ対象とする。"""
    import yt_dlp
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _TqdmLogger(),
                "sleep_interval_requests": 1.0,
                "ignore_no_formats_error": True,  # 再生数取得時はformat不要
                "extractor_args": {"youtube": {**_web_client_args()}},
                **_cookie_opts()}
    try:
        info = _yt_extract_with_retry(ydl_opts, url, download=False)
    except yt_dlp.utils.DownloadError as e:
        if _is_members_only_error(str(e)):
            return -1
        raise
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
    # -1（メンバー限定）も「キャッシュ済み」として再取得しない
    to_fetch = [v for v in videos if _extract_video_id(v["url"]) not in cache]
    sample = to_fetch if sample_size == 0 else to_fetch[:sample_size]

    if sample:
        import time
        _err(f"[popular] {len(sample)} 件の再生数を取得中...")
        for i, v in enumerate(tqdm(sample, desc="view count", file=sys.stderr, dynamic_ncols=True,
                      disable=not sys.stderr.isatty())):
            vid_id = _extract_video_id(v["url"])
            try:
                cache[vid_id] = _fetch_view_count(vid_id)
            except Exception as e:
                if "rate-limited" in str(e):
                    _err("[popular] レートリミット検知。キャッシュ済みデータで続行します")
                    break
            if i % 10 == 0:
                _save_view_cache(channel_name, cache)
            time.sleep(2)
        _save_view_cache(channel_name, cache)

    def _key(v):
        # -1（メンバー限定）は人気度0として最後尾に
        return max(cache.get(_extract_video_id(v["url"]), 0), 0)

    return sorted(videos, key=_key, reverse=True)


# ── 範囲指定（動画基準・日付基準） ────────────────────────────────────────────
#
# チャンネルの /videos タブは常に新着順（降順）で返る。2026-08 実測:
#   index 183 = 2024-07-20 / index 300 = 2020-11-08 / index 420 = 2019-05-15
# この性質があるので、
#   ・動画指定（--since-video / --until-video）は「リスト内の位置」でスライスするだけで
#     済み、追加のネットワーク呼び出しが一切要らない
#   ・日付指定（--after / --before）は二分探索で境界インデックスだけ求めればよく、
#     全件の日付取得（= 確実にレートリミットで破綻する）を回避できる
# flat 抽出は upload_date / timestamp を返さない（実測: 421件すべて None）ため日付は
# 個別に取りに行くしかない。だからこそ取得回数を log2(N) ≒ 9 回に抑える。

def _date_cache_path(channel_name: str) -> Path:
    return CACHE_DIR / f"{_sanitize(channel_name)}_date_cache.json"


def _load_date_cache(channel_name: str) -> dict:
    p = _date_cache_path(channel_name)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_date_cache(channel_name: str, cache: dict) -> None:
    p = _date_cache_path(channel_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_upload_date(video_id: str) -> str | None:
    """単一動画の投稿日を YYYY-MM-DD で返す。取得できなければ None。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "logger": _TqdmLogger(), "sleep_interval_requests": 1.0,
                "ignore_no_formats_error": True,
                "extractor_args": {"youtube": {**_web_client_args()}},
                **_cookie_opts()}
    try:
        info = _yt_extract_with_retry(ydl_opts, url, download=False)
    except Exception:
        return None
    raw = info.get("upload_date") or ""
    if len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def _upload_date_at(videos: list, i: int, channel_name: str, cache: dict,
                    lo: int = 0, hi: int = None) -> tuple[str | None, int]:
    """videos[i] 付近で投稿日が取れる動画を探し (投稿日, その動画の index) を返す。

    メンバー限定動画は日付が取れない（このチャンネルでは新しい側の43%が該当）。
    二分探索の途中でそれに当たると境界が求まらなくなるため近傍で代用するが、
    **代用したときは「どの index の日付か」も返す**。代用元の index を判定点に
    使わないと、たとえば「境界より古い側の動画の日付」を mid の日付とみなして
    しまい、境界が実際より新しい側にずれる。
    探索は [lo, hi) の内側に限る。外に出ると二分探索が収束しなくなるため。
    """
    hi = len(videos) if hi is None else hi
    for offset in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
        j = i + offset
        if not lo <= j < hi:
            continue
        vid = _extract_video_id(videos[j]["url"])
        if vid in cache:
            if cache[vid]:
                return cache[vid], j
            continue
        d = _fetch_upload_date(vid)
        cache[vid] = d
        _save_date_cache(channel_name, cache)
        time.sleep(1.5)
        if d:
            return d, j
    return None, -1


def _resolve_date_index(videos: list, channel_name: str, target: str,
                        inclusive: bool = True) -> int:
    """降順リストで「target より新しい側」が終わる位置 k を二分探索で返す。

    inclusive=True なら target 当日も新しい側に含める（videos[0:k] が date >= target）。
    inclusive=False なら date > target が videos[0:k]。
    """
    cache = _load_date_cache(channel_name)
    lo, hi, probes = 0, len(videos), 0
    while lo < hi:
        mid = (lo + hi) // 2
        d, j = _upload_date_at(videos, mid, channel_name, cache, lo, hi)
        probes += 1
        if d is None:
            # 窓内のどこからも日付が取れない。判定不能なので窓を1つ詰めて前進させる
            # （安全側＝対象を広めに残す方向へ倒す）
            lo = mid + 1
            continue
        # 判定は mid ではなく「実際に日付が取れた index j」で行う。
        # j は必ず [lo, hi) の内側なので lo/hi は毎回真に狭まり、必ず収束する。
        if (d >= target) if inclusive else (d > target):
            lo = j + 1
        else:
            hi = j
    _save_date_cache(channel_name, cache)
    _err(f"[range] 日付境界 {target} を {probes} 回の取得で解決 → index {lo}")
    return lo


def _resolve_video_index(videos: list, ref: str) -> int:
    """URL または動画IDが videos の何番目かを返す。見つからなければ -1。"""
    ref_id = _extract_video_id(ref)
    for i, v in enumerate(videos):
        if _extract_video_id(v["url"]) == ref_id:
            return i
    return -1


def _filter_by_range(videos: list, channel_name: str, since_video: str = None,
                     until_video: str = None, after: str = None, before: str = None,
                     exclusive: bool = False) -> list:
    """新着順リストを範囲で絞り込む。

    since_video / after … その動画・日付「以降」（＝より新しい側）を残す
    until_video / before … その動画・日付「以前」（＝より古い側）を残す
    exclusive … 境界に指定した動画それ自身を含めない（日付指定には影響しない）
    """
    start, end = 0, len(videos)  # videos[start:end] が対象

    if since_video:
        i = _resolve_video_index(videos, since_video)
        if i < 0:
            raise ValueError(f"--since-video の動画がチャンネル一覧にありません: {since_video}")
        end = min(end, i if exclusive else i + 1)
        _err(f"[range] since-video {since_video} → index {i}")

    if until_video:
        i = _resolve_video_index(videos, until_video)
        if i < 0:
            raise ValueError(f"--until-video の動画がチャンネル一覧にありません: {until_video}")
        start = max(start, i + 1 if exclusive else i)
        _err(f"[range] until-video {until_video} → index {i}")

    if after:
        end = min(end, _resolve_date_index(videos, channel_name, after, inclusive=True))
    if before:
        start = max(start, _resolve_date_index(videos, channel_name, before, inclusive=False))

    if start >= end:
        _err(f"[range] 範囲が空です (start={start}, end={end})")
        return []
    selected = videos[start:end]
    _err(f"[range] {len(videos)} 件中 {len(selected)} 件を選択 (index {start}..{end - 1})")
    return selected


# ── ダウンロード ──────────────────────────────────────────────────────────────

# 360p は format 18（映像+音声が単一ファイル）。マージ不要でDLが速く、実測で最も安定する。
# それ以上の画質は映像と音声が別ストリームなので yt-dlp にマージさせる（ffmpeg 必須）。
# いずれも音声を含むため、**同じファイルを Whisper の入力にそのまま使える**＝
# 納品用の動画を落としても YouTube への問い合わせ回数は 1 本あたり 1 回のまま増えない。
_VIDEO_FORMATS = {
    "360p": "18/best[height<=?360]/best",
    "720p": "bestvideo[height<=?720]+bestaudio/best[height<=?720]/best",
    "1080p": "bestvideo[height<=?1080]+bestaudio/best[height<=?1080]/best",
    "best": "bestvideo+bestaudio/best",
}


def _download_audio(url: str, out_dir: str, video_quality: str = None) -> str:
    import yt_dlp, time
    # クライアントの指定順。ios/web/mweb は bot 検知が緩く長く使えていたが、
    # YouTube 側の変更で「Requested format is not available」を返すようになることがある
    # （2026-08 実測: ios/web/mweb は3つとも音声形式を返さず、android_vr と
    #   yt-dlp の既定選択のみ成功）。
    # 最後の attempt は None = extractor_args を渡さず **yt-dlp の既定選択に委ねる**。
    # クライアントの優先順位は yt-dlp 側が追随して更新するため、ここを固定し続けるより
    # 既定に委ねる枝を残しておくほうが陳腐化に強い。
    _CLIENT_SEQUENCES = [
        ["ios", "web", "mweb"],   # attempt 0: ios 優先
        ["android_vr", "web"],    # attempt 1: android_vr（実測で現行有効）
        None,                     # attempt 2: yt-dlp の既定選択に委ねる
    ]
    # video_quality 指定時は音声つきの動画を落とし、それをそのまま Whisper に食わせる。
    # format 18 の音声は AAC 96kbps だが Whisper は 16kHz mono に落として使うため
    # 文字起こし精度への影響は無い。
    fmt = (_VIDEO_FORMATS[video_quality] if video_quality
           else "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best")
    for attempt in range(3):
        ydl_opts = {
            # 音声を優先（m4a→webm→任意のbestaudio）。最後の保険で best も許容
            "format": fmt,
            "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
            # 投稿日・再生数・尺をタダで手に入れる。DL時には情報を既に持っているので
            # 書き出しに追加の問い合わせは発生しない（納品ファイル名の日付に使う）
            "writeinfojson": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _TqdmLogger(),
            **_cookie_opts(),
        }
        if _CLIENT_SEQUENCES[attempt] is not None:
            ydl_opts["extractor_args"] = {
                "youtube": {"player_client": _CLIENT_SEQUENCES[attempt]}
            }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            break
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if attempt < 2 and "not a bot" in err:
                time.sleep(3)
                continue
            if attempt < 2 and "Requested format is not available" in err:
                # セッション状態の一時的な不整合→少し待ってリトライ
                time.sleep(10)
                continue
            raise
    # 動画指定時は動画コンテナを先に拾う（音声を内包しているので Whisper 入力を兼ねる）
    exts = ((".mp4", ".mkv", ".webm", ".m4a", ".opus") if video_quality
            else (".m4a", ".webm", ".opus", ".mp4"))
    for ext in exts:
        for f in Path(out_dir).iterdir():
            if f.suffix == ext:
                return str(f)
    raise RuntimeError(f"音声ファイルが見つかりません: {out_dir}")


# ── 文字起こし ─────────────────────────────────────────────────────────────────

def _transcribe_whisper_cpp(audio_path: str, lang: str, model_size: str) -> str:
    import subprocess, os
    model_file = WHISPER_MODELS_DIR / f"ggml-{model_size}.bin"
    if not model_file.exists():
        raise RuntimeError(f"モデルファイルが見つかりません: {model_file}")

    tmpwav = None
    audio = audio_path
    if not audio_path.endswith(".wav"):
        tmpwav = tempfile.mktemp(suffix=".wav")
        result = subprocess.run(
            ["ffmpeg", "-i", audio_path, "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", tmpwav, "-y", "-loglevel", "error"],
            capture_output=True, text=True, errors="replace",
        )
        if result.returncode != 0:
            _err(f"[ffmpeg-stderr] {result.stderr.strip()[-1000:]}")
            raise subprocess.CalledProcessError(result.returncode, result.args,
                                                 output=result.stdout, stderr=result.stderr)
        audio = tmpwav

    env = os.environ.copy()
    build_dir = WHISPER_CLI.parent.parent
    lib_dirs = [
        str(build_dir / "src"),
        str(build_dir / "ggml/src"),
        str(build_dir / "ggml/src/ggml-metal"),
        str(build_dir / "ggml/src/ggml-blas"),
    ]
    existing = env.get("DYLD_LIBRARY_PATH", "")
    env["DYLD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_base = str(Path(tmpdir) / "out")
            _err(f"[model] {model_size} (whisper.cpp / Metal) をロード中...")
            _err(f"[transcribe] {Path(audio_path).name}")

            dur_result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio],
                capture_output=True, text=True,
            )
            duration = float(dur_result.stdout.strip()) if dur_result.stdout.strip() else 0

            import re as _re
            from tqdm import tqdm as _tqdm
            # subprocess.Popen の stderr= には binary file を渡す（OSレベルの fd 書き込み）
            stderr_file = tempfile.TemporaryFile(mode="w+b")
            try:
                proc = subprocess.Popen(
                    [str(WHISPER_CLI), "-m", str(model_file), "-f", audio,
                     "-l", lang, "-of", out_base, "-otxt"],
                    stdout=subprocess.PIPE, stderr=stderr_file,
                    text=True, encoding="utf-8", errors="replace", env=env,
                )
                with _tqdm(total=int(duration), unit="s", file=sys.stderr, dynamic_ncols=True,
                           disable=not sys.stderr.isatty()) as pbar:
                    last = 0
                    for line in proc.stdout:
                        m = _re.match(r'\[(\d+):(\d+):(\d+\.\d+)', line)
                        if m:
                            h, mn, s = m.groups()
                            current = int(h) * 3600 + int(mn) * 60 + float(s)
                            inc = int(current) - last
                            if inc > 0:
                                pbar.update(inc)
                                last = int(current)
                    pbar.n = pbar.total or last
                    pbar.refresh()
                proc.wait()
                if proc.returncode != 0:
                    stderr_file.seek(0)
                    stderr_text = stderr_file.read().decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        _err(f"[whisper-stderr] {stderr_text[-1500:]}")
                    raise subprocess.CalledProcessError(proc.returncode, proc.args)
            finally:
                stderr_file.close()

            out_file = Path(out_base + ".txt")
            return out_file.read_text(encoding="utf-8", errors="replace").strip() if out_file.exists() else ""
    finally:
        if tmpwav:
            Path(tmpwav).unlink(missing_ok=True)


def _cuda_available() -> bool:
    import shutil, subprocess
    if not shutil.which("nvidia-smi"):
        return False
    try:
        return subprocess.run(["nvidia-smi"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _preload_cuda_libs() -> None:
    """pip install した nvidia-*-cu12 の .so を ctypes で先読みして dlopen に見せる"""
    import ctypes, sysconfig
    site = Path(sysconfig.get_path("purelib"))
    for pkg in ["cuda_runtime", "cublas", "cudnn"]:
        lib_dir = site / "nvidia" / pkg / "lib"
        if not lib_dir.exists():
            continue
        for so in sorted(lib_dir.glob("lib*.so.*")):
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def _transcribe_faster_whisper(audio_path: str, lang: str, model_size: str,
                                device: str, compute_type: str, label: str) -> str:
    from faster_whisper import WhisperModel
    from tqdm import tqdm
    if device == "cuda":
        _preload_cuda_libs()
    _err(f"[model] {model_size} (faster-whisper / {label}) をロード中...")
    kwargs = {"device": device, "compute_type": compute_type}
    if device == "cpu":
        kwargs["cpu_threads"] = 8
    model = WhisperModel(model_size, **kwargs)
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
    with tqdm(total=duration or None, unit="s",
              bar_format=bar_fmt if duration > 0 else None,
              file=sys.stderr, dynamic_ncols=True,
              disable=not sys.stderr.isatty()) as pbar:
        for seg in segments_iter:
            if seg.text.strip():
                texts.append(seg.text.strip())
            pbar.update(seg.end - pbar.n)
    return "\n".join(texts)


def _transcribe_cpu(audio_path: str, lang: str, model_size: str) -> str:
    return _transcribe_faster_whisper(audio_path, lang, model_size,
                                      device="cpu", compute_type="int8", label="CPU")


def _transcribe_gpu(audio_path: str, lang: str, model_size: str) -> str:
    return _transcribe_faster_whisper(audio_path, lang, model_size,
                                      device="cuda", compute_type="float16", label="CUDA")


def _transcribe(audio_path: str, lang: str = "ja", model_size: str = WHISPER_MODEL) -> str:
    if sys.platform == "darwin":
        return _transcribe_whisper_cpp(audio_path, lang, model_size)
    if _cuda_available():
        return _transcribe_gpu(audio_path, lang, model_size)
    return _transcribe_cpu(audio_path, lang, model_size)


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


# ── ポイントサマリー ──────────────────────────────────────────────────────────

def _call_ollama(prompt: str, base_url: str, model: str) -> str | None:
    import urllib.request
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{OLLAMA_GENERATE_PATH}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("response") or "").strip() or None


def _generate_core_summary(title: str, text: str) -> tuple[str, str]:
    local_url = os.environ.get("LOCAL_LLM_URL")
    local_model = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b")

    if not local_url:
        raise RuntimeError("LOCAL_LLM_URL が未設定です")

    prompt = f"""\
以下はYouTube動画の文字起こしです。

タイトル: {title}

---
{text[:4000]}
---

この動画の内容を、文字起こしを読んでいない人が読んでも完全に理解できる箇条書きにしてください。

ルール:
- 各点は単独で読んで意味が通るよう、主語・対象・文脈を省略しない
- 「この動画では」「ここでは」「このチャンネルでは」「YouTubeでは」のような指示語・参照語は使わない
- 数値や固有名詞は必ず文脈とセットで書く（例: 「Amazonでは重量物運搬時に2人体制が義務付けられている」）
- 内容を省略しない。重要な情報はすべて含める（多少長くなっても可）
- 列挙系タイトル（Top N、〇〇選など）の場合は全項目をカバーする
- マークダウン装飾（**など）は使わない

出力形式: 「## ポイント」という見出しの後に「- 」始まりの箇条書きのみ。それ以外の文章は一切不要。"""

    result = _call_ollama(prompt, local_url, local_model)
    if not result:
        raise RuntimeError("Ollama レスポンスが空でした")
    return result, f"Ollama({local_model})"


def _inject_core_summary(md_path: Path) -> None:
    content = md_path.read_text(encoding="utf-8")
    if "## ポイント" in content:
        return
    raw_transcript = content.split("\n---\n", 1)[-1].strip()
    summary, backend = _generate_core_summary(
        title=re.search(r"^# (.+)", content, re.MULTILINE).group(1) if re.search(r"^# (.+)", content, re.MULTILINE) else "",
        text=raw_transcript,
    )
    # 「処理日時: ...」行の直後、「---」の直前に挿入
    updated = re.sub(
        r"(処理日時: .+\n)(\n---\n)",
        rf"\1\n{summary}\n\2",
        content,
        count=1,
    )
    if updated != content:
        md_path.write_text(updated, encoding="utf-8")
        _err(f"[summary] ポイント挿入完了 (by {backend}): {md_path.name}")


# ── 処理エントリポイント ───────────────────────────────────────────────────────

def _deliver_video_dir(channel_name: str) -> Path:
    return DELIVER_DIR / _sanitize(channel_name) / "videos"


def _read_info_json(tmpdir: str, vid_id: str) -> dict:
    """yt-dlp が書いた <id>.info.json から投稿日・尺・再生数を拾う。

    納品ファイル名の日付や並び順に使う。取れなくても処理は止めない
    （info.json が無い＝古いバージョンで処理した動画、というだけなので）。
    """
    p = Path(tmpdir) / f"{vid_id}.info.json"
    if not p.exists():
        return {}
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    raw = info.get("upload_date") or ""
    out = {}
    if len(raw) == 8:
        out["upload_date"] = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if info.get("duration"):
        out["duration"] = int(info["duration"])
    if info.get("view_count"):
        out["view_count"] = int(info["view_count"])
    return out


def _process_url(url: str, channel_name: str, lang: str = "ja", title: str = None,
                 output_dir: Path = None, model_size: str = WHISPER_MODEL,
                 force: bool = False, video_quality: str = None) -> bool:
    vid_id = _extract_video_id(url)
    index = _load_index(channel_name)

    if not force:
        if vid_id in index:
            _err(f"[skip] 処理済み: {index[vid_id]['title']}")
            return False

        found, other_channel, other_title = _is_globally_processed(vid_id)
        if found:
            _err(f"[skip] 処理済み (チャンネル: {other_channel}): {other_title}")
            return False

    if title is None:
        _err(f"[info] タイトル取得中: {url}")
        title = _get_video_title(url)

    tmpdir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        _err(f"[download] {url}")
        audio_path = _download_audio(url, tmpdir, video_quality=video_quality)
        if video_quality:
            # tmpdir は finally で消えるので、文字起こしの前に納品用へ退避しておく
            vdir = _deliver_video_dir(channel_name)
            vdir.mkdir(parents=True, exist_ok=True)
            dest = vdir / f"{vid_id}{Path(audio_path).suffix}"
            shutil.copy2(audio_path, dest)
            _err(f"[video] {dest.name} ({dest.stat().st_size / 1024 / 1024:.0f}MB)")
        text = _transcribe(audio_path, lang, model_size=model_size)
        saved = _save_transcript(channel_name, title, url, text, output_dir=output_dir, model_size=model_size)

        _err(f"[saved] {saved}")
        _inject_core_summary(saved)
        _copy_file_to_drive(saved)
        index[vid_id] = {
            "title": title,
            "url": url,
            "file": str(saved),
            "transcribed_at": date.today().isoformat(),
            **_read_info_json(tmpdir, vid_id),
        }
        _save_index(channel_name, index)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _process_channel(channel_name: str, channel_url: str, lang: str = "ja", limit: int = 0,
                     sort: str = "date", popular_sample: int = 0,
                     model_size: str = WHISPER_MODEL, cache_only: bool = False,
                     force: bool = False, since_video: str = None, until_video: str = None,
                     after: str = None, before: str = None, exclusive: bool = False,
                     video_quality: str = None, dry_run: bool = False) -> int:
    _err(f"[channel] {channel_name}: 動画リスト取得中... (sort={sort})")
    videos = _get_channel_videos(channel_url)
    _err(f"[channel] {len(videos)} 件の動画を発見")

    # 範囲の絞り込みは人気順ソートより先に行う。
    # 位置スライスは「新着順に並んでいる」ことが前提なので、並べ替え後では成立しない。
    if any([since_video, until_video, after, before]):
        videos = _filter_by_range(videos, channel_name, since_video, until_video,
                                  after, before, exclusive)
        if not videos:
            return 0

    if sort == "popular":
        videos = _sort_by_popularity(videos, channel_name, popular_sample)
        _update_ranking(channel_name, videos)

    if cache_only:
        _err(f"[cache-only] {channel_name}: キャッシュ構築のみ完了\n")
        return 0

    index = _load_index(channel_name)
    cache = _load_view_cache(channel_name)
    videos = [
        v for v in videos
        if (force or _extract_video_id(v["url"]) not in index)
        and cache.get(_extract_video_id(v["url"]), 0) != -1  # メンバー限定をスキップ
    ]
    if limit > 0:
        videos = videos[:limit]

    if dry_run:
        _err(f"[dry-run] {channel_name}: 対象 {len(videos)} 件")
        for i, v in enumerate(videos, 1):
            print(f"{i:4d}  {_extract_video_id(v['url'])}  {v['title']}")
        _err("[dry-run] メンバー限定はDL時にしか判定できないため、実際の取得数はこれ以下になる")
        return 0

    processed = 0
    for i, v in enumerate(videos, 1):
        _err(f"\n[{i}/{len(videos)}] {v['title']}")
        try:
            if _process_url(v["url"], channel_name, lang, title=v["title"],
                            model_size=model_size, force=force, video_quality=video_quality):
                processed += 1
                index = _load_index(channel_name)
        except Exception as e:
            msg = str(e)
            if "rate-limited" in msg:
                _err(f"[warn] {channel_name}: レートリミット → このチャンネルの処理を中断")
                break
            if _is_members_only_error(msg):
                # 次回以降スキップできるよう view キャッシュに sentinel を刻む。
                # このチャンネルは新しい側の約半分がメンバー限定なので、毎回DLを
                # 試みると無駄な問い合わせでレートリミットを浪費する。
                cache[_extract_video_id(v["url"])] = -1
                _save_view_cache(channel_name, cache)
                _err(f"[warn] {v['title']}: メンバー限定 → スキップ（次回以降は判定不要）")
                continue
            if "confirm your age" in msg or "age-restricted" in msg:
                _err(f"[warn] {v['title']}: 年齢制限 → スキップ")
                continue
            if "not a bot" in msg or "Sign in to confirm" in msg:
                _err(f"[warn] {v['title']}: bot検知 → スキップ（cookies期限切れの可能性）")
                continue
            if "Got error" in msg or "Read timed out" in msg or "Connection reset" in msg:
                _err(f"[warn] {v['title']}: ネットワークエラー → スキップ")
                continue
            if "HTTP Error 403" in msg:
                _err(f"[warn] {v['title']}: アクセス禁止(403) → スキップ")
                continue
            _err(f"[error] {v['title']}: {e}")

    if sort == "popular" and processed > 0:
        _update_ranking(channel_name, videos)
    _err(f"[done] {channel_name}: {processed} 件処理\n")
    return processed


def _download_channel_to_queue(
    channel_name: str, channel_url: str, lang: str = "ja",
    limit: int = 0, sort: str = "popular", popular_sample: int = 200,
    since_video: str = None, until_video: str = None, after: str = None,
    before: str = None, exclusive: bool = False, video_quality: str = None,
) -> tuple[int, bool]:
    """音声を queue/ にダウンロードのみ行い文字起こしはしない。戻り値: (added, rate_limited)

    GPU が塞がっている間にDLだけ進めたいときに使う（DLはGPUを使わない）。
    範囲指定は _process_channel と同じ引数を受ける。ここに通しておかないと
    --since-video 等が黙って無視され、チャンネル全件を落としにいく。
    """
    _err(f"[dl-queue] {channel_name}: 動画リスト取得中... (sort={sort})")
    try:
        videos = _get_channel_videos(channel_url)
    except Exception as e:
        if "rate-limited" in str(e):
            _err(f"[rate-limit] {channel_name}: 動画リスト取得でレートリミット")
            return 0, True
        raise
    _err(f"[dl-queue] {len(videos)} 件の動画を発見")

    # 範囲の絞り込みは人気順ソートより先。位置スライスは新着順の並びが前提
    if any([since_video, until_video, after, before]):
        videos = _filter_by_range(videos, channel_name, since_video, until_video,
                                  after, before, exclusive)
        if not videos:
            return 0, False

    if sort == "popular":
        try:
            videos = _sort_by_popularity(videos, channel_name, popular_sample)
            _update_ranking(channel_name, videos)
        except Exception as e:
            if "rate-limited" in str(e):
                _err(f"[rate-limit] {channel_name}: 人気順ソートでレートリミット")
                return 0, True
            raise

    index = _load_index(channel_name)
    cache = _load_view_cache(channel_name)
    queued = _queued_video_ids(channel_name)
    videos = [
        v for v in videos
        if _extract_video_id(v["url"]) not in index
        and cache.get(_extract_video_id(v["url"]), 0) != -1
        and _extract_video_id(v["url"]) not in queued
    ]
    if limit > 0:
        videos = videos[:limit]

    q_dir = _queue_dir(channel_name)
    q_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    for v in videos:
        vid_id = _extract_video_id(v["url"])
        _err(f"\n[dl-queue] {v['title']}")
        try:
            audio_path = _download_audio(v["url"], str(q_dir), video_quality=video_quality)
            if video_quality:
                # drain-queue は文字起こし後に queue のファイルを消す。360p では
                # 音声と動画が同一ファイルなので、消される前に納品用へ退避する
                vdir = _deliver_video_dir(channel_name)
                vdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(audio_path, vdir / f"{vid_id}{Path(audio_path).suffix}")
            meta = {
                "title": v["title"],
                "url": v["url"],
                "channel": channel_name,
                "lang": lang,
                "queued_at": datetime.now().isoformat(timespec="seconds"),
                **_read_info_json(str(q_dir), vid_id),
            }
            # info.json は meta.json に畳んでから消す。queue/ に余計なファイルを
            # 残すと drain-queue の走査対象が無駄に増える
            (q_dir / f"{vid_id}.info.json").unlink(missing_ok=True)
            (q_dir / f"{vid_id}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            added += 1
            _err(f"[queued] {v['title']}")
        except Exception as e:
            msg = str(e)
            if "rate-limited" in msg:
                _err(f"[rate-limit] {channel_name}: レートリミット → DL中断")
                return added, True
            if "confirm your age" in msg or "age-restricted" in msg:
                _err(f"[warn] {v['title']}: 年齢制限 → スキップ")
                continue
            if "not a bot" in msg or "Sign in to confirm" in msg:
                _err(f"[warn] {v['title']}: bot検知 → スキップ")
                continue
            if "Got error" in msg or "Read timed out" in msg or "Connection reset" in msg:
                _err(f"[warn] {v['title']}: ネットワークエラー → スキップ")
                continue
            if "HTTP Error 403" in msg:
                _err(f"[warn] {v['title']}: アクセス禁止(403) → スキップ")
                continue
            _err(f"[error] {v['title']}: {e}")

    _err(f"[queue-added] {channel_name}: {added} 件をキューに追加\n")
    return added, False


def _drain_queue_all(model_size: str = WHISPER_MODEL,
                     idle_polls: int = 3, idle_sleep: int = 10) -> int:
    """queue/ の音声ファイルを文字起こしし続ける。キューが idle_polls 回連続空なら終了。"""
    import time
    audio_exts = {".m4a", ".webm", ".opus", ".mp4"}
    consecutive_empty = 0
    processed = 0

    while True:
        candidates = []
        if QUEUE_DIR.exists():
            for ch_dir in QUEUE_DIR.iterdir():
                if not ch_dir.is_dir():
                    continue
                for audio_file in ch_dir.iterdir():
                    if audio_file.suffix not in audio_exts:
                        continue
                    meta_file = audio_file.with_suffix(".meta.json")
                    if not meta_file.exists():
                        _err(f"[warn] [queue-skip] meta なし: {audio_file.name}")
                        continue
                    candidates.append((audio_file.stat().st_mtime, audio_file))

        if not candidates:
            consecutive_empty += 1
            if consecutive_empty >= idle_polls:
                _err("[queue-empty] キューが空です")
                break
            time.sleep(idle_sleep)
            continue

        consecutive_empty = 0
        candidates.sort()
        _, audio_path = candidates[0]
        meta_path = audio_path.with_suffix(".meta.json")

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            _err(f"[warn] [queue-skip] meta 読み込み失敗: {meta_path.name}")
            meta_path.unlink(missing_ok=True)
            continue

        vid_id = _extract_video_id(meta["url"])
        index = _load_index(meta["channel"])
        if vid_id in index:
            _err(f"[skip] 処理済み（重複）: {meta['title']}")
            audio_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            continue

        _err(f"\n[drain] {meta['title']}")
        try:
            text = _transcribe(str(audio_path), meta["lang"], model_size=model_size)
            saved = _save_transcript(meta["channel"], meta["title"], meta["url"], text,
                                     model_size=model_size)
            _err(f"[saved] {saved}")
            _inject_core_summary(saved)
            _copy_file_to_drive(saved)
            index[vid_id] = {
                "title": meta["title"],
                "url": meta["url"],
                "file": str(saved),
                "transcribed_at": date.today().isoformat(),
                **{k: meta[k] for k in ("upload_date", "duration", "view_count") if k in meta},
            }
            _save_index(meta["channel"], index)
            audio_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            processed += 1
        except Exception as e:
            _err(f"[error] drain-queue: {e}")

    return processed


def _git_pull_silent() -> None:
    import subprocess
    if not shutil.which("git"):
        return
    subprocess.run(
        ["git", "pull", "--ff-only", "--quiet"],
        cwd=BASE_DIR, capture_output=True,
    )


def _git_push_cache() -> None:
    import subprocess
    if not shutil.which("git"):
        return
    index_files = list(TRANSCRIPTS_DIR.glob("*/_index.json")) if TRANSCRIPTS_DIR.exists() else []
    index_rel = [str(p.relative_to(BASE_DIR)) for p in index_files]
    changed = subprocess.run(
        ["git", "status", "--porcelain", "cache/", "channels.txt"] + index_rel,
        capture_output=True, text=True, cwd=BASE_DIR,
    ).stdout.strip()
    if not changed:
        return
    subprocess.run(["git", "add", "cache/", "channels.txt"] + index_rel, cwd=BASE_DIR)
    subprocess.run(
        ["git", "commit", "-m", f"chore: update cache ({date.today().isoformat()})"],
        cwd=BASE_DIR,
    )
    result = subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True, text=True)
    if result.returncode == 0:
        _err("[git] cache/ を push しました")
    else:
        _err(f"[git] push 失敗: {result.stderr.strip()}")


def _copy_file_to_drive(file_path: Path) -> None:
    import subprocess
    if not shutil.which("rclone"):
        return
    try:
        rel = file_path.relative_to(BASE_DIR)
    except ValueError:
        return
    dest = f"{RCLONE_DEST}/{rel.parent}"
    subprocess.run(
        ["rclone", "copy", str(file_path), dest],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _err(f"[drive] {rel} → {dest}")


def _sync_drive(dirs: list[str] | None = None) -> None:
    import subprocess
    if dirs is None:
        dirs = ["transcripts", "summaries"]
    if not shutil.which("rclone"):
        _err("[error] rclone がインストールされていません。brew install rclone を実行してください")
        sys.exit(1)
    for d in dirs:
        src = BASE_DIR / d
        dest = f"{RCLONE_DEST}/{d}"
        _err(f"[sync] {src} → {dest}")
        result = subprocess.run(
            ["rclone", "sync", str(src), dest, "--progress"],
            text=True,
        )
        if result.returncode != 0:
            _err(f"[error] 同期失敗: {d}")
            sys.exit(1)
    _err("[done] Google Drive への同期が完了しました")



# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()
    # autonomous.sh 等のパイプ配下では親プロセスがログを管理するためスキップ
    if sys.stderr.isatty():
        _setup_log()
    _install_stderr_pipe_filter()

    parser = argparse.ArgumentParser(
        description="YouTube動画の文字起こし・チャンネル管理ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
常時稼働（WSL 推奨）:
  ./autonomous.sh                    # これだけ叩けば全自動（rate-limit自動回復・GPU常時稼働）
  ./autonomous.sh --limit 10 --model large-v3
  Ctrl+C で安全停止 → [session-end] を logs/autonomous/*.log に記録

ローカルLLM（Ollama）を使う場合:
  LOCAL_LLM_URL が設定されていれば Ollama 優先、失敗時は Gemini にフォールバック。
  Mac: .env に LOCAL_LLM_URL=http://<Windows-TailscaleIP>:11434 を設定（トンネル不要）
  WSL: .env に LOCAL_LLM_URL=http://localhost:11434 を設定（トンネル不要）

examples:
  # チャンネル追加（言語省略時は ja）
  python transcribe.py add メンタリストDAIGO https://www.youtube.com/@mentalistdaigo
  python transcribe.py add 3Blue1Brown https://www.youtube.com/@3blue1brown en

  # 登録チャンネル一覧
  python transcribe.py list

  # 単発URL（--model で軽量モデルを指定して高速化）
  python transcribe.py process https://youtu.be/xxx --model tiny
  python transcribe.py process https://youtu.be/aaa https://youtu.be/bbb --channel "メンタリストDAIGO"
  python transcribe.py process -f urls.txt --channel ひろゆき
  python transcribe.py process https://youtu.be/xxx -o ~/Desktop/output --model small

  # チャンネル全取得
  python transcribe.py channel "メンタリストDAIGO" --sort popular --limit 5 --model tiny
  python transcribe.py channel "メンタリストDAIGO" --sort popular --limit 100
  python transcribe.py channel "メンタリストDAIGO" --sort popular --cache-only  # 再生数キャッシュのみ構築
  python transcribe.py channel "メンタリストDAIGO" --sort popular --popular-sample 50 --limit 10

  # 範囲を絞る（/videos タブが新着順である性質を使う。動画指定は追加の通信ゼロ）
  python transcribe.py channel "八田エミリの日常" --url https://www.youtube.com/@hatta_emily \
      --since-video pLXK5N3Sy4A --dry-run          # まず対象を数える
  python transcribe.py channel "八田エミリの日常" --url https://www.youtube.com/@hatta_emily \
      --since-video pLXK5N3Sy4A --keep-video       # 動画も保存しつつ文字起こし
  python transcribe.py channel "メンタリスト DaiGo" --after 2025-01-01
  python transcribe.py channel "メンタリスト DaiGo" --before 2020-12-31 --limit 10

  # 全チャンネル一括
  python transcribe.py all --sort popular --limit 20
  python transcribe.py all --sort popular --cache-only

  # Google Drive 同期
  python transcribe.py sync --only transcripts
  python transcribe.py sync --only summaries
  python transcribe.py sync

AI要約は別スクリプト:
  python summarize.py "メンタリストDAIGO" --threshold 20
  python summarize.py all --force
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="チャンネルを channels.txt に追加")
    p_add.add_argument("name", help="チャンネル名（ディレクトリ名になる）")
    p_add.add_argument("url", help="チャンネルURL")
    p_add.add_argument("lang", nargs="?", default="ja", help="文字起こし言語 (default: ja)")

    p_remove = sub.add_parser("remove", help="チャンネルを channels.txt から削除")
    p_remove.add_argument("name", help="削除するチャンネル名")

    sub.add_parser("list", help="登録チャンネル一覧を表示")

    p_proc = sub.add_parser("process", help="特定URLを文字起こし（複数可）")
    p_proc.add_argument("urls", nargs="*", help="動画URL（複数可、省略時は --file が必須）")
    p_proc.add_argument("--channel", default="misc", help="チャンネル名（省略時は misc）")
    p_proc.add_argument("--lang", default="ja")
    p_proc.add_argument("--model", default=WHISPER_MODEL,
                        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"],
                        help=f"Whisperモデル (default: {WHISPER_MODEL})")
    p_proc.add_argument("-f", "--file", help="URLを1行1件で記述したテキストファイル（#はコメント、'URL | en' で言語指定可）")
    p_proc.add_argument("-o", "--output", help="出力ディレクトリ（省略時は transcripts/{channel}/）")
    p_proc.add_argument("--force", action="store_true", help="処理済みでも再度文字起こしする")

    p_ch = sub.add_parser("channel", help="チャンネルの全動画を処理")
    p_ch.add_argument("name", help="channels.txt のチャンネル名（--url 指定時は任意の名前でよい）")
    p_ch.add_argument("--url", help="channels.txt に未登録のチャンネルを名前+URLで直接処理する"
                                    "（単発の作業でチャンネルを常設登録したくない場合に使う）")
    p_ch.add_argument("--model", default=WHISPER_MODEL,
                      choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"])
    p_ch.add_argument("--limit", type=int, default=0, help="最大処理動画数（0=全件）")
    p_ch.add_argument("--sort", choices=["date", "popular"], default="date",
                      help="取得順序: date=新着順(default), popular=人気順")
    p_ch.add_argument("--popular-sample", type=int, default=200,
                      help="人気順ソート時に再生数を取得する動画数（0=上限なし、default: 200）"
                           "。メンバー限定動画は自動でキャッシュ＆次回スキップされる")
    p_ch.add_argument("--cache-only", action="store_true",
                      help="再生数キャッシュの構築のみ行い、文字起こしはしない（--sort popular と併用）")
    p_ch.add_argument("--download-only", action="store_true",
                      help="音声を queue/ にDLのみ行い文字起こしはしない（autonomous.sh 用）")
    p_ch.add_argument("--force", action="store_true", help="処理済みでも再度文字起こしする")
    p_ch.add_argument("--since-video", metavar="URL|ID",
                      help="この動画以降（＝これより新しい動画）だけを対象にする。既定で指定動画自身を含む")
    p_ch.add_argument("--until-video", metavar="URL|ID",
                      help="この動画以前（＝これより古い動画）だけを対象にする。既定で指定動画自身を含む")
    p_ch.add_argument("--after", metavar="YYYY-MM-DD", help="この日以降に投稿された動画だけを対象にする")
    p_ch.add_argument("--before", metavar="YYYY-MM-DD", help="この日以前に投稿された動画だけを対象にする")
    p_ch.add_argument("--exclusive", action="store_true",
                      help="--since-video / --until-video に指定した動画自身を含めない")
    p_ch.add_argument("--keep-video", action="store_true",
                      help="動画ファイルも deliver/{channel}/videos/ に保存する（文字起こしと同じDLを使うため追加の問い合わせは発生しない）")
    p_ch.add_argument("--video-quality", choices=list(_VIDEO_FORMATS), default="360p",
                      help="--keep-video 時の画質 (default: 360p。360p はマージ不要で最も速い)")
    p_ch.add_argument("--dry-run", action="store_true",
                      help="対象になる動画の一覧を表示するだけで何もダウンロードしない")

    p_all = sub.add_parser("all", help="全チャンネルを処理")
    p_all.add_argument("--model", default=WHISPER_MODEL,
                       choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"])
    p_all.add_argument("--limit", type=int, default=0)
    p_all.add_argument("--sort", choices=["date", "popular"], default="date")
    p_all.add_argument("--popular-sample", type=int, default=200)
    p_all.add_argument("--cache-only", action="store_true")
    p_all.add_argument("--force", action="store_true", help="処理済みでも再度文字起こしする")
    p_all.add_argument("--after", metavar="YYYY-MM-DD", help="この日以降に投稿された動画だけを対象にする")
    p_all.add_argument("--before", metavar="YYYY-MM-DD", help="この日以前に投稿された動画だけを対象にする")
    p_all.add_argument("--keep-video", action="store_true",
                       help="動画ファイルも deliver/{channel}/videos/ に保存する")
    p_all.add_argument("--video-quality", choices=list(_VIDEO_FORMATS), default="360p")
    p_all.add_argument("--dry-run", action="store_true",
                       help="対象になる動画の一覧を表示するだけで何もダウンロードしない")

    p_drain = sub.add_parser("drain-queue", help="queue/ の音声を文字起こし（autonomous.sh 用）")
    p_drain.add_argument("--model", default=WHISPER_MODEL,
                         choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"])

    sub.add_parser("refresh-cookies", help="Windows Chrome からクッキーを取得して cookies.txt を更新")

    sub.add_parser("repair-index", help="_index.json からファイルが存在しないエントリを削除")

    p_sync = sub.add_parser("sync", help="transcripts/ と summaries/ を Google Drive に同期")
    p_sync.add_argument("--only", choices=["transcripts", "summaries"],
                        help="同期対象を絞る（省略時は両方）")

    args = parser.parse_args()

    if args.cmd == "add":
        _add_channel(args.name, args.url, args.lang)
        _git_push_cache()

    elif args.cmd == "remove":
        _remove_channel(args.name)
        _git_push_cache()

    elif args.cmd == "list":
        _list_channels()

    elif args.cmd == "process":
        _git_pull_silent()
        url_langs = [(u, args.lang) for u in args.urls]
        if args.file:
            try:
                for line in Path(args.file).read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "|" in line:
                        url, lang = line.split("|", 1)
                        url_langs.append((url.strip(), lang.strip()))
                    else:
                        url_langs.append((line, args.lang))
            except FileNotFoundError:
                _err(f"[error] ファイルが見つかりません: {args.file}")
                sys.exit(1)
        if not url_langs:
            _err("[error] URLを引数で渡すか、--file でテキストファイルを指定してください")
            sys.exit(1)
        output_dir = Path(args.output) if args.output else None
        for i, (url, lang) in enumerate(url_langs):
            if i > 0:
                _err("")
            _process_url(url, args.channel, lang, output_dir=output_dir, model_size=args.model, force=args.force)

    elif args.cmd == "channel":
        if args.url:
            info = {"url": args.url, "lang": "ja"}
        else:
            channels = _load_channels()
            if args.name not in channels:
                _err(f"[error] '{args.name}' が channels.txt に見つかりません")
                _err("  常設登録せずに処理するなら --url でチャンネルURLを直接指定する")
                sys.exit(1)
            info = channels[args.name]
        if args.download_only:
            _download_channel_to_queue(
                args.name, info["url"], info["lang"], args.limit, args.sort, args.popular_sample,
                since_video=args.since_video, until_video=args.until_video,
                after=args.after, before=args.before, exclusive=args.exclusive,
                video_quality=args.video_quality if args.keep_video else None,
            )
        else:
            _process_channel(args.name, info["url"], info["lang"], args.limit, args.sort,
                             args.popular_sample, args.model, args.cache_only, args.force,
                             since_video=args.since_video, until_video=args.until_video,
                             after=args.after, before=args.before, exclusive=args.exclusive,
                             video_quality=args.video_quality if args.keep_video else None,
                             dry_run=args.dry_run)
            if not args.dry_run:
                _git_push_cache()

    elif args.cmd == "drain-queue":
        count = _drain_queue_all(args.model)
        if count == 0:
            sys.exit(2)

    elif args.cmd == "refresh-cookies":
        ok = _refresh_cookies_from_windows_chrome()
        sys.exit(0 if ok else 1)

    elif args.cmd == "sync":
        dirs = [args.only] if args.only else None
        _sync_drive(dirs)

    elif args.cmd == "repair-index":
        _repair_index()

    elif args.cmd == "all":
        channels = _load_channels()
        if not channels:
            _err("[warn] channels.txt にチャンネルが登録されていません")
            sys.exit(0)
        for name, info in channels.items():
            _process_channel(name, info["url"], info["lang"], args.limit, args.sort,
                             args.popular_sample, args.model, args.cache_only, args.force,
                             after=args.after, before=args.before,
                             video_quality=args.video_quality if args.keep_video else None,
                             dry_run=args.dry_run)
        if not args.dry_run:
            _git_push_cache()


if __name__ == "__main__":
    main()
