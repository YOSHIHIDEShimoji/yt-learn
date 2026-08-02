#!/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python
"""トランスクリプトをカテゴリ別に分類し、カテゴリごとのまとめを生成する（Ollama専用）。

summarize.py と分けてある理由:
  summarize.py は「1チャンネル = 1ファイル」の逐次追記型で、動画1本ごとに既存サマリー
  全文をLLMに再投入して全体を書き直させる。数十本を超えると呼び出し回数も1回あたりの
  入力長も膨らんで破綻するため、100本規模の一括処理には転用できない。
  こちらは分類 → 集約の2パス構成にして、動画数に対して線形に収まるようにしてある。

  パス0: 全タイトルからカテゴリ候補を抽出（--categories で手動指定も可）
  パス1: 各動画を1カテゴリに割り当ててキャッシュ（再実行で取り直さない）
  パス2: カテゴリごとに所属動画の「## ポイント」を map-reduce でまとめる

使い方:
  python src/categorize.py "八田エミリの日常"
  python src/categorize.py "八田エミリの日常" --categories "服装,恋愛,マインド,その他"
  python src/categorize.py "八田エミリの日常" --dry-run    # 分類結果だけ見る
"""

import argparse
import atexit
import json
import sys
from datetime import date, datetime
from pathlib import Path

# 同じ src/ にある summarize.py の純粋なヘルパーを再利用する
# （import 時の副作用は無い。main() は __main__ ガードの内側）
from summarize import (
    BASE_DIR,
    SUMMARIES_DIR,
    TRANSCRIPTS_DIR,
    _copy_file_to_drive,
    _extract_points,
    _is_timeout_error,
    _load_env,
    _sanitize,
)

CACHE_DIR = BASE_DIR / "cache"

_log_file = None


# ログは summarize と分ける。portal が logs/summarize/*.log の "[N/M]" 行を
# summarize の進捗として解釈するため、同じ場所に書くと STATUS 表示が壊れる。
def _setup_log() -> None:
    global _log_file
    log_dir = BASE_DIR / "logs" / "categorize"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"categorize_{date.today().strftime('%Y%m%d')}.log"
    _log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    atexit.register(_teardown_log)
    _log_write(f"=== 開始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {'=' * 30}")


def _teardown_log() -> None:
    global _log_file
    if _log_file:
        _log_write(f"[session-end] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        _log_file.close()
        _log_file = None


def _log_write(msg: str) -> None:
    if _log_file:
        print(msg, file=_log_file)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)
    _log_write(msg)

# 1回のプロンプトに詰め込む「## ポイント」の合計文字数の上限。
# qwen2.5:14b を num_ctx=8192 で動かす前提で、出力ぶんの余白を残して保守的に取る。
CHUNK_CHARS = 4000
NUM_CTX = 8192

# LLM が候補を出せなかった場合の保険。男磨き系チャンネルを想定した最小構成。
FALLBACK_CATEGORIES = ["服装・コーデ", "恋愛・モテ", "マインド", "見た目・グルーミング", "その他"]


def _call_llm(prompt: str, base_url: str, model: str) -> str | None:
    """summarize._call_ollama と同じだが num_ctx を明示する。

    Ollama の既定コンテキストは短く、カテゴリ集約のような長い入力では
    黙って前半を切り捨てられる。切り捨ては出力を見ても気づけないので明示する。
    """
    import urllib.request
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_ctx": NUM_CTX},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("response") or "").strip() or None


# ── キャッシュ ────────────────────────────────────────────────────────────────

def _defs_path(channel_name: str) -> Path:
    return CACHE_DIR / f"{_sanitize(channel_name)}_category_defs.json"


def _assign_path(channel_name: str) -> Path:
    return CACHE_DIR / f"{_sanitize(channel_name)}_categories.json"


def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 入力 ──────────────────────────────────────────────────────────────────────

