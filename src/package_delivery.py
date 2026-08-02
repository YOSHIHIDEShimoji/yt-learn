#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""処理済みの成果物を、そのまま人に渡せる1フォルダに組み立てる。

  <出力先>/
  ├── 00_はじめに.md         どこから読むかの案内
  ├── 1_カテゴリ別まとめ/     categorize.py の出力（これが本体）
  ├── 2_動画ごとの要点/       各動画の「## ポイント」だけ
  ├── 3_全文/                文字起こし全文（検索用）
  └── 4_動画/                360p の動画ファイル

2_ / 3_ / 4_ は同じゼロ埋め連番で対応する。カテゴリ別まとめの末尾にも同じ番号を
振り直すので、「まとめを読む → 番号を控える → 要点/動画を開く」が番号だけで辿れる。

番号は投稿日の新しい順（001 が最新）。チャンネルの /videos タブの並びと一致する。

使い方:
  python src/package_delivery.py "八田エミリの日常"
  python src/package_delivery.py "八田エミリの日常" --output ~/Desktop/納品 --no-videos
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from summarize import BASE_DIR, SUMMARIES_DIR, TRANSCRIPTS_DIR, _sanitize

DELIVER_DIR = BASE_DIR / "deliver"
CACHE_DIR = BASE_DIR / "cache"

# ファイル名は「連番_日付_タイトル」。ext4 の 255 バイト制限に対し、
# 接頭辞ぶんと拡張子ぶんの余白を残してタイトルを削る
TITLE_BYTES = 120

# 納品に含める動画コンテナ。transcribe._VIDEO_SUFFIXES と一致させること
# （片方だけ .webm を欠くと、正常に取れた動画が納品から黙って落ちる）
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _short(title: str) -> str:
    name = _sanitize(title)
    encoded = name.encode("utf-8")
    if len(encoded) > TITLE_BYTES:
        name = encoded[:TITLE_BYTES].decode("utf-8", errors="ignore")
    return name.strip()


def _stem(num: int, upload_date: str, title: str) -> str:
    parts = [f"{num:03d}"]
    if upload_date:
        parts.append(upload_date)
    parts.append(_short(title))
    return "_".join(parts)


def _load_entries(channel_name: str) -> list[dict]:
    """_index.json から納品対象を集め、投稿日の新しい順に番号を振る。

    投稿日が無いもの（この機能を入れる前に処理した動画）は末尾にまとめる。
    """
    index_path = TRANSCRIPTS_DIR / _sanitize(channel_name) / "_index.json"
    if not index_path.exists():
        _err(f"[error] インデックスがありません: {index_path}")
        return []
    index = json.loads(index_path.read_text(encoding="utf-8"))

    entries = []
    for vid_id, meta in index.items():
        md = Path(meta.get("file", ""))
        if not md.is_absolute():
            md = BASE_DIR / md
        if not md.exists():
            _err(f"[warn] 本文が見つからないので除外: {meta.get('title')}")
            continue
        entries.append({
            "id": vid_id,
            "title": meta.get("title") or vid_id,
            "url": meta.get("url", ""),
            "upload_date": meta.get("upload_date", ""),
            "md": md,
        })

    # 投稿日の降順。日付が無いものは比較できないので末尾へ回す
    dated = [e for e in entries if e["upload_date"]]
    undated = [e for e in entries if not e["upload_date"]]
    dated.sort(key=lambda e: e["upload_date"], reverse=True)
    ordered = dated + undated
    for n, e in enumerate(ordered, 1):
        e["num"] = n
        e["stem"] = _stem(n, e["upload_date"], e["title"])
    return ordered


def _split_transcript(text: str) -> tuple[str, str]:
    """本文を (ヘッダ+ポイント, 全文) に分ける。ポイントが無ければ前半は空。"""
    m = re.search(r"## ポイント\n((?:- .+\n?)+)", text)
    return (m.group(0).strip() if m else ""), text


def _write_points(out_dir: Path, entries: list[dict]) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for e in entries:
        text = e["md"].read_text(encoding="utf-8")
        points, _ = _split_transcript(text)
        if not points:
            continue
        body = f"# {e['title']}\n\n投稿日: {e['upload_date'] or '不明'}\nURL: {e['url']}\n\n{points}\n"
        (out_dir / f"{e['stem']}.md").write_text(body, encoding="utf-8")
        written += 1
    return written


def _write_full(out_dir: Path, entries: list[dict]) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        shutil.copy2(e["md"], out_dir / f"{e['stem']}.md")
    return len(entries)


def _copy_videos(out_dir: Path, entries: list[dict], channel_name: str) -> tuple[int, int]:
    """deliver/<channel>/videos/<id>.* を連番つきで並べ直す。戻り値: (件数, 合計MB)"""
    src_dir = DELIVER_DIR / _sanitize(channel_name) / "videos"
    if not src_dir.exists():
        return 0, 0
    out_dir.mkdir(parents=True, exist_ok=True)
    count, total = 0, 0
    by_id = {p.stem: p for p in src_dir.iterdir() if p.suffix in VIDEO_SUFFIXES}
    for e in entries:
        src = by_id.get(e["id"])
        if not src:
            continue
        shutil.copy2(src, out_dir / f"{e['stem']}{src.suffix}")
        count += 1
        total += src.stat().st_size
    return count, total // (1024 * 1024)


