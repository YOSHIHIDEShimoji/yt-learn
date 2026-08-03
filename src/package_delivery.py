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
import html
import json
import re
import shutil
import sys
import urllib.parse
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


def _collect_categories(channel_name: str, entries: list[dict]) -> list[dict]:
    """カテゴリごとに {name, body, members} を集める。md 出力と HTML 出力の共通の元。

    categorize.py はタイトルしか知らないので連番を書けない。番号の正は
    ここ（納品時の並び）なので、所属動画の一覧はこちらで組み直す。
    """
    src_dir = SUMMARIES_DIR / _sanitize(channel_name)
    if not src_dir.exists():
        return []
    assign_path = CACHE_DIR / f"{_sanitize(channel_name)}_categories.json"
    assign = json.loads(assign_path.read_text(encoding="utf-8")) if assign_path.exists() else {}
    by_filename = {e["md"].name: e for e in entries}

    # 並びは index.md（categorize.py が提案順で書く）に従う。ファイル名順にすると
    # 「読む順」として意味を持つ並びが失われる。index が無ければ名前順で妥協する
    order = []
    idx_path = src_dir / "index.md"
    if idx_path.exists():
        order = re.findall(r"^- \[.*?\]\((.+?)\.md\)", idx_path.read_text(encoding="utf-8"),
                           re.MULTILINE)
    files = [p for p in src_dir.glob("*.md") if p.stem != "index"]
    rank = {name: i for i, name in enumerate(order)}
    files.sort(key=lambda p: (rank.get(p.stem, len(rank)), p.stem))

    cats = []
    for src in files:
        text = src.read_text(encoding="utf-8")
        head, sep, _tail = text.partition("## このカテゴリの動画")
        members = [by_filename[f] for f, c in assign.items()
                   if _sanitize(c) == src.stem and f in by_filename]
        members.sort(key=lambda e: e["num"])
        cats.append({"name": src.stem, "file": src, "head": head,
                     "has_member_section": bool(sep), "members": members})
    return cats


def _renumber_categories(out_dir: Path, channel_name: str, entries: list[dict]) -> int:
    """カテゴリ別まとめを .md でコピーし、末尾の動画一覧に連番を振り直す。"""
    src_dir = SUMMARIES_DIR / _sanitize(channel_name)
    if not src_dir.exists():
        _err(f"[warn] カテゴリ別まとめがありません: {src_dir}（categorize.py 未実行）")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for cat in _collect_categories(channel_name, entries):
        text = cat["head"]
        if cat["has_member_section"]:
            lines = [f"- {e['num']:03d}  {e['title']}" for e in cat["members"]]
            text = cat["head"] + "## このカテゴリの動画\n\n" + "\n".join(lines) + "\n"
        (out_dir / cat["file"].name).write_text(text, encoding="utf-8")
        copied += 1
    idx = src_dir / "index.md"
    if idx.exists():
        shutil.copy2(idx, out_dir / "index.md")
        copied += 1
    return copied


_CSS = """
:root { color-scheme: light dark; --fg:#1c1c1e; --bg:#fff; --muted:#6b6b70;
        --line:#e3e3e6; --accent:#0b5fff; --card:#f7f7f8; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8ea; --bg:#141416; --muted:#9a9aa0;
          --line:#2c2c30; --accent:#7aa2ff; --card:#1c1c1f; }
}
* { box-sizing: border-box; }
body { margin:0; padding:0 1rem 4rem; background:var(--bg); color:var(--fg);
       font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
       line-height:1.75; -webkit-text-size-adjust:100%; }
.wrap { max-width:44rem; margin:0 auto; }
header { padding:2rem 0 1rem; border-bottom:1px solid var(--line); }
h1 { font-size:1.5rem; margin:0 0 .3rem; }
h2 { font-size:1.25rem; margin:2.5rem 0 .75rem; padding-top:.5rem; }
h3 { font-size:1rem; margin:2rem 0 .4rem; }
.sub { color:var(--muted); font-size:.875rem; margin:0; }
nav { background:var(--card); border-radius:.75rem; padding:1rem 1.25rem; margin:1.5rem 0; }
nav ol { margin:.25rem 0 0; padding-left:1.25rem; }
nav a, .vid-list a { color:var(--accent); text-decoration:none; }
nav a:hover, .vid-list a:hover { text-decoration:underline; }
ul { padding-left:1.25rem; }
li { margin:.4rem 0; }
.vid-list { list-style:none; padding:0; margin:.5rem 0 0; }
.vid-list li { margin:.3rem 0; font-size:.9375rem; }
.num { display:inline-block; min-width:2.6rem; color:var(--muted);
       font-variant-numeric:tabular-nums; }
.card { border:1px solid var(--line); border-radius:.75rem; padding:1rem 1.25rem;
        margin:1rem 0; }
.meta { color:var(--muted); font-size:.8125rem; margin:.2rem 0 .6rem; }
.meta a { color:var(--accent); text-decoration:none; }
.tip { background:var(--card); border-radius:.75rem; padding:1rem 1.25rem;
       font-size:.9375rem; }
.top { font-size:.8125rem; color:var(--muted); text-decoration:none; }
hr { border:0; border-top:1px solid var(--line); margin:3rem 0 0; }
"""


