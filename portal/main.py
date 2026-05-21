import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
PORTAL_DIR = Path(__file__).parent

# WSL 環境かどうかを起動時に一度だけ判定
_proc_version = Path("/proc/version")
IS_WSL = _proc_version.exists() and "microsoft" in _proc_version.read_text().lower()


class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


app = FastAPI(title="yt-learn Portal")
app.mount("/static", _NoCacheStaticFiles(directory=PORTAL_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PORTAL_DIR / "templates")

# docs/ が存在すれば /docs で配信（README 内の画像リンク用）
_docs_dir = ROOT / "docs"
if _docs_dir.exists():
    app.mount("/docs", StaticFiles(directory=_docs_dir), name="docs")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ── Drive キャッシュ ──────────────────────────────────────────
DRIVE_LINK_CACHE_FILE = PORTAL_DIR / "drive_link_cache.json"

_rclone_link_cache: dict[str, str] = {}          # path → url
_rclone_link_cache_ts: dict[str, float] = {}     # path → epoch seconds
_drive_file_cache: dict[str, dict[str, str]] = {}  # channel → {title: url}
_drive_file_cache_ts: dict[str, float] = {}      # channel → epoch seconds
DRIVE_FILE_CACHE_TTL = 60.0                       # 秒
RCLONE_LINK_EMPTY_TTL = 60.0                      # 空文字は短く（フォルダ未作成→作成後の検出用）
RCLONE_LINK_HIT_TTL = 3600.0                      # URL 取得済みは長く（URL 変化は稀）


def _load_drive_link_cache() -> None:
    """起動時: ファイルから非空 URL を復元。起動直後から Drive リンクを即表示できる。"""
    if not DRIVE_LINK_CACHE_FILE.exists():
        return
    try:
        data: dict[str, str] = json.loads(DRIVE_LINK_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        for path, url in data.items():
            if url:  # 空文字は復元しない
                _rclone_link_cache[path] = url
                _rclone_link_cache_ts[path] = now  # 起動時点を新鮮扱いに
    except Exception:
        pass


def _save_drive_link_cache() -> None:
    """非空 URL のみファイルに保存。asyncio シングルスレッドなので排他制御不要。"""
    try:
        data = {path: url for path, url in _rclone_link_cache.items() if url}
        DRIVE_LINK_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


_load_drive_link_cache()  # モジュールロード時（サーバー起動時）に実行

# 同時 rclone 実行数を制限（Google Drive API レートリミット対策）
# asyncio.Semaphore はイベントループ生成後でないと使えないため起動時に初期化
_rclone_semaphore: asyncio.Semaphore | None = None


def _get_rclone_semaphore() -> asyncio.Semaphore:
    global _rclone_semaphore
    if _rclone_semaphore is None:
        _rclone_semaphore = asyncio.Semaphore(4)  # 最大 4 並列
    return _rclone_semaphore


async def _rclone_link(path: str) -> str:
    """rclone link で URL 取得。空文字は 60 秒、ヒットは 1 時間キャッシュ。"""
    cached = _rclone_link_cache.get(path)
    if cached is not None:
        age = time.time() - _rclone_link_cache_ts.get(path, 0)
        ttl = RCLONE_LINK_HIT_TTL if cached else RCLONE_LINK_EMPTY_TTL
        if age < ttl:
            return cached
    if not shutil.which("rclone"):
        return cached or ""
    try:
        async with _get_rclone_semaphore():
            proc = await asyncio.create_subprocess_exec(
                "rclone", "link", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        url = stdout.decode().strip() if proc.returncode == 0 else ""
        _rclone_link_cache[path] = url
        _rclone_link_cache_ts[path] = time.time()
        if url:
            _save_drive_link_cache()  # 非空 URL を即ファイル永続化
        return url
    except Exception:
        return cached or ""


_drive_fetch_running: set[str] = set()


async def _fetch_channel_drive_urls_bg(channel: str) -> None:
    """バックグラウンドで rclone lsjson を実行してキャッシュを埋める（タイムアウトなし）"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "lsjson", "--files-only",
            f"gdrive:yt-learn/transcripts/{channel}/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()  # タイムアウトなし（大フォルダ対応）
        if proc.returncode != 0:
            return
        items = json.loads(stdout.decode())
        result = {}
        for item in items:
            name = item.get("Name", "")
            file_id = item.get("ID", "")
            if name and file_id:
                title = name[:-3] if name.endswith(".md") else name
                result[title] = f"https://drive.google.com/file/d/{file_id}/view"
        _drive_file_cache[channel] = result
        _drive_file_cache_ts[channel] = time.time()
    except Exception:
        pass  # 失敗してもキャッシュしない → 次回リトライ
    finally:
        _drive_fetch_running.discard(channel)


def _get_channel_drive_urls(channel: str) -> dict[str, str]:
    """キャッシュ TTL 60 秒。スタル時は古い値を返しつつバックグラウンドで再取得。"""
    cached = _drive_file_cache.get(channel)
    fresh = cached is not None and (time.time() - _drive_file_cache_ts.get(channel, 0)) < DRIVE_FILE_CACHE_TTL
    if not fresh and channel not in _drive_fetch_running and shutil.which("rclone"):
        _drive_fetch_running.add(channel)
        asyncio.ensure_future(_fetch_channel_drive_urls_bg(channel))
    return cached if cached is not None else {}


# ── ログ解析 ──────────────────────────────────────────────────
def _parse_session_videos(lines: list[str]) -> tuple[list[dict], dict | None]:
    """ログ行から動画イベントを解析。(done_videos newest-first, running_or_None) を返す"""
    done: list[dict] = []
    cur: dict | None = None

    for l in lines:
        ts_m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', l)
        ts = ts_m.group(1) if ts_m else ""

        if "[drain]" in l:
            m = re.search(r'\[drain\]\s+(.+)', l)
            if m:
                cur = {"title": m.group(1).strip(), "channel": "",
                       "drain_ts": ts, "drive_ts": ""}
        elif "[saved]" in l and cur is not None:
            m = re.search(r'\[saved\]\s+(.+\.md)', l)
            if m:
                parts = m.group(1).replace("\\", "/").split("/")
                cur["channel"] = parts[-2] if len(parts) >= 2 else ""
        elif "[drive]" in l and cur is not None:
            cur["drive_ts"] = ts
            try:
                if cur["drain_ts"] and cur["drive_ts"]:
                    t1 = datetime.strptime(cur["drain_ts"], "%Y-%m-%d %H:%M:%S")
                    t2 = datetime.strptime(cur["drive_ts"], "%Y-%m-%d %H:%M:%S")
                    cur["processing_sec"] = int((t2 - t1).total_seconds())
                else:
                    cur["processing_sec"] = 0
            except ValueError:
                cur["processing_sec"] = 0
            done.append(cur)
            cur = None

    running = cur
    done.reverse()
    return done, running


# ── Routes ───────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/channels")
async def get_channels():
    channels_file = ROOT / "channels.txt"
    channels = []
    if channels_file.exists():
        for line in channels_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                channels.append({
                    "name": parts[0],
                    "url": parts[1],
                    "lang": parts[2] if len(parts) >= 3 else "ja",
                })
    return JSONResponse({"channels": channels})


@app.get("/api/channel-drive-urls")
async def get_channel_drive_urls_api():
    channels_file = ROOT / "channels.txt"
    names = []
    if channels_file.exists():
        for line in channels_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                names.append(parts[0])
    urls = await asyncio.gather(*[_rclone_link(f"gdrive:yt-learn/transcripts/{n}") for n in names])
    return JSONResponse({"drive_urls": dict(zip(names, urls))})


@app.get("/api/readme")
async def get_readme():
    readme = ROOT / "README.md"
    content = readme.read_text(encoding="utf-8") if readme.exists() else ""
    return JSONResponse({"content": content})


@app.get("/api/logs")
async def get_logs():
    log_dirs = [ROOT / "logs", ROOT / "log"]
    log_files = []
    now = time.time()
    live_threshold = 30 * 60  # 30分以内に更新 + session-end なし → live

    for log_dir in log_dirs:
        if log_dir.exists():
            for f in sorted(log_dir.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                try:
                    tail = f.read_bytes()[-512:].decode(errors="replace")
                    session_ended = "[session-end]" in tail
                    recently_modified = (now - f.stat().st_mtime) < live_threshold
                    is_done = session_ended or not recently_modified
                except Exception:
                    is_done = True
                log_files.append({
                    "name": f.name,
                    "path": str(f.relative_to(ROOT)),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "is_done": is_done,
                })

    # live を先頭、次いで done（両グループ内は mtime 降順）
    log_files.sort(key=lambda x: (x["is_done"], -x["mtime"]))
    return JSONResponse({"logs": log_files})


@app.get("/api/log-content")
async def get_log_content(path: str):
    try:
        target = (ROOT / path).resolve()
        if not str(target).startswith(str(ROOT.resolve())):
            return JSONResponse({"error": "アクセス拒否"}, status_code=403)
        if not target.exists() or target.suffix != ".log":
            return JSONResponse({"error": "ファイルが見つかりません"}, status_code=404)
        content = target.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"content": content, "name": target.name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/status-summary")
async def get_status_summary():
    log_dirs = [ROOT / "logs", ROOT / "log"]

    for log_dir in log_dirs:
        if not log_dir.exists():
            continue
        logs = sorted(log_dir.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not logs:
            continue

        text = logs[0].read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        done_count  = sum(1 for l in lines if "[done]" in l)
        warn_count  = sum(1 for l in lines if "[warn]" in l)
        error_count = sum(1 for l in lines if "[error]" in l)
        rl_count    = sum(1 for l in lines if "rate-limit" in l.lower() or "レートリミット" in l)

        done_videos, running_video = _parse_session_videos(lines)

        phase = "idle"
        for l in reversed(lines[-20:]):
            if "[model]" in l or "[transcribe]" in l:
                phase = "transcribing"; break
            if "[summary]" in l:
                phase = "summarizing"; break
            if "[download]" in l:
                phase = "downloading"; break

        status = "unknown"
        for l in reversed(lines[-30:]):
            if "[session-end]" in l:
                status = "stopped"; break
            if "rate-limit" in l.lower() or "レートリミット" in l:
                status = "rate-limit"; break
            if "[done]" in l or "[download]" in l or "[saved]" in l or "[skip]" in l:
                status = "running"; break

        last_session = next(
            (l for l in reversed(lines) if "=== 開始" in l or "=== Started" in l), None
        )

        queue_dir = ROOT / "queue"
        queue_count = len(list(queue_dir.glob("*.m4a"))) if queue_dir.exists() else 0

        # 個別ファイル Drive URL（バックグラウンドフェッチ済みキャッシュから取得）
        unique_channels = list({v["channel"] for v in done_videos if v.get("channel")})
        folder_url = await _rclone_link("gdrive:yt-learn")
        ch_url_map = {ch: _get_channel_drive_urls(ch) for ch in unique_channels}
        for v in done_videos:
            file_map = ch_url_map.get(v.get("channel", ""), {})
            v["drive_url"] = file_map.get(v.get("title", ""), "")

        return JSONResponse({
            "log_file": logs[0].name,
            "done_count": done_count,
            "warn_count": warn_count,
            "error_count": error_count,
            "rate_limit_count": rl_count,
            "queue_count": queue_count,
            "done_videos": done_videos,
            "running_video": running_video,
            "phase": phase,
            "status": status,
            "last_session": last_session,
            "drive_folder_url": folder_url,
        })

    return JSONResponse({
        "error": "ログファイルなし",
        "done_count": 0, "warn_count": 0, "error_count": 0,
        "rate_limit_count": 0, "queue_count": 0,
        "done_videos": [], "running_video": None, "phase": "—", "status": "不明",
        "last_session": None, "drive_folder_url": "",
    })


# ── Phase 2: チャンネル管理 ──────────────────────────────────────

class ChannelBody(BaseModel):
    name: str
    url: str
    lang: str = "ja"


@app.post("/api/channels")
async def add_channel(body: ChannelBody):
    name = body.name.strip()
    url = body.url.strip()
    lang = body.lang.strip() or "ja"
    if not name or not url:
        return JSONResponse({"error": "name と url は必須です"}, status_code=400)
    channels_file = ROOT / "channels.txt"
    content = channels_file.read_text(encoding="utf-8") if channels_file.exists() else ""
    for line in content.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] == name:
            return JSONResponse({"error": f"'{name}' は既に登録済みです"}, status_code=409)
    if content and not content.endswith("\n"):
        content += "\n"
    content += f"{name} | {url} | {lang}\n"
    channels_file.write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True})


@app.delete("/api/channels")
async def delete_channel(name: str):
    channels_file = ROOT / "channels.txt"
    if not channels_file.exists():
        return JSONResponse({"error": "channels.txt が見つかりません"}, status_code=404)
    lines = channels_file.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            parts = [p.strip() for p in stripped.split("|")]
            if parts[0] == name:
                found = True
                continue
        new_lines.append(line)
    if not found:
        return JSONResponse({"error": f"'{name}' が見つかりません"}, status_code=404)
    channels_file.write_text("".join(new_lines), encoding="utf-8")
    return JSONResponse({"ok": True})


# ── Phase 2: 実行管理 ────────────────────────────────────────────

class RunBody(BaseModel):
    limit: int = 10
    model: str = "large-v3"


_VALID_MODELS = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"}
_YT_SESSION_PREFIX = "yt-learn_"


async def _find_yt_session() -> str | None:
    """tmux ls から yt-learn_ プレフィックスのセッション名を返す。なければ None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "ls",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode != 0:
            return None
        for line in stdout.decode().splitlines():
            name = line.split(":")[0]
            if name.startswith(_YT_SESSION_PREFIX):
                return name
        return None
    except Exception:
        return None


@app.get("/api/env")
async def get_env():
    return JSONResponse({"is_wsl": IS_WSL})


@app.get("/api/run/status")
async def run_status():
    session = await _find_yt_session()
    return JSONResponse({"running": session is not None, "session": session})


@app.post("/api/run")
async def start_run(body: RunBody):
    if not IS_WSL:
        return JSONResponse({"error": "WSL 環境でのみ実行できます"}, status_code=400)
    existing = await _find_yt_session()
    if existing:
        return JSONResponse({"error": f"既に実行中です ({existing})"}, status_code=409)
    limit   = max(1, min(body.limit, 100))
    model   = body.model if body.model in _VALID_MODELS else "large-v3"
    session = f"{_YT_SESSION_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", session,
        f"zsh -ic './autonomous.sh --limit {limit} --model {model}'",
        cwd=str(ROOT),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return JSONResponse({"ok": proc.returncode == 0, "session": session})


@app.post("/api/run/stop")
async def stop_run():
    if not IS_WSL:
        return JSONResponse({"error": "WSL 環境でのみ実行できます"}, status_code=400)
    session = await _find_yt_session()
    if not session:
        return JSONResponse({"ok": True, "was_running": False})
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", session,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return JSONResponse({"ok": True, "was_running": True, "session": session})


# ── Phase 2: URL 処理（複数URL対応） ────────────────────────────

class ProcessUrlBody(BaseModel):
    urls: list[str]
    channel: str = "misc"
    lang: str = "ja"


async def _await_and_close(proc, f) -> None:
    try:
        await proc.wait()
    finally:
        f.close()


async def _bg_process_urls(urls: list[str], channel: str, lang: str) -> None:
    log_dir = ROOT / "logs" / "process"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_process.log"
    try:
        python = shutil.which("python") or "python3"
        f = open(log_file, "wb")
        proc = await asyncio.create_subprocess_exec(
            python, str(ROOT / "transcribe.py"), "process", *urls,
            "--channel", channel, "--lang", lang,
            cwd=str(ROOT),
            stdout=f, stderr=f,
        )
        asyncio.ensure_future(_await_and_close(proc, f))
    except Exception:
        pass


@app.post("/api/process-url")
async def process_url(body: ProcessUrlBody):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        return JSONResponse({"error": "URL は必須です"}, status_code=400)
    asyncio.ensure_future(_bg_process_urls(urls, body.channel or "misc", body.lang or "ja"))
    return JSONResponse({"ok": True, "message": f"{len(urls)} 件の処理を開始しました", "count": len(urls)})


# ── Phase 2: その他 CLI コマンド ──────────────────────────────────

async def _bg_run_script(args: list[str], log_subdir: str, log_prefix: str) -> None:
    log_dir = ROOT / "logs" / log_subdir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{log_prefix}.log"
    try:
        f = open(log_file, "wb")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(ROOT), stdout=f, stderr=f,
        )
        asyncio.ensure_future(_await_and_close(proc, f))
    except Exception:
        pass


class TranscribeChannelBody(BaseModel):
    channel: str
    limit: int = 10
    model: str = "large-v3"


class TranscribeAllBody(BaseModel):
    limit: int = 10
    model: str = "large-v3"


class TranscribeSyncBody(BaseModel):
    only: str = ""


class SummarizeBody(BaseModel):
    threshold: int = 20


@app.post("/api/transcribe/channel")
async def transcribe_channel(body: TranscribeChannelBody):
    channel = body.channel.strip()
    if not channel:
        return JSONResponse({"error": "channel は必須です"}, status_code=400)
    limit = max(1, min(body.limit, 100))
    model = body.model if body.model in _VALID_MODELS else "large-v3"
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "transcribe.py"), "channel", channel,
            "--sort", "popular", "--limit", str(limit), "--model", model]
    asyncio.ensure_future(_bg_run_script(args, "transcribe", f"ch_{channel[:20]}"))
    return JSONResponse({"ok": True, "message": f"'{channel}' の文字起こしを開始しました"})


@app.post("/api/transcribe/all")
async def transcribe_all(body: TranscribeAllBody):
    limit = max(1, min(body.limit, 100))
    model = body.model if body.model in _VALID_MODELS else "large-v3"
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "transcribe.py"), "all",
            "--sort", "popular", "--limit", str(limit), "--model", model]
    asyncio.ensure_future(_bg_run_script(args, "transcribe", "all"))
    return JSONResponse({"ok": True, "message": "全チャンネルの文字起こしを開始しました"})


@app.post("/api/transcribe/sync")
async def transcribe_sync(body: TranscribeSyncBody):
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "transcribe.py"), "sync"]
    if body.only in ("transcripts", "summaries"):
        args += ["--only", body.only]
    asyncio.ensure_future(_bg_run_script(args, "transcribe", "sync"))
    return JSONResponse({"ok": True, "message": "Drive 同期を開始しました"})


@app.post("/api/summarize")
async def summarize_all(body: SummarizeBody):
    threshold = max(1, min(body.threshold, 1000))
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "summarize.py"), "all", "--threshold", str(threshold)]
    asyncio.ensure_future(_bg_run_script(args, "summarize", "all"))
    return JSONResponse({"ok": True, "message": "要約を開始しました"})
