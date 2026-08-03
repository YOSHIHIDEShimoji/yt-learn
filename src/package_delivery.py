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

from summarize import (BASE_DIR, SUMMARIES_DIR, TRANSCRIPTS_DIR, _sanitize,
                       normalize_bullets)

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
    """本文を (ポイント, 全文) に分ける。ポイントが無ければ前半は空。

    「## ポイント」の次の行から本文区切り（---）までを丸ごと拾って正規化する。
    「- 」で始まる行の連続だけを見る書き方だと、LLM が見出しを綴り間違えた行
    （実測: `## ポイnts`）が1行挟まっただけで抽出が空になり、その動画が
    納品物から丸ごと消える。
    """
    _, sep, after = text.partition("## ポイント")
    if not sep:
        return "", text
    section = re.split(r"\n-{3,}\n", after, maxsplit=1)[0]
    bullets = normalize_bullets(section)
    return (f"## ポイント\n{bullets}" if bullets else ""), text


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


def _pick_videos(entries: list[dict], available: set, limit: int, order: str) -> list[dict]:
    """動画を同梱する対象を選ぶ。

    スマホで見る場合、全部入れると容量も再生時間も現実的でなくなる
    （89本＝約27時間。どの便でも見きれないうえ電池が持たない）。
    既定は再生数の多い順。_index.json に再生数が無い動画は投稿日順で後ろに回す。
    """
    pool = [e for e in entries if e["id"] in available]
    if order == "popular":
        pool.sort(key=lambda e: (e.get("view_count") or 0), reverse=True)
    # order == "date" のときは entries の並び（投稿が新しい順）をそのまま使う
    if limit > 0:
        pool = pool[:limit]
    return sorted(pool, key=lambda e: e["num"])