def _renumber_categories(out_dir: Path, channel_name: str, entries: list[dict]) -> int:
    """カテゴリ別まとめをコピーし、末尾の動画一覧に連番を振り直す。

    categorize.py はタイトルしか知らないので番号を書けない。番号の正は
    ここ（納品時の並び）なので、コピーのタイミングで差し替える。
    """
    src_dir = SUMMARIES_DIR / _sanitize(channel_name)
    if not src_dir.exists():
        _err(f"[warn] カテゴリ別まとめがありません: {src_dir}（categorize.py 未実行）")
        return 0

    assign_path = CACHE_DIR / f"{_sanitize(channel_name)}_categories.json"
    assign = json.loads(assign_path.read_text(encoding="utf-8")) if assign_path.exists() else {}
    # 分類キャッシュのキーは transcripts のファイル名。そこから連番へ橋渡しする
    by_filename = {e["md"].name: e for e in entries}

    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(src_dir.glob("*.md")):
        text = src.read_text(encoding="utf-8")
        head, sep, _tail = text.partition("## このカテゴリの動画")
        # index.md にはこの節が無いのでそのままコピーされる
        if sep:
            members = [by_filename[f] for f, c in assign.items()
                       if _sanitize(c) == src.stem and f in by_filename]
            members.sort(key=lambda e: e["num"])
            lines = [f"- {e['num']:03d}  {e['title']}" for e in members]
            text = head + "## このカテゴリの動画\n\n" + "\n".join(lines) + "\n"
        (out_dir / src.name).write_text(text, encoding="utf-8")
        copied += 1
    return copied


def _write_readme(out_path: Path, channel_name: str, entries: list[dict],
                  n_points: int, n_videos: int, mb: int, n_categories: int) -> None:
    lines = [
        f"# {channel_name} まとめ",
        "",
        f"対象 {len(entries)} 本。",
        "",
        "## どこから読むか",
        "",
        "**1_カテゴリ別まとめ/ だけ読めば足ります。** 服装なら服装、恋愛なら恋愛と、",
        "全動画の内容を話題ごとに1ファイルに統合してあります。",
        "",
        "気になった話題が出てきたら、そのカテゴリの末尾にある番号を控えてください。",
        "同じ番号のファイルが 2_ 3_ 4_ にあります。",
        "",
        "## フォルダの中身",
        "",
        f"- `1_カテゴリ別まとめ/` — {n_categories} ファイル。**まずここ**",
        f"- `2_動画ごとの要点/` — {n_points} ファイル。1本あたり10行程度の要点",
        f"- `3_全文/` — {len(entries)} ファイル。文字起こし全文。読む用ではなく検索用",
    ]
    if n_videos:
        lines.append(f"- `4_動画/` — {n_videos} ファイル / 約{mb}MB。360p")
    lines += [
        "",
        "## 番号について",
        "",
        "投稿日の新しい順に 001 から振ってあります。ファイル名の日付は投稿日です。",
        "",
        "## 入っていないもの",
        "",
        "メンバーシップ限定の動画は取得できないため含まれていません。",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="処理済みの成果物を1つの納品フォルダに組み立てる",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python src/package_delivery.py "八田エミリの日常"
  python src/package_delivery.py "八田エミリの日常" --output ~/Desktop/納品 --no-videos
""",
    )
    parser.add_argument("channel", help="チャンネル名（transcripts/ 配下のディレクトリ名）")
    parser.add_argument("--output", help="出力先（省略時は deliver/<channel>/納品）")
    parser.add_argument("--no-videos", action="store_true", help="動画ファイルを含めない")
    args = parser.parse_args()

    entries = _load_entries(args.channel)
    if not entries:
        _err("[error] 納品対象が0件です")
        sys.exit(1)

    out_root = Path(args.output).expanduser() if args.output else \
        DELIVER_DIR / _sanitize(args.channel) / "納品"
    out_root.mkdir(parents=True, exist_ok=True)

    n_cat = _renumber_categories(out_root / "1_カテゴリ別まとめ", args.channel, entries)
    n_points = _write_points(out_root / "2_動画ごとの要点", entries)
    _write_full(out_root / "3_全文", entries)
    n_videos, mb = (0, 0) if args.no_videos else \
        _copy_videos(out_root / "4_動画", entries, args.channel)

    _write_readme(out_root / "00_はじめに.md", args.channel, entries,
                  n_points, n_videos, mb, n_cat)

    _err(f"[done] {out_root}")
    _err(f"  カテゴリ別まとめ {n_cat} / 要点 {n_points} / 全文 {len(entries)} / 動画 {n_videos}（{mb}MB）")


if __name__ == "__main__":
    main()
