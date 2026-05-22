import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
PORTAL_DIR = Path(__file__).parent

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

_docs_dir = ROOT / "docs"
if _docs_dir.exists():
    app.mount("/docs", StaticFiles(directory=_docs_dir), name="docs")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ── Drive キャッシュ ──────────────────────────────────────────
DRIVE_LINK_CACHE_FILE = PORTAL_DIR / "drive_link_cache.json"

_rclone_link_cache: dict[str, str] = {}
_rclone_link_cache_ts: dict[str, float] = {}
_drive_file_cache: dict[str, dict[str, str]] = {}
_drive_file_cache_ts: dict[str, float] = {}
DRIVE_FILE_CACHE_TTL = 60.0
RCLONE_LINK_EMPTY_TTL = 60.0
RCLONE_LINK_HIT_TTL = 3600.0


def _load_drive_link_cache() -> None:
    if not DRIVE_LINK_CACHE_FILE.exists():
        return
    try:
        data: dict[str, str] = json.loads(DRIVE_LINK_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        for path, url in data.items():
            if url:
                _rclone_link_cache[path] = url
                _rclone_link_cache_ts[path] = now
    except Exception:
        pass


def _save_drive_link_cache() -> None:
    try:
        data = {path: url for path, url in _rclone_link_cache.items() if url}
        DRIVE_LINK_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


_load_drive_link_cache()

_rclone_semaphore: asyncio.Semaphore | None = None


def _get_rclone_semaphore() -> asyncio.Semaphore:
    global _rclone_semaphore
    if _rclone_semaphore is None:
        _rclone_semaphore = asyncio.Semaphore(4)
    return _rclone_semaphore


async def _rclone_link(path: str) -> str:
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
            _save_drive_link_cache()
        return url
    except Exception:
        return cached or ""


_drive_fetch_running: set[str] = set()


async def _fetch_channel_drive_urls_bg(channel: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "lsjson", "--files-only",
            f"gdrive:yt-learn/transcripts/{channel.replace('/', '_')}/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
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
        pass
    finally:
        _drive_fetch_running.discard(channel)


def _get_channel_drive_urls(channel: str) -> dict[str, str]:
    cached = _drive_file_cache.get(channel)
    fresh = cached is not None and (time.time() - _drive_file_cache_ts.get(channel, 0)) < DRIVE_FILE_CACHE_TTL
    if not fresh and channel not in _drive_fetch_running and shutil.which("rclone"):
        _drive_fetch_running.add(channel)
        asyncio.ensure_future(_fetch_channel_drive_urls_bg(channel))
    return cached if cached is not None else {}


# ── ログ解析 ──────────────────────────────────────────────────
def _parse_session_videos(lines: list[str]) -> tuple[list[dict], dict | None]:
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


# ── ジョブ管理（Phase 3）─────────────────────────────────────
_active_jobs: dict[str, dict] = {}


def _register_job(job_id: str, job_type: str, proc, log_file: Path) -> None:
    _active_jobs[job_id] = {
        "id": job_id,
        "type": job_type,
        "pid": proc.pid,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_file": str(log_file.relative_to(ROOT)),
        "proc": proc,
    }


async def _await_and_close(proc, f, job_id: str | None = None, append_session_end: bool = False) -> None:
    try:
        await proc.wait()
        if append_session_end:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[session-end] {ts}\n".encode())
    finally:
        f.close()
        if job_id:
            _active_jobs.pop(job_id, None)


# ── ジョブラベル ─────────────────────────────────────────────
_JOB_LABELS: dict[str, str] = {
    "autonomous": "autonomous.sh",
    "process":    "URL処理",
    "summarize":  "summarize",
    "sync":       "Drive Sync",
    "transcribe": "transcribe",
}


# ── summarize ログ解析 ────────────────────────────────────────
def _parse_summarize_videos(lines: list[str]) -> tuple[list[dict], dict | None]:
    done: list[dict] = []
    running: dict | None = None
    current_channel = ""
    channel_gpath: dict[str, str] = {}

    for line in lines:
        m = re.search(r'\[summarize\]\s+(.+?):', line)
        if m:
            current_channel = m.group(1).strip()
            running = {"title": current_channel, "channel": current_channel, "drive_url": ""}
            continue
        m = re.match(r'^\s+\[(\d+)/\d+\]\s+(.+)', line)
        if m and current_channel:
            done.append({"title": m.group(2).strip(), "channel": current_channel,
                         "drive_url": "", "_gpath": ""})
            continue
        m = re.search(r'\[drive\]\s+(\S+)\s+→', line)
        if m and current_channel:
            channel_gpath[current_channel] = f"gdrive:yt-learn/{m.group(1)}"
            continue
        if "[done]" in line and "サマリー更新完了" in line:
            running = None

    for v in done:
        v["_gpath"] = channel_gpath.get(v["channel"], "")

    done.reverse()
    return done, running


# ── アクティブプロセス一覧 ─────────────────────────────────────
async def _get_active_processes() -> list[dict]:
    procs = []
    for j in _active_jobs.values():
        procs.append({
            "id": j["id"],
            "type": j["type"],
            "label": _JOB_LABELS.get(j["type"], j["type"]),
            "log_file": j["log_file"],
            "started_at": j["started_at"],
        })
    if IS_WSL:
        yt_session = await _find_yt_session()
        if yt_session:
            log_dir = ROOT / "logs" / "autonomous"
            log_file = ""
            if log_dir.exists():
                logs = sorted(log_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
                if logs:
                    log_file = str(logs[0].relative_to(ROOT))
            procs.append({
                "id": f"autonomous_{yt_session}",
                "type": "autonomous",
                "label": "autonomous.sh",
                "log_file": log_file,
                "started_at": "",
            })
    return procs


# ── GPU 統計取得（Phase 3）───────────────────────────────────
async def _get_gpu_stats() -> dict:
    if IS_WSL:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                parts = stdout.decode().strip().split(",")
                if len(parts) >= 4:
                    return {
                        "available": True,
                        "util": int(parts[0].strip()),
                        "mem_used": int(parts[1].strip()),
                        "mem_total": int(parts[2].strip()),
                        "temp": int(parts[3].strip()),
                    }
        except Exception:
            pass
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', stdout.decode())
                if m:
                    return {
                        "available": True,
                        "util": int(m.group(1)),
                        "mem_used": -1,
                        "mem_total": -1,
                        "temp": -1,
                    }
        except Exception:
            pass
    return {"available": False}


# ── Status データ構築（SSE / REST 共通）────────────────────────
async def _build_status_data(log_path: str | None = None) -> dict:
    # 共通データを先に取得
    yt_session = await _find_yt_session() if IS_WSL else None
    gpu        = await _get_gpu_stats()
    processes  = await _get_active_processes()
    active_jobs = [
        {"id": j["id"], "type": j["type"], "pid": j["pid"],
         "started_at": j["started_at"], "log_file": j["log_file"]}
        for j in _active_jobs.values()
    ]
    queue_dir   = ROOT / "queue"
    queue_count = len(list(queue_dir.glob("*.m4a"))) if queue_dir.exists() else 0
    folder_url  = await _rclone_link("gdrive:yt-learn")

    # 対象ログファイルを決定
    target_log: Path | None = None
    if log_path:
        candidate = (ROOT / log_path).resolve()
        if (str(candidate).startswith(str(ROOT.resolve()))
                and candidate.exists() and candidate.suffix == ".log"):
            target_log = candidate
    if target_log is None:
        for log_dir in [ROOT / "logs", ROOT / "log"]:
            if not log_dir.exists():
                continue
            logs = sorted(log_dir.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
            if logs:
                target_log = logs[0]
                break

    if target_log is None:
        return {
            "error": "ログファイルなし",
            "done_count": 0, "warn_count": 0, "error_count": 0,
            "rate_limit_count": 0, "queue_count": queue_count,
            "done_videos": [], "running_video": None, "phase": "idle", "status": "idle",
            "drive_folder_url": folder_url, "session_type": "idle",
            "active_jobs": active_jobs, "yt_session": yt_session, "processes": processes,
            "log_file": "", "log_file_path": "", "gpu": gpu,
        }

    text  = target_log.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    done_count  = sum(1 for l in lines if "[done]" in l)
    warn_count  = sum(1 for l in lines if "[warn]" in l)
    error_count = sum(1 for l in lines if "[error]" in l)
    rl_count    = sum(1 for l in lines if "rate-limit" in l.lower() or "レートリミット" in l)

    is_summarize = any("[summarize]" in l for l in lines[:50])
    if is_summarize:
        done_videos, running_video = _parse_summarize_videos(lines)
        seen: dict[str, str] = {}
        for v in done_videos:
            gpath = v.pop("_gpath", "")
            if gpath:
                if gpath not in seen:
                    seen[gpath] = await _rclone_link(gpath)
                v["drive_url"] = seen[gpath]
    else:
        done_videos, running_video = _parse_session_videos(lines)
        unique_channels = list({v["channel"] for v in done_videos if v.get("channel")})
        ch_url_map = {ch: _get_channel_drive_urls(ch) for ch in unique_channels}
        for v in done_videos:
            file_map = ch_url_map.get(v.get("channel", ""), {})
            v["drive_url"] = file_map.get(v.get("title", ""), "")

    phase = "idle"
    for l in reversed(lines[-20:]):
        if "[model]" in l or "[transcribe]" in l:
            phase = "transcribing"; break
        if "[summary]" in l or "[summarize]" in l:
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

    session_type = "idle"
    if yt_session:
        session_type = "autonomous"
    elif active_jobs:
        types = [j["type"] for j in active_jobs]
        if   "process"   in types: session_type = "process"
        elif "summarize" in types: session_type = "summarize"
        elif "sync"      in types: session_type = "sync"
        elif "transcribe" in types: session_type = "transcribe"

    return {
        "log_file":       target_log.name,
        "log_file_path":  str(target_log.relative_to(ROOT)),
        "done_count":     done_count,
        "warn_count":     warn_count,
        "error_count":    error_count,
        "rate_limit_count": rl_count,
        "queue_count":    queue_count,
        "done_videos":    done_videos,
        "running_video":  running_video,
        "phase":          phase,
        "status":         status,
        "drive_folder_url": folder_url,
        "session_type":   session_type,
        "active_jobs":    active_jobs,
        "yt_session":     yt_session,
        "processes":      processes,
        "gpu":            gpu,
    }


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
    urls = await asyncio.gather(*[_rclone_link(f"gdrive:yt-learn/transcripts/{n.replace('/', '_')}") for n in names])
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
    live_threshold = 30 * 60

    for log_dir in log_dirs:
        if log_dir.exists():
            for f in sorted(log_dir.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                try:
                    tail = f.read_bytes()[-512:].decode(errors="replace")
                    session_ended = "[session-end]" in tail
                    recently_modified = (now - f.stat().st_mtime) < live_threshold
                    has_error = "[error]" in tail
                    is_done = session_ended or not recently_modified or has_error
                except Exception:
                    is_done = True
                    has_error = False
                log_files.append({
                    "name": f.name,
                    "path": str(f.relative_to(ROOT)),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "is_done": is_done,
                    "has_error": has_error,
                })

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
async def get_status_summary(log: str | None = None):
    return JSONResponse(await _build_status_data(log_path=log))


@app.get("/api/gpu")
async def get_gpu():
    return JSONResponse(await _get_gpu_stats())


# ── Phase 3: SSE ─────────────────────────────────────────────
@app.get("/api/events")
async def status_events(request: Request):
    async def generate():
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await _build_status_data()
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(5)
    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/log-stream")
async def log_stream(request: Request, path: str):
    try:
        target = (ROOT / path).resolve()
        if not str(target).startswith(str(ROOT.resolve())):
            return JSONResponse({"error": "アクセス拒否"}, status_code=403)
        if target.suffix != ".log":
            return JSONResponse({"error": "ファイルが見つかりません"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "パスエラー"}, status_code=400)

    async def generate():
        offset = 0
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if lines:
                yield f"data: {json.dumps({'lines': lines, 'init': True}, ensure_ascii=False)}\n\n"
                offset = len(lines)
            tail = content[-512:] if len(content) > 512 else content
            if "[session-end]" in tail:
                yield f"data: {json.dumps({'done': True})}\n\n"
                return
        except Exception:
            pass

        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(2)
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                if len(lines) > offset:
                    new_lines = lines[offset:]
                    yield f"data: {json.dumps({'lines': new_lines}, ensure_ascii=False)}\n\n"
                    offset = len(lines)
                tail = content[-512:] if len(content) > 512 else content
                if "[session-end]" in tail:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
            except Exception:
                pass

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Phase 3: ジョブ管理 API ──────────────────────────────────
@app.get("/api/jobs")
async def get_jobs():
    jobs = [
        {"id": j["id"], "type": j["type"], "pid": j["pid"],
         "started_at": j["started_at"], "log_file": j["log_file"]}
        for j in _active_jobs.values()
    ]
    return JSONResponse({"jobs": jobs})


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    job = _active_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "ジョブが見つかりません"}, status_code=404)
    try:
        job["proc"].terminate()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Phase 2: チャンネル管理 ──────────────────────────────────

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


# ── Phase 2: 実行管理 ────────────────────────────────────────

class RunBody(BaseModel):
    limit: int = 10
    model: str = "large-v3"


_VALID_MODELS = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3", "large-v3-turbo"}
_YT_SESSION_PREFIX = "yt-learn_"


async def _find_yt_session() -> str | None:
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


# ── Phase 2: URL 処理 ────────────────────────────────────────

class ProcessUrlBody(BaseModel):
    urls: list[str]
    channel: str = "misc"
    lang: str = "ja"


async def _bg_process_urls(urls: list[str], channel: str, lang: str) -> None:
    log_dir = ROOT / "logs" / "process"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f"{ts}_process.log"
    job_id = f"process_{ts}"
    try:
        python = shutil.which("python") or "python3"
        f = open(log_file, "wb")
        proc = await asyncio.create_subprocess_exec(
            python, str(ROOT / "transcribe.py"), "process", *urls,
            "--channel", channel, "--lang", lang,
            cwd=str(ROOT),
            stdout=f, stderr=f,
        )
        _register_job(job_id, "process", proc, log_file)
        asyncio.ensure_future(_await_and_close(proc, f, job_id=job_id, append_session_end=True))
    except Exception:
        pass


@app.post("/api/process-url")
async def process_url(body: ProcessUrlBody):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        return JSONResponse({"error": "URL は必須です"}, status_code=400)
    asyncio.ensure_future(_bg_process_urls(urls, body.channel or "misc", body.lang or "ja"))
    return JSONResponse({"ok": True, "message": f"{len(urls)} 件の処理を開始しました", "count": len(urls)})


# ── Phase 2: その他 CLI コマンド ──────────────────────────────

async def _bg_run_script(args: list[str], log_subdir: str, log_prefix: str, job_type: str = "script") -> None:
    log_dir = ROOT / "logs" / log_subdir
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f"{ts}_{log_prefix}.log"
    job_id = f"{job_type}_{ts}"
    try:
        f = open(log_file, "wb")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(ROOT), stdout=f, stderr=f,
        )
        _register_job(job_id, job_type, proc, log_file)
        asyncio.ensure_future(_await_and_close(proc, f, job_id=job_id, append_session_end=True))
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
    asyncio.ensure_future(_bg_run_script(args, "transcribe", f"ch_{channel[:20]}", job_type="transcribe"))
    return JSONResponse({"ok": True, "message": f"'{channel}' の文字起こしを開始しました"})


@app.post("/api/transcribe/all")
async def transcribe_all(body: TranscribeAllBody):
    limit = max(1, min(body.limit, 100))
    model = body.model if body.model in _VALID_MODELS else "large-v3"
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "transcribe.py"), "all",
            "--sort", "popular", "--limit", str(limit), "--model", model]
    asyncio.ensure_future(_bg_run_script(args, "transcribe", "all", job_type="transcribe"))
    return JSONResponse({"ok": True, "message": "全チャンネルの文字起こしを開始しました"})


@app.post("/api/transcribe/sync")
async def transcribe_sync(body: TranscribeSyncBody):
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "transcribe.py"), "sync"]
    if body.only in ("transcripts", "summaries"):
        args += ["--only", body.only]
    asyncio.ensure_future(_bg_run_script(args, "transcribe", "sync", job_type="sync"))
    return JSONResponse({"ok": True, "message": "Drive 同期を開始しました"})


@app.post("/api/summarize")
async def summarize_all(body: SummarizeBody):
    threshold = max(1, min(body.threshold, 1000))
    python = shutil.which("python") or "python3"
    args = [python, str(ROOT / "summarize.py"), "all", "--threshold", str(threshold)]
    asyncio.ensure_future(_bg_run_script(args, "summarize", "all", job_type="summarize"))
    return JSONResponse({"ok": True, "message": "要約を開始しました"})