def _load_items(channel_name: str) -> list[dict]:
    """チャンネルのトランスクリプトから {file, title, points} を集める。

    「## ポイント」が無いものは分類も集約もできないので落とす
    （transcribe.py 側で Ollama が失敗した動画がこれに当たる）。
    """
    ch_dir = TRANSCRIPTS_DIR / _sanitize(channel_name)
    if not ch_dir.exists():
        _err(f"[error] トランスクリプトがありません: {ch_dir}")
        return []
    items, skipped = [], 0
    for p in sorted(ch_dir.glob("*.md")):
        points = _extract_points(p.read_text(encoding="utf-8"))
        if not points:
            skipped += 1
            continue
        items.append({"file": p.name, "title": p.stem, "points": points})
    if skipped:
        _err(f"[warn] 「## ポイント」が無い {skipped} 件を除外した")
    return items


# ── パス0: カテゴリ候補の抽出 ─────────────────────────────────────────────────

def _propose_categories(channel_name: str, items: list[dict],
                        base_url: str, model: str) -> list[str]:
    titles = "\n".join(f"- {it['title']}" for it in items)
    # タイトルだけなら100本でも数千文字に収まるので、ここは分割せず一度に見せる
    prompt = f"""以下はYouTubeチャンネル「{channel_name}」の動画タイトル一覧です。

{titles[:12000]}

## 指示
このチャンネルの動画を内容で分類するためのカテゴリを5〜8個提案してください。

ルール:
- 実際にタイトルに現れる話題だけを使う。存在しない話題のカテゴリを作らない
- どの動画も必ずどれか1つに入るようにする
- カテゴリ名は日本語で10文字以内
- 最後に必ず「その他」を入れる

出力形式: カテゴリ名を1行に1つ書くだけ。番号・記号・説明は一切不要。"""
    result = _call_llm(prompt, base_url, model)
    if not result:
        _err("[warn] カテゴリ候補を取得できなかった。既定のカテゴリを使う")
        return list(FALLBACK_CATEGORIES)
    cats = []
    for line in result.splitlines():
        name = line.strip().lstrip("-*0123456789. 　").strip()
        if name and len(name) <= 20 and name not in cats:
            cats.append(name)
    if len(cats) < 2:
        _err("[warn] カテゴリ候補が不十分。既定のカテゴリを使う")
        return list(FALLBACK_CATEGORIES)
    if "その他" not in cats:
        cats.append("その他")
    return cats


# ── パス1: 分類 ───────────────────────────────────────────────────────────────

def _normalize_category(raw: str, categories: list[str]) -> str:
    """LLM の出力を必ず既知のカテゴリ名に落とす。

    完全一致 → 部分一致 の順で寄せ、それでも決まらなければ「その他」。
    ここを緩めると存在しないカテゴリのファイルが生えるので厳格に扱う。
    """
    fallback = "その他" if "その他" in categories else categories[-1]
    lines = (raw or "").strip().splitlines()
    ans = lines[0].strip().lstrip("-*0123456789. 　").strip() if lines else ""
    # 空文字を部分一致に渡すと "" in c が全カテゴリに真になり、
    # 解析不能な応答が黙って先頭カテゴリに流れ込む
    if not ans:
        return fallback
    for c in categories:
        if ans == c:
            return c
    for c in categories:
        if c and (c in ans or ans in c):
            return c
    return fallback


def _classify(item: dict, categories: list[str], base_url: str, model: str) -> str:
    prompt = f"""次の動画を、下のカテゴリのどれか1つに分類してください。

カテゴリ:
{chr(10).join(f"- {c}" for c in categories)}

動画タイトル: {item['title']}

内容:
{item['points'][:1500]}

出力形式: カテゴリ名だけを1行で書く。説明・理由・記号は一切不要。"""
    return _normalize_category(_call_llm(prompt, base_url, model), categories)