def _copy_videos(out_dir: Path, entries: list[dict], channel_name: str,
                 limit: int = 0, order: str = "popular") -> tuple[int, int, set]:
    """deliver/<channel>/videos/<id>.* を連番つきで並べ直す。

    戻り値: (件数, 合計MB, 同梱した動画の連番の集合)
    """
    src_dir = DELIVER_DIR / _sanitize(channel_name) / "videos"
    if not src_dir.exists():
        return 0, 0, set()
    by_id = {p.stem: p for p in src_dir.iterdir() if p.suffix in VIDEO_SUFFIXES}
    picked = _pick_videos(entries, set(by_id), limit, order)
    if not picked:
        return 0, 0, set()

    out_dir.mkdir(parents=True, exist_ok=True)
    total, nums = 0, set()
    for e in picked:
        src = by_id[e["id"]]
        shutil.copy2(src, out_dir / f"{e['stem']}{src.suffix}")
        total += src.stat().st_size
        nums.add(e["num"])
    return len(picked), total // (1024 * 1024), nums


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
        # categorize.py が「まず結論」を付けていれば分けて持つ（HTML で強調するため）
        lede, has_lede, detail = head.partition("## くわしく")
        if has_lede:
            lede = lede.partition("## まず結論")[2]
        else:
            lede, detail = "", head
        members = [by_filename[f] for f, c in assign.items()
                   if _sanitize(c) == src.stem and f in by_filename]
        members.sort(key=lambda e: e["num"])
        cats.append({"name": src.stem, "file": src, "head": head,
                     "lede": lede, "detail": detail,
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


# 索引タブの色。話題ごとに1色を割り当て、見出し・タブ・動画番号で同じ色を使う。
# 装飾ではなく「どの話題の話か」を色で示すための対応づけ。(明色, 暗色) の対。
_TAB_COLORS = [
    ("#3b4c8c", "#93a4e8"), ("#4a6b4a", "#8fbe8f"), ("#a4593f", "#e2977e"),
    ("#6d4260", "#c398b7"), ("#8a6a1f", "#dcc070"), ("#2f6b70", "#83c4ca"),
    ("#7a3f4e", "#d094a0"), ("#4d5a2e", "#b3c17e"), ("#4a4f7a", "#a0a5d8"),
    ("#8a5a2b", "#dba977"),
]

# 手帳の索引タブを模した見た目にしてある。89本ぶんの長い1ページを「話題ごとに
# 見出しを立てて、いつでも別の話題へ飛べる」形にするのが目的で、飾りではない。
# 明朝を見出し・ゴシックを本文に当てるのは日本語組版の定石だが、オフライン前提
# なので Web フォントは使わず OS 標準の明朝／ゴシックだけで組む。
# 機内は暗いことが多いのでダークモードは本気で作る。
_CSS = """
:root{color-scheme:light dark;
 --paper:#f2f1ee;--card:#fff;--ink:#1a1d21;--ink2:#5b5f66;--rule:#dedbd4;
 --shadow:0 1px 2px rgba(26,29,33,.06),0 6px 20px rgba(26,29,33,.05);
 --mincho:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP","Songti SC",serif;
 --gothic:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",YuGothic,"Noto Sans JP",sans-serif;
 --mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,monospace;}
/* 話題の色は要素に (明色, 暗色) の対をインラインで持たせ、--tab をここで選ぶ。
   インラインで --tab を直接指定すると、インラインの優先度が勝ってしまい
   ダークモードや印刷での切り替えが効かなくなる。 */
.tab,.cat,.card{--tab:var(--tab-l)}
@media (prefers-color-scheme:dark){:root{
 --paper:#15171a;--card:#1c1f23;--ink:#e7e5e1;--ink2:#989ba1;--rule:#2b2f34;
 --shadow:none;}
 /* 暗所では沈むので明るい対に差し替える */
 .tab,.cat,.card{--tab:var(--tab-d)}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--gothic);
 font-size:16px;line-height:1.9;-webkit-text-size-adjust:100%;
 font-feature-settings:"palt" 1;}
a{color:inherit}
:focus-visible{outline:2px solid currentColor;outline-offset:3px;border-radius:3px}

/* 表紙を全幅に置き、その下を「索引 | 本文」の2段にする。
   DOM順を 表紙→索引→本文 にしてあるので、紙に流したときもそのまま目次になる。 */
.shell{max-width:62rem;margin:0 auto;padding:0 1.25rem 5rem;
 display:grid;grid-template-columns:9.25rem minmax(0,1fr);
 grid-template-areas:"cover cover" "index body";gap:0 3rem;align-items:start}
.cover{grid-area:cover}
.index{grid-area:index}
.body{grid-area:body}

/* ── 索引タブ ── */
.index{position:sticky;top:1.5rem;padding-top:3rem}
.index-label{display:block;font-size:.6875rem;letter-spacing:.22em;color:var(--ink2);
 margin:0 0 .75rem .25rem}
.index ol{list-style:none;margin:0;padding:0}
.index li{margin:0 0 .25rem}
.tab{display:flex;align-items:baseline;gap:.5rem;text-decoration:none;
 padding:.5rem .7rem;border-left:3px solid var(--tab);border-radius:0 .4rem .4rem 0;
 background:var(--card);box-shadow:var(--shadow);transition:transform .12s ease}
.tab:hover{transform:translateX(3px)}
@media (prefers-reduced-motion:reduce){.tab:hover{transform:none}}
.tab-name{font-size:.8125rem;line-height:1.4;flex:1}
.tab-count{font-family:var(--mono);font-size:.6875rem;color:var(--ink2);
 font-variant-numeric:tabular-nums}
.tab-all{border-left-color:var(--ink2);margin-top:.75rem}

/* ── 表紙 ── */
.cover{padding:3.5rem 0 2.5rem;border-bottom:1px solid var(--rule)}
.eyebrow{font-size:.6875rem;letter-spacing:.22em;color:var(--ink2);margin:0 0 1rem}
.cover h1{font-family:var(--mincho);font-weight:600;letter-spacing:.03em;
 font-size:clamp(2.125rem,6vw,3.25rem);line-height:1.25;margin:0 0 1.25rem}
.standfirst{font-size:1.0625rem;margin:0 0 2rem;max-width:32em}
.how{border-left:2px solid var(--rule);padding-left:1.1rem;margin:0}
.how p{margin:0 0 .4rem;font-size:.875rem;color:var(--ink2);line-height:1.8}
.how p:last-child{margin-bottom:0}

/* ── 話題 ── */
.cat{padding-top:3.25rem;scroll-margin-top:1.5rem}
.cat-head{display:flex;align-items:center;gap:.75rem;margin:0 0 1.25rem}
.chip{width:.6rem;height:1.6rem;border-radius:.2rem;background:var(--tab);flex:none}
.cat h2{font-family:var(--mincho);font-weight:600;letter-spacing:.03em;
 font-size:clamp(1.4375rem,3.6vw,1.875rem);line-height:1.3;margin:0}
.cat-n{font-family:var(--mono);font-size:.75rem;color:var(--ink2);
 font-variant-numeric:tabular-nums}
.cat ul{padding-left:1.15rem;margin:0}
.cat li{margin:0 0 .7rem}
.cat li::marker{color:var(--tab)}

/* 「まず結論」— 読み返すときここだけ見れば思い出せる、という置き場 */
.lede{background:var(--card);border-left:3px solid var(--tab);border-radius:0 .5rem .5rem 0;
 padding:1.1rem 1.35rem;margin:0 0 1.75rem;box-shadow:var(--shadow)}
.lede-label{display:block;font-size:.6875rem;letter-spacing:.18em;color:var(--ink2);
 margin-bottom:.6rem}
.lede ul{padding-left:1.1rem;margin:0}
.lede li{margin:0 0 .5rem;font-weight:500}
.lede li:last-child{margin-bottom:0}
.detail-label{display:block;font-size:.6875rem;letter-spacing:.18em;color:var(--ink2);
 margin:0 0 .7rem}
/* 話題の中の小見出し。31本ぶんの買い物リストのような長い節を、スマホで
   スクロールしながらでも現在地がわかる粒度に割るためのもの */
.sub{font-family:var(--mincho);font-size:1.0625rem;font-weight:600;
 margin:1.9rem 0 .6rem;padding-left:.7rem;border-left:2px solid var(--tab)}
.sub:first-child{margin-top:0}

.roll{margin-top:2rem;border-top:1px solid var(--rule);padding-top:1.25rem}
.roll-label{font-size:.6875rem;letter-spacing:.18em;color:var(--ink2);margin:0 0 .75rem}
.roll ul{list-style:none;padding:0;margin:0}
.roll li{margin:0}
.roll a{display:flex;gap:.85rem;align-items:baseline;text-decoration:none;
 padding:.45rem .5rem;margin-left:-.5rem;border-radius:.4rem;font-size:.9375rem}
.roll a:hover{background:var(--card)}
.num{font-family:var(--mono);font-size:.75rem;color:var(--tab);
 font-variant-numeric:tabular-nums;flex:none;padding-top:.15rem}

/* ── 動画ごとの要点 ── */
.notes{padding-top:4rem;scroll-margin-top:1.5rem}
.notes-head{font-family:var(--mincho);font-weight:600;letter-spacing:.03em;
 font-size:clamp(1.4375rem,3.6vw,1.875rem);margin:0 0 .35rem}
.notes-sub{color:var(--ink2);font-size:.875rem;margin:0 0 1.5rem}
.card{background:var(--card);border-radius:.7rem;padding:1.35rem 1.5rem;
 margin:0 0 1rem;box-shadow:var(--shadow);scroll-margin-top:1.5rem;
 border-left:3px solid var(--tab)}
.card:target{outline:2px solid var(--tab);outline-offset:2px}
.card h3{font-size:1rem;line-height:1.6;margin:0 0 .5rem;font-weight:600;
 display:flex;gap:.75rem;align-items:baseline}
.card ul{padding-left:1.15rem;margin:.6rem 0 0}
.card li{margin:0 0 .55rem;font-size:.9375rem}
.card li::marker{color:var(--tab)}
.meta{font-family:var(--mono);font-size:.75rem;color:var(--ink2);margin:0}
.meta a{color:var(--tab);text-decoration:none;font-family:var(--gothic)}
.meta a:hover{text-decoration:underline}
.dot{opacity:.4;margin:0 .5rem}

/* ── 画面が狭いとき: 索引を上部の横スクロール帯にする ── */
@media (max-width:820px){
 .shell{grid-template-columns:minmax(0,1fr);gap:0;padding:0 1rem 4rem}
 .index{position:sticky;top:0;z-index:5;padding:.6rem 1rem;margin:0 -1rem;
  background:var(--paper);border-bottom:1px solid var(--rule);
  overflow-x:auto;-webkit-overflow-scrolling:touch}
 .index-label{display:none}
 .index ol{display:flex;gap:.4rem;width:max-content}
 .index li{margin:0}
 .tab{border-left:none;border-bottom:3px solid var(--tab);border-radius:.4rem .4rem 0 0;
  white-space:nowrap;padding:.4rem .7rem}
 .tab:hover{transform:none}
 .tab-all{border-bottom-color:var(--ink2);margin-top:0}
 .cover{padding:2.5rem 0 2rem}
 .cat,.notes{scroll-margin-top:3.75rem}
 .card{scroll-margin-top:3.75rem;padding:1.15rem 1.15rem}
}

/* ── 印刷 / PDF ──
   読むのはスマホなので、A4 ではなく文庫本ほどのページにする。
   画面幅にページを合わせたときに本文が読める大きさで出るのが目的。
   索引は固定をやめ、表紙の次に「目次」として1度だけ流す。 */
@media print{
 :root{--paper:#fff;--card:#fff;--ink:#15181c;--ink2:#5c6167;--rule:#d6d3cd;--shadow:none}
 .tab,.cat,.card{--tab:var(--tab-l)}
 @page{size:110mm 190mm;margin:11mm 10mm}
 body{font-size:10pt;line-height:1.85;background:#fff}
 .shell{display:block;max-width:none;padding:0;gap:0}

 /* 表紙と目次は1ページに同居させる。別ページに割ると、どちらも紙の1/3しか
    埋まらずスカスカな冊子になる。 */
 .cover{padding:8mm 0 7mm;border:none;break-after:auto}
 .cover h1{font-size:22pt;line-height:1.3;margin-bottom:7mm}
 .eyebrow{font-size:7.5pt;margin-bottom:5mm}
 .standfirst{font-size:10.5pt;margin-bottom:7mm}
 .how p{font-size:8.5pt}

 .index{position:static;padding:7mm 0 0;margin:0;background:none;
  border-top:1px solid var(--rule);overflow:visible;break-after:page}
 .index-label{display:block;font-size:8pt;margin-bottom:4mm}
 .index ol{display:block;width:auto}
 .index li{margin:0 0 1.5mm}
 .tab{display:flex;background:none;box-shadow:none;border-radius:0;
  border-left:2.5pt solid var(--tab);padding:1.5mm 3mm;break-inside:avoid}
 .tab-name{font-size:9.5pt}
 .tab-all{border-left-color:var(--ink2);margin-top:4mm}

 .cat{break-before:page;padding-top:0}
 .cat h2{font-size:15pt}
 .cat li{margin-bottom:2mm;break-inside:avoid}
 .sub{font-size:11.5pt;margin:6mm 0 2mm;break-after:avoid}
 .chip{height:6mm;width:2mm}
 /* 一覧そのものは改ページをまたいでよい（塊で送ると空きページができる）。
    ただし1件が途中で割れるのは防ぐ */
 .roll{margin-top:6mm;padding-top:4mm}
 .roll li{break-inside:avoid}
 .roll a{padding:.8mm 0;margin:0;font-size:9pt}
 .roll a:hover{background:none}

 /* まず結論の枠 */
 .lede{background:#f6f5f2;border-left:2.5pt solid var(--tab);border-radius:2pt;
  padding:4mm 5mm;margin:0 0 6mm;break-inside:avoid}
 .lede-label{font-size:7.5pt}
 .lede li{font-size:10pt;margin-bottom:2mm}

 .notes{break-before:page;padding-top:0}
 .notes-head{font-size:15pt;break-after:avoid}
 /* カードは改ページをまたいでよい。1枚まるごと avoid にすると、紙1枚に収まらない
    カード（要点が10個あると普通に超える）が丸ごと次ページへ送られ、そのぶんの
    余白が全ページに生まれる（実測: 72本で135ページ、半分が空白）。割れて困るのは
    カードではなく「見出しだけ取り残される」「要点1行が途中で切れる」の2つなので、
    そこだけ守る。.roll で同じ直し方をしている。 */
 .card{box-shadow:none;border:1px solid var(--rule);
  border-left:2.5pt solid var(--tab);border-radius:2pt;padding:4mm 4.5mm;margin-bottom:4mm}
 .card h3{font-size:10.5pt;break-after:avoid}
 .card .meta{break-after:avoid}
 .card li{font-size:9.5pt;margin-bottom:1.5mm;break-inside:avoid}
 .card:target{outline:none}
 a{text-decoration:none}
}
@media print and (max-width:820px){
 .index{position:static;margin:0;padding:0;border:none;background:none}
 .index ol{display:block;width:auto}
 .tab{border-bottom:none;border-left:2.5pt solid var(--tab);border-radius:0;
  white-space:normal}
}
"""


def _bullets_to_html(text: str) -> str:
    """「- 」始まりの箇条書きと「### 」の小見出しを HTML に起こす。

    小見出しを拾うのは「### 」だけに限る。壊れた見出し（実測: `## ポイnts`）を
    本文として出さないための「# 始まりは捨てる」という守りを緩めないため。
    """
    out, buf = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("- ", "・")):
            buf.append(f"<li>{html.escape(line.lstrip('-・ ').strip())}</li>")
            continue
        if buf:
            out.append("<ul>" + "".join(buf) + "</ul>")
            buf = []
        if line.startswith("### "):
            out.append(f"<h3 class='sub'>{html.escape(line[4:].strip())}</h3>")
            continue
        if line and not line.startswith(("#", "---", "チャンネル:")):
            out.append(f"<p>{html.escape(line)}</p>")
    if buf:
        out.append("<ul>" + "".join(buf) + "</ul>")
    return "\n".join(out)


def _write_html(out_path: Path, channel_name: str, entries: list[dict],
                cats: list[dict], video_nums=()) -> None:
    """まとめ全体を1枚の自己完結 HTML にする。

    飛行機で読む前提なので、外部リソースを一切参照しない（オフラインで開ける）。
    .md はスマホの標準アプリで読めないため、こちらを主たる読み物にする。
    カテゴリ別まとめと動画ごとの要点を同一ファイルに入れ、番号のリンクで
    行き来できるようにしてある。全文だけは分量が大きいので .md のまま外に置く。
    """
    esc = html.escape

    def tint(i):
        light, dark = _TAB_COLORS[i % len(_TAB_COLORS)]
        return f"--tab-l:{light};--tab-d:{dark}"

    # 動画番号にも所属話題の色を当て、タブ・見出し・番号を同じ色で結ぶ
    color_of = {e["num"]: tint(i) for i, c in enumerate(cats) for e in c["members"]}
    default_tint = "--tab-l:#5b5f66;--tab-d:#989ba1"

    nav = ["<aside class='index'><span class='index-label'>目次</span><ol>"]
    for i, c in enumerate(cats):
        nav.append(f"<li><a class='tab' style='{tint(i)}' href='#c{i}'>"
                   f"<span class='tab-name'>{esc(c['name'])}</span>"
                   f"<span class='tab-count'>{len(c['members'])}</span></a></li>")
    nav.append("<li><a class='tab tab-all' href='#notes'>"
               "<span class='tab-name'>動画ごとの要点</span>"
               f"<span class='tab-count'>{len(entries)}</span></a></li>")
    nav.append("</ol></aside>")

    cover = (
        "<header class='cover'>"
        "<p class='eyebrow'>持ち歩ける要点集</p>"
        f"<h1>{esc(channel_name)}</h1>"
        f"<p class='standfirst'>{len(entries)}本の動画を{len(cats)}つの話題に整理しました。"
        "動画を見なくても、言っていたことの要点だけ追えます。</p>"
        "<div class='how'>"
        "<p>話題ごとのまとめを読み、気になった動画は番号から要点へ。</p>"
        "<p>通信がなくても全部読めます。</p>"
        "</div></header>")

    p = ["<main class='body'>"]
    for i, c in enumerate(cats):
        p.append(f"<section class='cat' id='c{i}' style='{tint(i)}'>")
        p.append(f"<div class='cat-head'><span class='chip'></span>"
                 f"<h2>{esc(c['name'])}</h2>"
                 f"<span class='cat-n'>{len(c['members'])}本</span></div>")
        if c.get("lede", "").strip():
            p.append("<div class='lede'><span class='lede-label'>まず結論</span>"
                     + _bullets_to_html(c["lede"]) + "</div>")
            p.append("<span class='detail-label'>くわしく</span>")
        p.append(_bullets_to_html(c.get("detail") or c["head"]))
        if c["members"]:
            p.append("<div class='roll'><p class='roll-label'>この話題の動画</p><ul>")
            for e in c["members"]:
                p.append(f"<li><a href='#v{e['num']:03d}'>"
                         f"<span class='num'>{e['num']:03d}</span>"
                         f"<span>{esc(e['title'])}</span></a></li>")
            p.append("</ul></div>")
        p.append("</section>")

    p.append("<section class='notes' id='notes'>"
             "<h2 class='notes-head'>動画ごとの要点</h2>"
             "<p class='notes-sub'>投稿が新しい順。番号はフォルダ内のファイル名と対応します。</p>")
    for e in entries:
        points = _split_transcript(e["md"].read_text(encoding="utf-8"))[0]
        if not points:
            continue
        style = color_of.get(e["num"], default_tint)
        p.append(f"<article class='card' id='v{e['num']:03d}' style='{style}'>")
        p.append(f"<h3><span class='num'>{e['num']:03d}</span>"
                 f"<span>{esc(e['title'])}</span></h3>")
        meta = [esc(e["upload_date"] or "投稿日不明")]
        # 動画を同梱した回だけリンクを出す。入っていない動画にリンクを張ると
        # 押しても何も起きない（PDF でも同じ）
        if e["num"] in video_nums:
            href = urllib.parse.quote(f"4_動画/{e['stem']}.mp4")
            meta.append(f"<a href='{href}'>動画を見る</a>")
        p.append(f"<p class='meta'>{'<span class=dot>·</span>'.join(meta)}</p>")
        p.append(_bullets_to_html(points))
        p.append("</article>")
    p.append("</section></main>")

    out_path.write_text(
        "<!doctype html>\n<html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(channel_name)} まとめ</title><style>{_CSS}</style></head>"
        f"<body><div class='shell'>{cover}{''.join(nav)}{''.join(p)}</div></body></html>\n",
        encoding="utf-8")


# PDF は Chrome のヘッドレス印刷で作る。LaTeX を挟まないのは、画面用の CSS を
# そのまま紙面にも使えて二重管理にならないのと、日本語フォントの用意が要らないため。
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    # WSL には Linux 版が入っていないことが多い。パイプライン本体は WSL で動くので、
    # 最後の砦として Windows 側の Chrome を直接呼ぶ（下の _win_path でパスを渡す）
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
)


