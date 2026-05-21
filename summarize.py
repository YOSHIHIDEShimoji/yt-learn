#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""チャンネルのサマリーをGeminiで生成・更新するスクリプト

使い方:
  python summarize.py メンタリストDAIGO     # 指定チャンネルの未要約動画をまとめて要約
  python summarize.py all                  # 全チャンネルを処理
  python summarize.py メンタリストDAIGO --force  # 処理済みを無視して全件再処理
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
SUMMARIES_DIR = BASE_DIR / "summaries"
CHANNELS_FILE = BASE_DIR / "channels.txt"

GEMINI_MODEL = "gemini-2.5-flash-lite"
OLLAMA_GENERATE_PATH = "/api/generate"
RCLONE_REMOTE = "gdrive"
RCLONE_DEST = f"{RCLONE_REMOTE}:yt-learn"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip()
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        name = encoded[:200].decode("utf-8", errors="ignore")
    return name


def _load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


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


# ── 処理済みトラッキング ───────────────────────────────────────────────────────

def _processed_path(channel_name: str) -> Path:
    return SUMMARIES_DIR / f"{_sanitize(channel_name)}_processed.json"


def _load_processed(channel_name: str) -> set:
    p = _processed_path(channel_name)
    if p.exists():
        return set(json.loads(p.read_text(encoding="utf-8")))
    return set()


def _save_processed(channel_name: str, processed: set) -> None:
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    _processed_path(channel_name).write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── ローカルLLM / Gemini 共通ユーティリティ ──────────────────────────────────

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


# ── チャンネルサマリー生成 ────────────────────────────────────────────────────

def _update_summary(channel_name: str, transcript: str, video_title: str, api_key: str, video_count: int) -> None:
    local_url = os.environ.get("LOCAL_LLM_URL")
    local_model = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b")
    from google import genai

    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = SUMMARIES_DIR / f"{_sanitize(channel_name)}.md"
    existing = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    today = date.today().isoformat()

    prompt = f"""あなたはYouTubeチャンネル「{channel_name}」の学習内容をまとめるアシスタントです。

## 既存のサマリー
{existing if existing else "（まだサマリーはありません）"}

## 新しい動画の文字起こし
タイトル: {video_title}

{transcript}

## 指示
新しい文字起こしから、既存のサマリーに**まだ含まれていないユニークな洞察・テーマ・主張**のみを抽出してサマリーに追加してください。

ルール:
- 既存サマリーにある内容は追加しない（重複排除）
- 言い回しが違っても内容が同じなら重複とみなす
- 新しい洞察がなければサマリーをそのまま返す
- 末尾の「最終更新」と「動画数」は以下の値に必ず更新する

完全なサマリー全体を以下のフォーマットで返してください（フォーマット外のテキストは不要）:

# {channel_name} - Learning Summary

## 主要テーマ
- （テーマをリスト）

## キーインサイト
- （ユニークな洞察をリスト）

---
最終更新: {today}
動画数: {video_count}
"""

    result = None

    if local_url:
        try:
            result = _call_ollama(prompt, local_url, local_model)
            if result:
                _err(f"  → Ollama({local_model}) でサマリー生成")
            else:
                _err("  → Ollama レスポンスが空 → Geminiにフォールバック")
        except Exception as e:
            _err(f"  → Ollama接続失敗 ({e}) → Geminiにフォールバック")

    if not result:
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY が未設定でOllamaも利用できません")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        result = response.text.strip()

    summary_path.write_text(result + "\n", encoding="utf-8")
    _err(f"  → サマリー更新: {summary_path.name}")


# ── チャンネル処理 ────────────────────────────────────────────────────────────

def _summarize_channel(channel_name: str, api_key: str, force: bool = False, threshold: int = 0) -> None:
    channel_dir = TRANSCRIPTS_DIR / _sanitize(channel_name)
    if not channel_dir.exists():
        _err(f"[skip] トランスクリプトなし: {channel_dir}")
        return

    all_transcripts = sorted(channel_dir.glob("*.md"))
    if not all_transcripts:
        _err(f"[skip] {channel_name}: トランスクリプトファイルが0件")
        return

    processed = set() if force else _load_processed(channel_name)
    new_transcripts = [t for t in all_transcripts if t.name not in processed]

    if threshold > 0 and len(new_transcripts) < threshold:
        _err(f"[skip] {channel_name}: 未処理 {len(new_transcripts)} 件 < {threshold} 件")
        return

    if not new_transcripts:
        _err(f"[skip] {channel_name}: 未処理のトランスクリプトがありません（{len(all_transcripts)} 件処理済み）")
        return

    _err(f"[summarize] {channel_name}: {len(new_transcripts)} 件を処理（合計 {len(all_transcripts)} 件中）")

    summary_path = SUMMARIES_DIR / f"{_sanitize(channel_name)}.md"
    is_new = not summary_path.exists()
    done_count = 0

    for i, t in enumerate(new_transcripts, 1):
        _err(f"  [{i}/{len(new_transcripts)}] {t.stem}")
        try:
            transcript_text = t.read_text(encoding="utf-8")
            video_count = len(processed) + i
            _update_summary(channel_name, transcript_text, t.stem, api_key, video_count)
            _copy_file_to_drive(summary_path)
            processed.add(t.name)
            _save_processed(channel_name, processed)
            done_count += 1
        except Exception as e:
            _err(f"  [error] {t.name}: {e}")

    _err(f"[done] {channel_name}: サマリー更新完了")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    api_key = os.environ.get("GEMINI_API_KEY")
    local_url = os.environ.get("LOCAL_LLM_URL")
    if not api_key and not local_url:
        _err("[error] GEMINI_API_KEY または LOCAL_LLM_URL が設定されていません")
        _err("  .env ファイルに GEMINI_API_KEY または LOCAL_LLM_URL を設定してください")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="チャンネルのサマリーをGeminiで生成・更新する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
ローカルLLM（Ollama）を使う場合:
  .env に LOCAL_LLM_URL と LOCAL_LLM_MODEL を設定する。
  Mac: LOCAL_LLM_URL=http://<Windows-TailscaleIP>:11434（トンネル不要）
  WSL: LOCAL_LLM_URL=http://localhost:11434（トンネル不要）
  未設定または接続失敗時は Gemini にフォールバック。

examples:
  # 特定チャンネルのサマリー更新
  python summarize.py "メンタリストDAIGO" --threshold 20

  # 全チャンネル一括（未処理20本未満はスキップ）
  python summarize.py all --threshold 20

  # 処理済みを無視して全件再生成
  python summarize.py "メンタリストDAIGO" --force
""",
    )
    parser.add_argument(
        "target",
        help="チャンネル名、または 'all'（全チャンネル）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="処理済みを無視して全トランスクリプトを再処理",
    )
    parser.add_argument(
        "--threshold", type=int, default=0,
        help="未処理ファイルがこの件数未満のチャンネルはスキップ（0=常に実行）",
    )
    args = parser.parse_args()

    if args.target == "all":
        channels = _load_channels()
        if not channels:
            _err("[warn] channels.txt にチャンネルが登録されていません")
            sys.exit(0)
        for name in channels:
            _summarize_channel(name, api_key, args.force, args.threshold)
    else:
        _summarize_channel(args.target, api_key, args.force, args.threshold)


if __name__ == "__main__":
    main()