def _classify_all(channel_name: str, items: list[dict], categories: list[str],
                  base_url: str, model: str, force: bool) -> dict:
    assign = {} if force else _load_json(_assign_path(channel_name), {})
    todo = [it for it in items if it["file"] not in assign]
    if not todo:
        _err(f"[classify] 全 {len(items)} 件が分類済み")
        return assign

    _err(f"[classify] {len(todo)} 件を分類（済み {len(assign)} 件）")
    for i, it in enumerate(todo, 1):
        try:
            cat = _classify(it, categories, base_url, model)
        except Exception as e:
            if _is_timeout_error(e):
                _err(f"  [warn] {it['title']}: タイムアウト → 未分類のまま次回に回す")
                continue
            _err(f"  [error] {it['title']}: {e}")
            continue
        assign[it["file"]] = cat
        _err(f"  [{i}/{len(todo)}] {cat} ← {it['title']}")
        if i % 10 == 0:
            _save_json(_assign_path(channel_name), assign)
    _save_json(_assign_path(channel_name), assign)
    return assign


# ── パス2: カテゴリごとの集約 ─────────────────────────────────────────────────

def _chunk_items(items: list[dict]) -> list[list[dict]]:
    """「## ポイント」の合計が CHUNK_CHARS に収まるようまとめる。

    1本だけで上限を超える動画もあるため、必ず1本は入れてから判定する
    （空チャンクを作ると集約が空振りする）。
    """
    chunks, cur, size = [], [], 0
    for it in items:
        n = len(it["points"])
        if cur and size + n > CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _summarize_chunk(channel_name: str, category: str, chunk: list[dict],
                     base_url: str, model: str) -> str | None:
    body = "\n\n".join(f"### {it['title']}\n{it['points']}" for it in chunk)
    prompt = f"""以下はYouTubeチャンネル「{channel_name}」の「{category}」に関する動画{len(chunk)}本の要点です。

{body}

## 指示
これらに共通する主張・具体的なノウハウを、重複を排してまとめてください。

ルール:
- 動画をまたいで同じことを言っている場合は1つにまとめる
- 「この動画では」のような参照語は使わない。単独で読んで意味が通る文にする
- 具体的な固有名詞・数値（ブランド名、アイテム名、割合など）は落とさず残す
- 抽象論に逃げない。実際に何をすればいいかが分かる粒度で書く
- マークダウンの装飾（**など）は使わない

出力形式: 「- 」始まりの箇条書きのみ。見出しや前置きは不要。"""
    return _call_llm(prompt, base_url, model)


def _reduce_summaries(channel_name: str, category: str, partials: list[str],
                      base_url: str, model: str) -> str | None:
    joined = "\n".join(partials)
    prompt = f"""以下はYouTubeチャンネル「{channel_name}」の「{category}」についての要点を、
複数のまとまりに分けて整理したものです。同じ内容が重複して含まれています。

{joined[:12000]}

## 指示
重複を排してひとつに統合し、読みやすい順に並べ替えてください。

ルール:
- 内容を削らない。重複の統合以外で情報を落とさない
- 関連する項目は近くに並べる
- 「この動画では」のような参照語は使わない
- マークダウンの装飾（**など）は使わない

出力形式: 「- 」始まりの箇条書きのみ。見出しや前置きは不要。"""
    return _call_llm(prompt, base_url, model)