def _find_chrome() -> str | None:
    for c in _CHROME_CANDIDATES:
        if "/" in c:
            if Path(c).exists():
                return c
        elif shutil.which(c):
            return shutil.which(c)
    return None


def _win_path(p: Path) -> str | None:
    """WSL のパスを Windows 形式（\\\\wsl.localhost\\...）に直す。

    Windows 側の Chrome は Linux のパスを解釈できないので、入力の HTML も
    出力の PDF も変換して渡す必要がある。UNC 越しの読み書きは実測で通る。
    """
    import subprocess
    try:
        r = subprocess.run(["wslpath", "-w", str(p.resolve())],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


def _html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """HTML を PDF に変換する。Chrome が無ければ HTML だけ残して諦める。"""
    import subprocess
    chrome = _find_chrome()
    if not chrome:
        _err("[warn] Chrome が見つからないので PDF は作らない（HTML はできている）")
        return False

    if chrome.startswith("/mnt/"):          # WSL から Windows の Chrome を呼ぶ場合
        src, dst = _win_path(html_path), _win_path(pdf_path)
        if not src or not dst:
            _err("[warn] wslpath が使えないので PDF は作らない（HTML はできている）")
            return False
    else:
        src, dst = html_path.resolve().as_uri(), str(pdf_path)

    cmd = [chrome, "--headless", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer",          # 既定のURL・日付・ページ番号を出さない
           "--virtual-time-budget=20000",     # フォント適用を待つ
           f"--print-to-pdf={dst}", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        _err(f"[warn] PDF 生成に失敗（HTML はできている）: {e}")
        return False
    if r.returncode != 0 or not pdf_path.exists():
        tail = (r.stderr or "").strip().splitlines()
        _err(f"[warn] PDF 生成に失敗（HTML はできている）: {tail[-1] if tail else r.returncode}")
        return False
    return True


def _write_readme(out_path: Path, channel_name: str, entries: list[dict],
                  n_points: int, n_videos: int, mb: int, n_categories: int) -> None:
    lines = [
        f"# {channel_name} まとめ",
        "",
        f"対象 {len(entries)} 本。",
        "",
        "## どこから読むか",
        "",
        "**`まとめ.pdf` を開いてください。それだけで足ります。**",
        "スマホにそのまま入れて読めます。通信は要りません。",
        "",
        "話題ごとのまとめと、動画1本ごとの要点が全部入っています。",
        "",
        "`まとめ.html` は同じ内容のブラウザ版です。文字の大きさを変えたい、",
        "検索したい、というときはこちらのほうが快適です。",
        "",
        "下のフォルダは、元データが欲しいとき用です。",
        "",
        "## フォルダの中身",
        "",
        "- `まとめ.pdf` — **まずここ。これだけ開けばいい**",
        "- `まとめ.html` — 同じ内容のブラウザ版（検索が速い）",
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
    parser.add_argument("--video-limit", type=int, default=0, metavar="N",
                        help="同梱する動画の本数上限（0=制限なし）。"
                             "スマホに入れる場合は全部入れても見きれないので絞る")
    parser.add_argument("--video-order", choices=["popular", "date"], default="popular",
                        help="--video-limit で残す優先順位 (default: popular)")
    parser.add_argument("--no-pdf", action="store_true", help="PDF を作らない")
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
    n_videos, mb, video_nums = (0, 0, set()) if args.no_videos else \
        _copy_videos(out_root / "4_動画", entries, args.channel,
                     args.video_limit, args.video_order)

    cats = _collect_categories(args.channel, entries)
    html_path = out_root / "まとめ.html"
    _write_html(html_path, args.channel, entries, cats, video_nums)
    pdf_ok = False if args.no_pdf else _html_to_pdf(html_path, out_root / "まとめ.pdf")
    _write_readme(out_root / "00_はじめに.md", args.channel, entries,
                  n_points, n_videos, mb, len(cats))

    _err(f"[done] {out_root}")
    _err(f"  {'まとめ.pdf + ' if pdf_ok else ''}まとめ.html / カテゴリ {len(cats)} / "
         f"要点 {n_points} / 全文 {len(entries)} / 動画 {n_videos}（{mb}MB）")


if __name__ == "__main__":
    main()
