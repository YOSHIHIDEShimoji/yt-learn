#!/usr/bin/env python3
"""
一括修正: transcripts/ 以下の .md ファイルで
「ポイnt」「ポイnts」「ポイnto」などの誤記を「ポイント」に修正する。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
TRANSCRIPTS = ROOT / "transcripts"

PATTERN = re.compile(r"ポイn[^\s、。\n]*")


def fix_file(path: Path, dry_run: bool) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    new_text, count = PATTERN.subn("ポイント", text)
    if count:
        print(f"  {path.relative_to(ROOT)}: {count} 箇所")
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
    return count


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("[dry-run] 変更はしません")

    total_files = 0
    total_fixes = 0
    for md in sorted(TRANSCRIPTS.rglob("*.md")):
        if md.name.startswith("_"):
            continue
        n = fix_file(md, dry_run)
        if n:
            total_files += 1
            total_fixes += n

    print(f"\n合計: {total_files} ファイル / {total_fixes} 箇所" + (" (dry-run)" if dry_run else " を修正しました"))


if __name__ == "__main__":
    main()