def _write_category_file(channel_name: str, category: str, body: str,
                         items: list[dict]) -> Path:
    out_dir = SUMMARIES_DIR / _sanitize(channel_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_sanitize(category)}.md"
    lines = [
        f"# {category}",
        "",
        f"チャンネル: {channel_name}　／　対象動画: {len(items)}本",
        "",
        body.strip(),
        "",
        "---",
        "",
        "## このカテゴリの動画",
        "",
    ]
    lines += [f"- {it['title']}" for it in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_index(channel_name: str, grouped: dict) -> Path:
    out_dir = SUMMARIES_DIR / _sanitize(channel_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.md"
    total = sum(len(v) for v in grouped.values())
    lines = [
        f"# {channel_name} — カテゴリ別まとめ",
        "",
        f"全{total}本を{len(grouped)}カテゴリに分類した。",
        "",
    ]
    for cat, items in grouped.items():
        lines.append(f"- [{cat}]({_sanitize(cat)}.md) — {len(items)}本")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _summarize_categories(channel_name: str, items: list[dict], assign: dict,
                          categories: list[str], base_url: str, model: str) -> None:
    grouped = {}
    for it in items:
        cat = assign.get(it["file"])
        if cat:
            grouped.setdefault(cat, []).append(it)
    # カテゴリの並びは提案順（＝人が読む順）を保ち、未知のカテゴリは末尾に回す
    ordered = {c: grouped[c] for c in categories if c in grouped}
    ordered.update({c: v for c, v in grouped.items() if c not in ordered})

    for cat, members in ordered.items():
        chunks = _chunk_items(members)
        _err(f"[summarize] {cat}: {len(members)}本 / {len(chunks)}チャンク")
        partials = []
        for i, chunk in enumerate(chunks, 1):
            try:
                r = _summarize_chunk(channel_name, cat, chunk, base_url, model)
            except Exception as e:
                if _is_timeout_error(e):
                    _err(f"  [warn] {cat} チャンク{i}: タイムアウト → スキップ")
                    continue
                _err(f"  [error] {cat} チャンク{i}: {e}")
                continue
            if r:
                partials.append(r)
                _err(f"  [{i}/{len(chunks)}] 集約")
        if not partials:
            _err(f"  [warn] {cat}: まとめを生成できなかった")
            continue
        body = partials[0]
        if len(partials) > 1:
            try:
                body = _reduce_summaries(channel_name, cat, partials, base_url, model) or "\n".join(partials)
            except Exception as e:
                _err(f"  [warn] {cat}: 統合に失敗（分割のまま出力）: {e}")
                body = "\n".join(partials)
        path = _write_category_file(channel_name, cat, body, members)
        _copy_file_to_drive(path)
        _err(f"  → {path}")

    idx = _write_index(channel_name, ordered)
    _copy_file_to_drive(idx)
    _err(f"[done] {channel_name}: {len(ordered)} カテゴリ / {idx}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _setup_log()
    _load_env()

    import os
    base_url = os.environ.get("LOCAL_LLM_URL")
    model = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b")
    if not base_url:
        _err("[error] LOCAL_LLM_URL が設定されていません")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="トランスクリプトをカテゴリ別に分類してまとめを生成する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python src/categorize.py "八田エミリの日常"
  python src/categorize.py "八田エミリの日常" --categories "服装・コーデ,恋愛・モテ,マインド,その他"
  python src/categorize.py "八田エミリの日常" --dry-run
  python src/categorize.py "八田エミリの日常" --force      # 分類をやり直す
""",
    )
    parser.add_argument("channel", help="チャンネル名（transcripts/ 配下のディレクトリ名）")
    parser.add_argument("--categories", help="カテゴリをカンマ区切りで手動指定する（パス0を飛ばす）")
    parser.add_argument("--force", action="store_true", help="分類キャッシュを無視して全件やり直す")
    parser.add_argument("--dry-run", action="store_true",
                        help="分類までで止め、カテゴリごとの件数だけ表示する")
    args = parser.parse_args()

    items = _load_items(args.channel)
    if not items:
        sys.exit(1)
    _err(f"[load] {args.channel}: {len(items)} 件")

    if args.categories:
        categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    else:
        cached = _load_json(_defs_path(args.channel), None)
        if cached and not args.force:
            categories = cached
        else:
            categories = _propose_categories(args.channel, items, base_url, model)
            _save_json(_defs_path(args.channel), categories)
    _err(f"[categories] {' / '.join(categories)}")

    assign = _classify_all(args.channel, items, categories, base_url, model, args.force)

    counts = {}
    for f, c in assign.items():
        counts[c] = counts.get(c, 0) + 1
    for c in categories:
        _err(f"  {c}: {counts.get(c, 0)}本")

    if args.dry_run:
        _err("[dry-run] 分類まで。まとめは生成しない")
        return

    _summarize_categories(args.channel, items, assign, categories, base_url, model)


if __name__ == "__main__":
    main()