def _bullets_to_html(text: str) -> str:
    """LLM が出す「- 」始まりの箇条書きを HTML に起こす。装飾記法は想定しない。"""
    out, buf = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("- ", "・")):
            buf.append(f"<li>{html.escape(line.lstrip('-・ ').strip())}</li>")
            continue
        if buf:
            out.append("<ul>" + "".join(buf) + "</ul>")
            buf = []
        if line and not line.startswith(("#", "---", "チャンネル:")):
            out.append(f"<p>{html.escape(line)}</p>")
    if buf:
        out.append("<ul>" + "".join(buf) + "</ul>")
    return "\n".join(out)


def _write_html(out_path: Path, channel_name: str, entries: list[dict],
                cats: list[dict], with_videos: bool) -> None:
    """まとめ全体を1枚の自己完結 HTML にする。

    飛行機で読む前提なので、外部リソースを一切参照しない（オフラインで開ける）。
    .md はスマホの標準アプリで読めないため、こちらを主たる読み物にする。
    カテゴリ別まとめと動画ごとの要点を同一ファイルに入れ、番号のリンクで
    行き来できるようにしてある。全文だけは分量が大きいので .md のまま外に置く。
    """
    esc = html.escape
    p = []
    p.append(f"<header><h1>{esc(channel_name)} まとめ</h1>"
             f"<p class='sub'>対象 {len(entries)} 本／{len(cats)} カテゴリ"
             f"　このページはオフラインで読めます</p></header>")
    p.append("<div class='tip'>まず<strong>カテゴリ別まとめ</strong>を読んでください。"
             "気になった動画の番号を押すと、その動画の要点に飛べます。"
             "探し物はブラウザの検索（PCなら Ctrl/⌘+F、スマホならメニューの"
             "「ページ内を検索」）が使えます。</div>")

    p.append("<nav><strong>カテゴリ</strong><ol>")
    for i, c in enumerate(cats):
        p.append(f"<li><a href='#c{i}'>{esc(c['name'])}</a>"
                 f"<span class='sub'>（{len(c['members'])}本）</span></li>")
    p.append("</ol></nav>")

    for i, c in enumerate(cats):
        p.append(f"<h2 id='c{i}'>{esc(c['name'])}</h2>")
        p.append(_bullets_to_html(c["head"]))
        if c["members"]:
            p.append("<h3>このカテゴリの動画</h3><ul class='vid-list'>")
            for e in c["members"]:
                p.append(f"<li><a href='#v{e['num']:03d}'>"
                         f"<span class='num'>{e['num']:03d}</span>"
                         f"{esc(e['title'])}</a></li>")
            p.append("</ul>")
        p.append("<p><a class='top' href='#'>↑ 先頭へ</a></p>")

    p.append("<hr><h2 id='videos'>動画ごとの要点</h2>")
    for e in entries:
        points = _split_transcript(e["md"].read_text(encoding="utf-8"))[0]
        if not points:
            continue
        p.append(f"<div class='card' id='v{e['num']:03d}'>")
        p.append(f"<h3>{e['num']:03d}　{esc(e['title'])}</h3>")
        meta = [esc(e["upload_date"] or "投稿日不明")]
        if with_videos:
            href = urllib.parse.quote(f"4_動画/{e['stem']}.mp4")
            meta.append(f"<a href='{href}'>動画を見る</a>")
        p.append(f"<p class='meta'>{'　／　'.join(meta)}</p>")
        p.append(_bullets_to_html(points))
        p.append("<p><a class='top' href='#'>↑ 先頭へ</a></p></div>")

    out_path.write_text(
        "<!doctype html>\n<html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(channel_name)} まとめ</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{''.join(p)}</div></body></html>\n",
        encoding="utf-8")


def _write_readme(out_path: Path, channel_name: str, entries: list[dict],
                  n_points: int, n_videos: int, mb: int, n_categories: int) -> None:
    lines = [
        f"# {channel_name} まとめ",
        "",
        f"対象 {len(entries)} 本。",
        "",
        "## どこから読むか",
        "",
        "**`まとめ.html` をブラウザで開いてください。それだけで足ります。**",
        "インターネットに繋がっていなくても開けます（飛行機の中でも読めます）。",
        "",
        "話題ごとのまとめと、動画1本ごとの要点が全部入っていて、",
        "番号を押すと行き来できます。探し物はブラウザの検索機能が使えます。",
        "",
        "下のフォルダは、元データが欲しいとき用です。",
        "",
        "## フォルダの中身",
        "",
        "- `まとめ.html` — **まずここ。これだけ開けばいい**",
        f"- `1_カテゴリ別まとめ/` — {n_categories} ファイル。html と同じ内容のテキスト版",
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

    cats = _collect_categories(args.channel, entries)
    _write_html(out_root / "まとめ.html", args.channel, entries, cats, n_videos > 0)
    _write_readme(out_root / "00_はじめに.md", args.channel, entries,
                  n_points, n_videos, mb, len(cats))

    _err(f"[done] {out_root}")
    _err(f"  まとめ.html / カテゴリ {len(cats)} / 要点 {n_points} / 全文 {len(entries)} / 動画 {n_videos}（{mb}MB）")


if __name__ == "__main__":
    main()
