#!/usr/bin/env python3
"""
fix_english_titles.py
_index.json に記録された英語タイトルを YouTube oEmbed API から取得した実際のタイトルで置換し、
対応する .md ファイルをリネームする。
- 元々英語タイトルの動画（3Blue1Brown, Fireship, TED 等）は取得後も英語ならそのまま維持
- move to Archive/ after run
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
TRANSCRIPTS = ROOT / "transcripts"

REQUEST_DELAY = 0.3   # 秒（oEmbed API レート制限対策）


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip()
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        name = encoded[:200].decode("utf-8", errors="ignore")
    return name


def has_cjk(text: str) -> bool:
    """CJK 文字（日本語・中国語・韓国語）が含まれるか判定"""
    return any(0x2E80 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF
               or 0xFF00 <= ord(c) <= 0xFFEF for c in text)


def fetch_oembed_title(video_id: str) -> str | None:
    """YouTube oEmbed API でタイトルを取得（認証不要）"""
    url = (
        f"https://www.youtube.com/oembed"
        f"?url=https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            return data.get("title")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # 動画削除済み
        print(f"    [warn] oEmbed HTTP {e.code} for {video_id}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    [warn] oEmbed error for {video_id}: {e}", file=sys.stderr)
        return None


def load_index(idx_path: Path) -> dict:
    with open(idx_path, encoding="utf-8") as f:
        return json.load(f)


def save_index(idx_path: Path, data: dict) -> None:
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("[fix_english_titles] DRY RUN — ファイルは変更しません")

    # 全チャンネルの _index.json を走査して英語タイトルの動画を収集
    candidates: list[tuple[Path, str, str, str]] = []

    for idx_path in sorted(TRANSCRIPTS.glob("**/_index.json")):
        data = load_index(idx_path)
        for vid_id, info in data.items():
            title = info.get("title", "")
            if not title:
                continue
            if not has_cjk(title):
                candidates.append((idx_path, vid_id, title, info.get("file", "")))

    print(f"[fix_english_titles] 英語タイトル動画: {len(candidates)} 件")

    # oEmbed API で実際のタイトルを取得
    renames: list[tuple[Path, str, str, str, Path | None, Path]] = []

    for i, (idx_path, vid_id, old_title, file_str) in enumerate(candidates):
        if i % 20 == 0:
            print(f"  checking {i+1}/{len(candidates)} ...", flush=True)

        yt_title = fetch_oembed_title(vid_id)
        time.sleep(REQUEST_DELAY)

        if not yt_title:
            continue
        if yt_title == old_title:
            continue
        if not has_cjk(yt_title):
            continue  # 取得後も英語 → 本来英語タイトルの動画

        ch_dir = idx_path.parent
        new_filename = f"{_sanitize(yt_title)}.md"
        new_file_path = ch_dir / new_filename
        old_file_path = Path(file_str) if file_str else None

        renames.append((idx_path, vid_id, old_title, yt_title,
                        old_file_path, new_file_path))

    print(f"\n[fix_english_titles] リネーム対象: {len(renames)} 件")
    if not renames:
        print("変更なし。終了します。")
        return

    # リネーム実行
    idx_updates: dict[Path, dict] = {}

    renamed = 0
    skipped = 0
    for idx_path, vid_id, old_title, new_title, old_file, new_file in renames:
        print(f"  [{vid_id}]")
        print(f"    旧: {old_title[:70]}")
        print(f"    新: {new_title[:70]}")

        if idx_path not in idx_updates:
            idx_updates[idx_path] = load_index(idx_path)

        data = idx_updates[idx_path]
        if vid_id not in data:
            print(f"    [warn] vid_id not in index — skip")
            skipped += 1
            continue

        # .md ファイルリネーム
        if old_file and old_file.exists():
            if new_file.exists() and new_file != old_file:
                print(f"    [warn] 新ファイル名が既存と衝突 — skip")
                skipped += 1
                continue
            if not dry_run:
                old_file.rename(new_file)
            print(f"    renamed: {old_file.name[:60]} → {new_file.name[:60]}")
        else:
            print(f"    [warn] .md ファイルが見つからない — index のみ更新")
            new_file = idx_path.parent / f"{_sanitize(new_title)}.md"

        data[vid_id]["title"] = new_title
        data[vid_id]["file"] = str(new_file)
        renamed += 1

    if not dry_run:
        for idx_path, data in idx_updates.items():
            save_index(idx_path, data)
            print(f"  saved: {idx_path.relative_to(ROOT)}")

    print(f"\n[fix_english_titles] 完了: renamed={renamed}, skipped={skipped}")


if __name__ == "__main__":
    main()
