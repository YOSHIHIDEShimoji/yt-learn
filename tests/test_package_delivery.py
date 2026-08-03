import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import package_delivery as pkg


TRANSCRIPT = """# {title}

チャンネル: CH
URL: https://youtu.be/{vid}
モデル: large-v3
処理日時: 2026-08-03 01:00:00

## ポイント
- 要点その1
- 要点その2

---

ここが全文。あーとかえーとかが入っている本文。
"""


def _build(tmp_path, monkeypatch, videos, with_video_files=()):
    """transcripts/ と _index.json を組み立て、モジュールのパスを差し替える。

    videos: [(vid_id, title, upload_date)] / upload_date が None なら日付なし動画
    """
    tdir = tmp_path / "transcripts" / "CH"
    tdir.mkdir(parents=True)
    index = {}
    for vid, title, up in videos:
        md = tdir / f"{title}.md"
        md.write_text(TRANSCRIPT.format(title=title, vid=vid), encoding="utf-8")
        entry = {"title": title, "url": f"https://youtu.be/{vid}", "file": str(md),
                 "transcribed_at": "2026-08-03"}
        if up:
            entry["upload_date"] = up
        index[vid] = entry
    (tdir / "_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    vdir = tmp_path / "deliver" / "CH" / "videos"
    if with_video_files:
        vdir.mkdir(parents=True)
        for vid in with_video_files:
            (vdir / f"{vid}.mp4").write_bytes(b"v" * 2048)

    monkeypatch.setattr(pkg, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(pkg, "SUMMARIES_DIR", tmp_path / "summaries")
    monkeypatch.setattr(pkg, "DELIVER_DIR", tmp_path / "deliver")
    monkeypatch.setattr(pkg, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pkg, "BASE_DIR", tmp_path)


class TestStem:
    def test_includes_number_date_and_title(self):
        assert pkg._stem(7, "2026-07-18", "タイトル") == "007_2026-07-18_タイトル"

    def test_omits_date_when_unknown(self):
        assert pkg._stem(1, "", "タイトル") == "001_タイトル"

    def test_zero_pads_to_three_digits(self):
        assert pkg._stem(184, "2024-07-20", "T").startswith("184_")

    def test_truncates_long_title(self):
        stem = pkg._stem(1, "2026-01-01", "あ" * 200)
        assert len(stem.encode("utf-8")) < 200

    def test_strips_path_separators_from_title(self):
        assert "/" not in pkg._stem(1, "", "服装/コーデ")


class TestLoadEntries:
    def test_orders_newest_first(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [
            ("v1", "古い", "2024-01-01"),
            ("v2", "新しい", "2026-08-01"),
            ("v3", "中間", "2025-05-05"),
        ])
        entries = pkg._load_entries("CH")
        assert [e["title"] for e in entries] == ["新しい", "中間", "古い"]
        assert [e["num"] for e in entries] == [1, 2, 3]

    def test_undated_videos_go_last(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [
            ("v1", "日付なし", None),
            ("v2", "日付あり", "2025-01-01"),
        ])
        entries = pkg._load_entries("CH")
        assert [e["title"] for e in entries] == ["日付あり", "日付なし"]

    def test_excludes_entries_whose_file_is_missing(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "有り", "2025-01-01")])
        idx_path = tmp_path / "transcripts" / "CH" / "_index.json"
        index = json.loads(idx_path.read_text(encoding="utf-8"))
        index["v9"] = {"title": "消えた", "url": "u",
                       "file": str(tmp_path / "transcripts" / "CH" / "消えた.md"),
                       "upload_date": "2026-01-01"}
        idx_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        entries = pkg._load_entries("CH")
        assert [e["title"] for e in entries] == ["有り"]

    def test_missing_index_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pkg, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        assert pkg._load_entries("CH") == []


class TestSplitTranscript:
    def test_extracts_points_section(self):
        points, full = pkg._split_transcript(TRANSCRIPT.format(title="T", vid="v"))
        assert points.startswith("## ポイント")
        assert "要点その2" in points
        assert "ここが全文" not in points

    def test_no_points_returns_empty_head(self):
        points, full = pkg._split_transcript("# T\n\n---\n\n本文だけ")
        assert points == ""
        assert "本文だけ" in full


