#!/usr/bin/env python3
"""既存 .md ファイルの ## ポイント を新プロンプトで一括再生成する。

使い方:
  python repoint.py                        # 全ファイル
  python repoint.py --channel 年収チャンネル  # 1チャンネルのみ
  python repoint.py --dry-run              # 対象確認のみ
  python repoint.py --delay 2             # LLM間隔 2 秒
"""

import argparse
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"


def _load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    import os
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _repoint_file(md_path: Path, dry_run: bool) -> bool:
    from transcribe import _generate_core_summary

    content = md_path.read_text(encoding="utf-8")

    title_m = re.search(r"^# (.+)", content, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else ""

    raw_transcript = content.split("\n---\n", 1)[-1].strip()
    if not raw_transcript:
        _err(f"  [skip] 生テキストなし: {md_path.name}")
        return False

    if dry_run:
        has_points = "## ポイント" in content
        _err(f"  [dry-run] {'上書き' if has_points else '新規'}: {md_path.name}")
        return True

    summary, backend = _generate_core_summary(title=title, text=raw_transcript)
    if not summary:
        _err(f"  [error] LLM 失敗: {md_path.name}")
        return False

    # 既存の ## ポイント セクションを除去してから挿入
    content_stripped = re.sub(
        r"\n## ポイント\n(?:- .+\n?)*",
        "",
        content,
    )
    updated = re.sub(
        r"(処理日時: .+\n)(\n---\n)",
        rf"\1\n{summary}\n\2",
        content_stripped,
        count=1,
    )
    if updated == content_stripped:
        # フォールバック: ファイル先頭にメタブロックがない旧形式
        updated = f"{content_stripped.rstrip()}\n\n{summary}\n"

    md_path.write_text(updated, encoding="utf-8")
    _err(f"  [done] by {backend}: {md_path.name}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="既存 .md のポイントを一括再生成")
    parser.add_argument("--channel", metavar="NAME", help="特定チャンネルのみ処理")
    parser.add_argument("--dry-run", action="store_true", help="変更せず対象一覧を表示")
    parser.add_argument("--delay", type=float, default=1.0, metavar="SEC", help="LLMコール間の待機秒数（デフォルト 1.0）")
    args = parser.parse_args()

    _load_env()

    import os
    if not args.dry_run and not os.environ.get("LOCAL_LLM_URL") and not os.environ.get("GEMINI_API_KEY"):
        _err("[error] LOCAL_LLM_URL または GEMINI_API_KEY が未設定です")
        sys.exit(1)

    if args.channel:
        dirs = [TRANSCRIPTS_DIR / args.channel]
    else:
        dirs = sorted(p for p in TRANSCRIPTS_DIR.iterdir() if p.is_dir())

    md_files = []
    for d in dirs:
        if not d.exists():
            _err(f"[error] ディレクトリが見つかりません: {d}")
            sys.exit(1)
        md_files.extend(sorted(d.glob("*.md")))

    total = len(md_files)
    _err(f"[repoint] 対象: {total} ファイル" + (" (dry-run)" if args.dry_run else ""))

    ok = 0
    for i, md_path in enumerate(md_files, 1):
        _err(f"[{i}/{total}] {md_path.parent.name} / {md_path.name}")
        if _repoint_file(md_path, dry_run=args.dry_run):
            ok += 1
        if not args.dry_run and i < total:
            time.sleep(args.delay)

    _err(f"[repoint] 完了: {ok}/{total}")


if __name__ == "__main__":
    main()
