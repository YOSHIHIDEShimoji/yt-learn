import asyncio
import re
import shutil
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent.parent
PORTAL_DIR = Path(__file__).parent

app = FastAPI(title="yt-learn Portal")

# rclone link 結果キャッシュ（プロセス内永続）
_drive_url_cache: dict[str, str] = {}

async def _rclone_link(path: str) -> str:
    if path in _drive_url_cache:
        return _drive_url_cache[path]
    if not shutil.which("rclone"):
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "link", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        url = stdout.decode().strip() if proc.returncode == 0 else ""
        _drive_url_cache[path] = url
        return url
    except Exception:
        return ""
app.mount("/static", StaticFiles(directory=PORTAL_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PORTAL_DIR / "templates")


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


@app.get("/api/readme")
async def get_readme():
    readme = ROOT / "README.md"
    content = readme.read_text(encoding="utf-8") if readme.exists() else ""
    return JSONResponse({"content": content})


@app.get("/api/logs")
async def get_logs():
    log_dirs = [ROOT / "logs", ROOT / "log"]
    log_files = []
    for log_dir in log_dirs:
        if log_dir.exists():
            for f in sorted(log_dir.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                log_files.append({
                    "name": f.name,
                    "path": str(f.relative_to(ROOT)),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                })
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

        # 最近処理した動画（[saved] 行から抽出）
        recent_videos = []
        for l in reversed(lines):
            if "[saved]" not in l:
                continue
            m = re.search(r'\[saved\]\s+(.+\.md)', l)
            if not m:
                continue
            path_str = m.group(1).replace("\\", "/")
            parts = path_str.split("/")
            title   = parts[-1].replace(".md", "") if parts else path_str
            channel = parts[-2] if len(parts) >= 2 else ""
            recent_videos.append({"title": title, "channel": channel})
            if len(recent_videos) >= 8:
                break

        # 現在のフェーズ（直近の行から推定）
        phase = "アイドル"
        for l in reversed(lines[-20:]):
            if "[model]" in l or "[transcribe]" in l:
                phase = "Whisper 処理中"
                break
            if "[summary]" in l:
                phase = "AI 要約中"
                break
            if "[download]" in l:
                phase = "ダウンロード中"
                break

        # 稼働ステータス
        status = "不明"
        for l in reversed(lines[-30:]):
            if "[session-end]" in l:
                status = "停止"
                break
            if "rate-limit" in l.lower() or "レートリミット" in l:
                status = "rate-limit 中"
                break
            if "[done]" in l or "[download]" in l or "[saved]" in l or "[skip]" in l:
                status = "稼働中"
                break

        # セッション開始行
        last_session = next(
            (l for l in reversed(lines) if "=== 開始" in l or "=== Started" in l), None
        )

        # queue 残数
        queue_dir = ROOT / "queue"
        queue_count = len(list(queue_dir.glob("*.m4a"))) if queue_dir.exists() else 0

        # Google Drive リンクを並列取得（キャッシュ済みなら即返却）
        unique_channels = list({v["channel"] for v in recent_videos if v.get("channel")})
        drive_results = await asyncio.gather(
            _rclone_link("gdrive:yt-learn"),
            *[_rclone_link(f"gdrive:yt-learn/transcripts/{ch}") for ch in unique_channels],
        )
        drive_folder_url = drive_results[0]
        channel_drive = dict(zip(unique_channels, drive_results[1:]))
        for v in recent_videos:
            v["drive_url"] = channel_drive.get(v.get("channel", ""), "")

        return JSONResponse({
            "log_file": logs[0].name,
            "done_count": done_count,
            "warn_count": warn_count,
            "error_count": error_count,
            "rate_limit_count": rl_count,
            "queue_count": queue_count,
            "recent_videos": recent_videos,
            "phase": phase,
            "status": status,
            "last_session": last_session,
            "lines": lines[-50:],
            "drive_folder_url": drive_folder_url,
        })

    return JSONResponse({
        "error": "ログファイルなし",
        "done_count": 0, "warn_count": 0, "error_count": 0,
        "rate_limit_count": 0, "queue_count": 0,
        "recent_videos": [], "phase": "—", "status": "不明",
        "last_session": None, "lines": [], "drive_folder_url": "",
    })