class TestWriteOutputs:
    def test_points_files_contain_only_points(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        n = pkg._write_points(tmp_path / "out", entries)
        assert n == 1
        f = tmp_path / "out" / "001_2026-01-02_動画A.md"
        text = f.read_text(encoding="utf-8")
        assert "要点その1" in text
        assert "ここが全文" not in text
        assert "投稿日: 2026-01-02" in text

    def test_full_files_are_verbatim_copies(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        pkg._write_full(tmp_path / "full", entries)
        copied = (tmp_path / "full" / "001_2026-01-02_動画A.md").read_text(encoding="utf-8")
        assert copied == entries[0]["md"].read_text(encoding="utf-8")

    def test_points_skips_videos_without_points(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        entries[0]["md"].write_text("# 動画A\n\n---\n\n本文だけ", encoding="utf-8")
        assert pkg._write_points(tmp_path / "out", entries) == 0

    def test_videos_are_renamed_to_match_numbering(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch,
               [("v1", "古い", "2024-01-01"), ("v2", "新しい", "2026-08-01")],
               with_video_files=("v1", "v2"))
        entries = pkg._load_entries("CH")
        count, mb, _ = pkg._copy_videos(tmp_path / "vids", entries, "CH")
        assert count == 2
        assert (tmp_path / "vids" / "001_2026-08-01_新しい.mp4").exists()
        assert (tmp_path / "vids" / "002_2024-01-01_古い.mp4").exists()

    def test_missing_video_files_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        # メンバー限定などで一部だけ動画が無いのが常態。落ちずに残りを並べること
        _build(tmp_path, monkeypatch,
               [("v1", "有り", "2026-01-01"), ("v2", "無し", "2025-01-01")],
               with_video_files=("v1",))
        entries = pkg._load_entries("CH")
        count, _, _ = pkg._copy_videos(tmp_path / "vids", entries, "CH")
        assert count == 1

    def test_no_video_dir_returns_zero(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        assert pkg._copy_videos(tmp_path / "vids", entries, "CH") == (0, 0, set())


class TestRenumberCategories:
    def _setup_summaries(self, tmp_path, entries_titles):
        sdir = tmp_path / "summaries" / "CH"
        sdir.mkdir(parents=True)
        body = ("# 服装\n\nチャンネル: CH\n\n- まとめ\n\n---\n\n"
                "## このカテゴリの動画\n\n" +
                "\n".join(f"- {t}" for t in entries_titles) + "\n")
        (sdir / "服装.md").write_text(body, encoding="utf-8")
        (sdir / "index.md").write_text("# CH — カテゴリ別まとめ\n\n- [服装](服装.md) — 2本\n",
                                       encoding="utf-8")

    def test_adds_numbers_to_member_list(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch,
               [("v1", "古い", "2024-01-01"), ("v2", "新しい", "2026-08-01")])
        self._setup_summaries(tmp_path, ["古い", "新しい"])
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "CH_categories.json").write_text(
            json.dumps({"古い.md": "服装", "新しい.md": "服装"}, ensure_ascii=False),
            encoding="utf-8")
        entries = pkg._load_entries("CH")
        n = pkg._renumber_categories(tmp_path / "out", "CH", entries)
        assert n == 2
        text = (tmp_path / "out" / "服装.md").read_text(encoding="utf-8")
        assert "- 001  新しい" in text
        assert "- 002  古い" in text
        assert text.index("001") < text.index("002")  # 番号順に並ぶ

    def test_index_is_copied_untouched(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._setup_summaries(tmp_path, ["動画A"])
        entries = pkg._load_entries("CH")
        pkg._renumber_categories(tmp_path / "out", "CH", entries)
        src = (tmp_path / "summaries" / "CH" / "index.md").read_text(encoding="utf-8")
        assert (tmp_path / "out" / "index.md").read_text(encoding="utf-8") == src

    def test_missing_summaries_dir_is_not_fatal(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        assert pkg._renumber_categories(tmp_path / "out", "CH", entries) == 0


class TestCollectCategories:
    def _summaries(self, tmp_path, index_body):
        sdir = tmp_path / "summaries" / "CH"
        sdir.mkdir(parents=True)
        for name in ("服装", "恋愛", "マインド"):
            (sdir / f"{name}.md").write_text(
                f"# {name}\n\n- 本文\n\n---\n\n## このカテゴリの動画\n\n- 動画A\n",
                encoding="utf-8")
        (sdir / "index.md").write_text(index_body, encoding="utf-8")
        return sdir

    def test_follows_index_order_not_filename_order(self, tmp_path, monkeypatch):
        # index.md は categorize.py が提案順で書く。読む順として意味があるので守る
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._summaries(tmp_path, "# CH\n\n- [服装](服装.md) — 1本\n"
                                  "- [恋愛](恋愛.md) — 1本\n- [マインド](マインド.md) — 1本\n")
        entries = pkg._load_entries("CH")
        assert [c["name"] for c in pkg._collect_categories("CH", entries)] == \
            ["服装", "恋愛", "マインド"]

    def test_falls_back_to_name_order_without_index(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        sdir = self._summaries(tmp_path, "")
        (sdir / "index.md").unlink()
        entries = pkg._load_entries("CH")
        names = [c["name"] for c in pkg._collect_categories("CH", entries)]
        assert names == sorted(names)

    def test_category_missing_from_index_goes_last(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._summaries(tmp_path, "# CH\n\n- [恋愛](恋愛.md) — 1本\n")
        entries = pkg._load_entries("CH")
        assert pkg._collect_categories("CH", entries)[0]["name"] == "恋愛"

    def test_index_itself_is_not_a_category(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._summaries(tmp_path, "# CH\n\n- [服装](服装.md) — 1本\n")
        entries = pkg._load_entries("CH")
        assert "index" not in [c["name"] for c in pkg._collect_categories("CH", entries)]


class TestPickVideos:
    def _entries(self, specs):
        return [{"id": i, "num": n, "view_count": v}
                for n, (i, v) in enumerate(specs, 1)]

    def test_keeps_only_available(self):
        e = self._entries([("a", 10), ("b", 20), ("c", 30)])
        out = pkg._pick_videos(e, {"a", "c"}, 0, "popular")
        assert [x["id"] for x in out] == ["a", "c"]

    def test_limit_takes_most_viewed(self):
        # スマホに全部入れても見きれないので、絞るときは再生数の多い順
        e = self._entries([("a", 10), ("b", 500), ("c", 30)])
        out = pkg._pick_videos(e, {"a", "b", "c"}, 2, "popular")
        assert {x["id"] for x in out} == {"b", "c"}

    def test_result_is_sorted_by_number_not_by_views(self):
        # 連番順に並べておかないとファイルの並びがばらける
        e = self._entries([("a", 10), ("b", 500), ("c", 30)])
        out = pkg._pick_videos(e, {"a", "b", "c"}, 3, "popular")
        assert [x["num"] for x in out] == [1, 2, 3]

    def test_date_order_keeps_newest(self):
        e = self._entries([("a", 10), ("b", 500), ("c", 30)])
        out = pkg._pick_videos(e, {"a", "b", "c"}, 2, "date")
        assert {x["id"] for x in out} == {"a", "b"}

    def test_missing_view_count_sinks_to_the_bottom(self):
        e = [{"id": "a", "num": 1}, {"id": "b", "num": 2, "view_count": 5}]
        out = pkg._pick_videos(e, {"a", "b"}, 1, "popular")
        assert out[0]["id"] == "b"

    def test_zero_limit_keeps_everything(self):
        e = self._entries([("a", 10), ("b", 20)])
        assert len(pkg._pick_videos(e, {"a", "b"}, 0, "popular")) == 2


class TestConclusionSection:
    def _write(self, tmp_path, body):
        sdir = tmp_path / "summaries" / "CH"
        sdir.mkdir(parents=True)
        (sdir / "服装.md").write_text(body, encoding="utf-8")
        (sdir / "index.md").write_text("# CH\n\n- [服装](服装.md) — 1本\n", encoding="utf-8")

    def test_splits_lede_and_detail(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._write(tmp_path, "# 服装\n\nチャンネル: CH\n\n## まず結論\n\n- 結論だ\n\n"
                              "## くわしく\n\n- 詳細だ\n\n---\n\n## このカテゴリの動画\n\n- 動画A\n")
        c = pkg._collect_categories("CH", pkg._load_entries("CH"))[0]
        assert "結論だ" in c["lede"] and "詳細だ" not in c["lede"]
        assert "詳細だ" in c["detail"] and "結論だ" not in c["detail"]

    def test_without_lede_everything_is_detail(self, tmp_path, monkeypatch):
        # categorize.py 側で結論の抽出が失敗しても本文が消えないこと
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._write(tmp_path, "# 服装\n\nチャンネル: CH\n\n- 本文だけ\n\n"
                              "---\n\n## このカテゴリの動画\n\n- 動画A\n")
        c = pkg._collect_categories("CH", pkg._load_entries("CH"))[0]
        assert c["lede"] == ""
        assert "本文だけ" in c["detail"]

    def test_html_renders_lede_box(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._write(tmp_path, "# 服装\n\nチャンネル: CH\n\n## まず結論\n\n- 結論だ\n\n"
                              "## くわしく\n\n- 詳細だ\n\n---\n\n## このカテゴリの動画\n\n- 動画A\n")
        entries = pkg._load_entries("CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, pkg._collect_categories("CH", entries))
        text = out.read_text(encoding="utf-8")
        assert "class='lede'" in text
        assert "まず結論" in text and "結論だ" in text
        assert text.index("結論だ") < text.index("詳細だ")

    def test_html_without_lede_has_no_empty_box(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._write(tmp_path, "# 服装\n\nチャンネル: CH\n\n- 本文だけ\n\n"
                              "---\n\n## このカテゴリの動画\n\n- 動画A\n")
        entries = pkg._load_entries("CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, pkg._collect_categories("CH", entries))
        text = out.read_text(encoding="utf-8")
        assert "class='lede'" not in text
        assert "本文だけ" in text


class TestHtmlToPdf:
    def test_missing_chrome_is_not_fatal(self, tmp_path, monkeypatch):
        # Chrome が無い環境（WSL 等）でも HTML の納品は成立させる
        monkeypatch.setattr(pkg, "_find_chrome", lambda: None)
        html_file = tmp_path / "a.html"
        html_file.write_text("<p>x</p>", encoding="utf-8")
        assert pkg._html_to_pdf(html_file, tmp_path / "a.pdf") is False

    def test_reports_failure_when_pdf_not_produced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pkg, "_find_chrome", lambda: "/bin/echo")
        html_file = tmp_path / "a.html"
        html_file.write_text("<p>x</p>", encoding="utf-8")
        assert pkg._html_to_pdf(html_file, tmp_path / "nope.pdf") is False


class TestBulletsToHtml:
    def test_converts_bullets_to_list(self):
        out = pkg._bullets_to_html("- 一つめ\n- 二つめ")
        assert out == "<ul><li>一つめ</li><li>二つめ</li></ul>"

    def test_escapes_html(self):
        # LLM 出力に < や & が混ざってもページを壊さない
        out = pkg._bullets_to_html("- a < b & c")
        assert "&lt;" in out and "&amp;" in out
        assert "<b>" not in out

    def test_drops_headings_and_metadata_lines(self):
        out = pkg._bullets_to_html("# 見出し\nチャンネル: CH\n\n- 本文")
        assert "見出し" not in out and "チャンネル" not in out
        assert "本文" in out

    def test_plain_paragraph_survives(self):
        assert "<p>ふつうの文</p>" in pkg._bullets_to_html("ふつうの文")


class TestWriteHtml:
    def _cats(self, entries):
        return [{"name": "服装", "head": "# 服装\n\n- 色は3色以内",
                 "has_member_section": True, "members": entries[:1]}]

    def test_single_self_contained_file(self, tmp_path, monkeypatch):
        # 飛行機で開く前提。外部リソースを一切参照しないこと
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "まとめ.html"
        pkg._write_html(out, "CH", entries, self._cats(entries))
        text = out.read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>")
        assert "<style>" in text          # CSS はインライン
        assert "http://" not in text and "https://cdn" not in text
        assert "<script" not in text

    def test_category_links_to_video_anchor(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, self._cats(entries))
        text = out.read_text(encoding="utf-8")
        assert "href='#v001'" in text
        assert "id='v001'" in text

    def test_includes_per_video_points(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, self._cats(entries))
        text = out.read_text(encoding="utf-8")
        assert "要点その1" in text and "要点その2" in text
        assert "ここが全文" not in text  # 全文は入れない（分量が大きいので .md のまま）

    def test_video_link_only_when_videos_included(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        a, b = tmp_path / "a.html", tmp_path / "b.html"
        pkg._write_html(a, "CH", entries, self._cats(entries), video_nums={1})
        pkg._write_html(b, "CH", entries, self._cats(entries))
        assert "4_%E5%8B%95%E7%94%BB/" in a.read_text(encoding="utf-8")  # URLエンコード済み
        assert "動画を見る" not in b.read_text(encoding="utf-8")

    def test_title_with_html_chars_is_escaped(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "a<b>c", "2026-01-02")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, self._cats(entries))
        text = out.read_text(encoding="utf-8")
        assert "a&lt;b&gt;c" in text

    def test_videos_without_points_are_skipped(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-02")])
        entries = pkg._load_entries("CH")
        entries[0]["md"].write_text("# 動画A\n\n---\n\n本文だけ", encoding="utf-8")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, self._cats(entries))
        assert "id='v001'" not in out.read_text(encoding="utf-8")


class TestReadme:
    def test_mentions_counts_and_where_to_start(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "00.md"
        pkg._write_readme(out, "CH", entries, n_points=1, n_videos=1, mb=23, n_categories=5)
        text = out.read_text(encoding="utf-8")
        assert "`まとめ.pdf` を開いてください" in text
        assert "メンバーシップ限定" in text
        assert "23MB" in text

    def test_video_line_omitted_when_no_videos(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "00.md"
        pkg._write_readme(out, "CH", entries, n_points=1, n_videos=0, mb=0, n_categories=5)
        assert "4_動画/" not in out.read_text(encoding="utf-8")
