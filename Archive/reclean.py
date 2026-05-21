#!/usr/bin/env python3
"""tiny など低精度モデルで作られた .md を削除し _index.json のフラグをリセットする。

使い方:
  python reclean.py                  # tiny モデルの全ファイルを処理
  python reclean.py --dry-run        # 対象確認のみ
  python reclean.py --channel 年収チャンネル
  python reclean.py --model tiny     # デフォルト: tiny のみ
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

KEEP_MODELS = {"large-v3", "large-v3-turbo"}


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _get_model(content: str) -> str | None:
    m = re.search(r"^モデル: (.+)", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def _get_url(content: str) -> str | None:
    m = re.search(r"^URL: (.+)", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def _trash(path: Path) -> None:
    if shutil.which("trash"):
        subprocess.run(["trash", str(path)], check=True)
    else:
        path.unlink()


def _process_channel(channel_dir: Path, target_models: set[str], dry_run: bool) -> tuple[int, int]:
    """削除数・スキップ数を返す。"""
    index_path = channel_dir / "_index.json"
    if not index_path.exists():
        return 0, 0

    index: dict = json.loads(index_path.read_text(encoding="utf-8"))
    deleted = 0
    skipped = 0

    for md_path in sorted(channel_dir.glob("*.md")):
        content = md_path.read_text(encoding="utf-8")
        model = _get_model(content)

        if model is None:
            _err(f"  [skip] モデル行なし: {md_path.name}")
            skipped += 1
            continue

        if model in KEEP_MODELS:
            continue

        if target_models and model not in target_models:
            continue

        url = _get_url(content)
        vid_id = _extract_video_id(url) if url else None

        if dry_run:
            _err(f"  [dry-run] モデル={model} vid={vid_id or '不明'}: {md_path.name}")
            deleted += 1
            continue

        # _index.json から除去
        if vid_id and vid_id in index:
            del index[vid_id]
            _err(f"  [index] {vid_id} を削除")

        # .md を削除
        _trash(md_path)
        _err(f"  [deleted] {md_path.name}")
        deleted += 1

    if not dry_run and deleted > 0:
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _err(f"  [index] {index_path.name} を更新")

    return deleted, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="低精度モデルの .md を削除してフラグをリセット")
    parser.add_argument("--channel", metavar="NAME", help="特定チャンネルのみ処理")
    parser.add_argument("--dry-run", action="store_true", help="変更せず対象一覧を表示")
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="tiny",
        help="削除対象モデル名（カンマ区切りで複数指定可。デフォルト: tiny）",
    )
    args = parser.parse_args()

    target_models = {m.strip() for m in args.model.split(",")} if args.model else set()

    if args.channel:
        dirs = [TRANSCRIPTS_DIR / args.channel]
    else:
        dirs = sorted(p for p in TRANSCRIPTS_DIR.iterdir() if p.is_dir())

    total_deleted = 0
    total_skipped = 0

    label = f"対象モデル: {', '.join(sorted(target_models)) or '全モデル（large-v3系以外）'}"
    _err(f"[reclean] {label}" + (" (dry-run)" if args.dry_run else ""))

    for d in dirs:
        if not d.exists():
            _err(f"[error] ディレクトリが見つかりません: {d}")
            sys.exit(1)
        _err(f"[channel] {d.name}")
        deleted, skipped = _process_channel(d, target_models, dry_run=args.dry_run)
        total_deleted += deleted
        total_skipped += skipped

    action = "対象" if args.dry_run else "削除"
    _err(f"[reclean] 完了: {action} {total_deleted} 件 / スキップ {total_skipped} 件")


if __name__ == "__main__":
    main()
