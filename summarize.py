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
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
SUMMARIES_DIR = BASE_DIR / "summaries"
CHANNELS_FILE = BASE_DIR / "channels.txt"

GEMINI_MODEL = "gemini-2.5-flash-lite"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _sanitize(name: str) -> str:
    import re
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip()[:200]


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
        name, url = line.split("|", 1)
        channels[name.strip()] = url.strip()
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


# ── Gemini 要約 ───────────────────────────────────────────────────────────────

def _update_summary(channel_name: str, transcript: str, video_title: str, api_key: str, video_count: int) -> None:
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

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    summary_path.write_text(response.text.strip() + "\n", encoding="utf-8")
    _err(f"  → サマリー更新: {summary_path.name}")


# ── チャンネル処理 ────────────────────────────────────────────────────────────

def _summarize_channel(channel_name: str, api_key: str, force: bool = False) -> None:
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

    if not new_transcripts:
        _err(f"[skip] {channel_name}: 未処理のトランスクリプトがありません（{len(all_transcripts)} 件処理済み）")
        return

    _err(f"[summarize] {channel_name}: {len(new_transcripts)} 件を処理（合計 {len(all_transcripts)} 件中）")

    for i, t in enumerate(new_transcripts, 1):
        _err(f"  [{i}/{len(new_transcripts)}] {t.stem}")
        try:
            transcript_text = t.read_text(encoding="utf-8")
            video_count = len(processed) + i
            _update_summary(channel_name, transcript_text, t.stem, api_key, video_count)
            processed.add(t.name)
            _save_processed(channel_name, processed)
        except Exception as e:
            _err(f"  [error] {t.name}: {e}")

    _err(f"[done] {channel_name}: サマリー更新完了")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _err("[error] GEMINI_API_KEY が設定されていません")
        _err("  .env ファイルに GEMINI_API_KEY=your_key を記述するか、環境変数を設定してください")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="チャンネルのサマリーをGeminiで生成・更新する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python summarize.py メンタリストDAIGO
  python summarize.py all
  python summarize.py メンタリストDAIGO --force
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
    args = parser.parse_args()

    if args.target == "all":
        channels = _load_channels()
        if not channels:
            _err("[warn] channels.txt にチャンネルが登録されていません")
            sys.exit(0)
        for name in channels:
            _summarize_channel(name, api_key, args.force)
    else:
        _summarize_channel(args.target, api_key, args.force)


if __name__ == "__main__":
    main()
