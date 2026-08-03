import json
import subprocess
import sys
import types
import urllib.parse
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


def _build(tmp_path, monkeypatch, videos, with_video_files=(), views=None,
           video_ext=".mp4"):
    """transcripts/ と _index.json を組み立て、モジュールのパスを差し替える。

    videos: [(vid_id, title, upload_date)] / upload_date が None なら日付なし動画
    views: {vid_id: 再生数} / _index.json に view_count を載せる
    video_ext: 置く動画の拡張子（.webm 混在の検証用）
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
        if views and vid in views:
            entry["view_count"] = views[vid]
        index[vid] = entry
    (tdir / "_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    vdir = tmp_path / "deliver" / "CH" / "videos"
    if with_video_files:
        vdir.mkdir(parents=True)
        for vid in with_video_files:
            (vdir / f"{vid}{video_ext}").write_bytes(b"v" * 2048)

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
        assert pkg._copy_videos(tmp_path / "vids", entries, "CH") == (0, 0, {})

    def test_returns_actual_filenames_so_links_are_not_guessed(self, tmp_path, monkeypatch):
        # .webm で取れた動画に .mp4 のリンクを張ると、押しても何も起きない
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")],
               with_video_files=("v1",), video_ext=".webm")
        entries = pkg._load_entries("CH")
        _, _, files = pkg._copy_videos(tmp_path / "vids", entries, "CH")
        assert files == {1: "001_2026-01-01_動画A.webm"}
        assert (tmp_path / "vids" / "001_2026-01-01_動画A.webm").exists()

    def test_html_link_matches_the_file_that_was_copied(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")],
               with_video_files=("v1",), video_ext=".webm")
        entries = pkg._load_entries("CH")
        _, _, files = pkg._copy_videos(tmp_path / "vids", entries, "CH")
        out = tmp_path / "o.html"
        pkg._write_html(out, "CH", entries, [], files)
        text = out.read_text(encoding="utf-8")
        assert urllib.parse.quote("4_動画/001_2026-01-01_動画A.webm") in text
        assert ".mp4" not in text

    def test_view_count_reaches_entries_so_popular_order_works(self, tmp_path, monkeypatch):
        # _load_entries が view_count を載せ忘れると全件 0 になり、
        # 「人気順」が黙って「新着順」に化ける
        _build(tmp_path, monkeypatch,
               [("v1", "古いが人気", "2024-01-01"), ("v2", "新しいが不人気", "2026-08-01")],
               with_video_files=("v1", "v2"), views={"v1": 999999, "v2": 100})
        entries = pkg._load_entries("CH")
        _, _, files = pkg._copy_videos(tmp_path / "vids", entries, "CH",
                                       limit=1, order="popular")
        assert list(files.values()) == ["002_2024-01-01_古いが人気.mp4"]


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

    def test_detail_without_lede_keeps_the_text_above_it(self, tmp_path, monkeypatch):
        # 「くわしく」はあるが「まず結論」が無い md。手で書いたときに起きる。
        # その手前の本文を捨てると、納品物から無音で消える
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        self._write(tmp_path, "# 服装\n\nこの話題の前置き。\n\n"
                              "## くわしく\n\n- 詳細だ\n\n---\n\n"
                              "## このカテゴリの動画\n\n- 動画A\n")
        c = pkg._collect_categories("CH", pkg._load_entries("CH"))[0]
        assert c["lede"] == ""
        assert "この話題の前置き。" in c["detail"]
        assert "詳細だ" in c["detail"]

    def test_members_match_when_category_name_has_slash(self, tmp_path, monkeypatch):
        # カテゴリ名にファイル名禁止文字が入るのは実際に起きる（LLM が提案する）。
        # 突合が壊れると「この話題の動画」一覧と番号↔色の対応が消える
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        sdir = tmp_path / "summaries" / "CH"
        sdir.mkdir(parents=True)
        (sdir / "服装_コーデ.md").write_text(
            "# 服装/コーデ\n\n## まず結論\n\n- 結論だ\n\n## くわしく\n\n- 詳細だ\n\n"
            "---\n\n## このカテゴリの動画\n\n- 動画A\n", encoding="utf-8")
        cdir = tmp_path / "cache"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "CH_categories.json").write_text(
            json.dumps({"動画A.md": "服装/コーデ"}, ensure_ascii=False), encoding="utf-8")
        c = pkg._collect_categories("CH", pkg._load_entries("CH"))[0]
        assert [m["title"] for m in c["members"]] == ["動画A"]

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

    def _spy(self, monkeypatch, tmp_path, chrome):
        """Chrome を呼ばずに、組み立てたコマンドだけ捕まえる。"""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pkg, "_find_chrome", lambda: chrome)
        monkeypatch.setattr(subprocess, "run", fake_run)
        html_file = tmp_path / "a.html"
        html_file.write_text("<p>x</p>", encoding="utf-8")
        assert pkg._html_to_pdf(html_file, tmp_path / "a.pdf") is True
        return seen["cmd"]

    def test_native_chrome_gets_file_uri(self, tmp_path, monkeypatch):
        cmd = self._spy(monkeypatch, tmp_path, "/usr/bin/google-chrome")
        assert cmd[-1].startswith("file://")
        assert cmd[-2] == f"--print-to-pdf={tmp_path / 'a.pdf'}"

    def test_windows_chrome_gets_windows_paths(self, tmp_path, monkeypatch):
        # WSL から Windows の Chrome を呼ぶ場合、file:// URI も Linux パスも通らない。
        # 入力・出力の両方が wslpath の変換結果になっていないと PDF が黙って落ちる
        monkeypatch.setattr(pkg, "_win_path", lambda p: rf"\\wsl.localhost\U\{p.name}")
        cmd = self._spy(monkeypatch, tmp_path,
                        "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
        assert cmd[-1] == r"\\wsl.localhost\U\a.html"
        assert cmd[-2] == r"--print-to-pdf=\\wsl.localhost\U\a.pdf"
        assert not any(str(tmp_path) in str(a) for a in cmd[1:])

    def test_windows_chrome_without_wslpath_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pkg, "_find_chrome",
                            lambda: "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
        monkeypatch.setattr(pkg, "_win_path", lambda p: None)
        html_file = tmp_path / "a.html"
        html_file.write_text("<p>x</p>", encoding="utf-8")
        assert pkg._html_to_pdf(html_file, tmp_path / "a.pdf") is False

    def test_stale_pdf_is_removed_before_generating(self, tmp_path, monkeypatch):
        # 残したまま失敗すると、前回の古い PDF が今回の成果物として残る
        monkeypatch.setattr(pkg, "_find_chrome", lambda: None)
        html_file = tmp_path / "a.html"
        html_file.write_text("<p>x</p>", encoding="utf-8")
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-old")
        assert pkg._html_to_pdf(html_file, pdf) is False
        assert not pdf.exists()

    def test_win_path_returns_none_when_wslpath_missing(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no wslpath")

        monkeypatch.setattr(subprocess, "run", boom)
        assert pkg._win_path(tmp_path / "x.html") is None

    def test_win_path_uses_wslpath_output(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kw):
            assert cmd[:2] == ["wslpath", "-w"]
            return types.SimpleNamespace(returncode=0, stdout="C:\\tmp\\x.html\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert pkg._win_path(tmp_path / "x.html") == "C:\\tmp\\x.html"


class TestBulletsToHtml:
    def test_converts_bullets_to_list(self):
        out = pkg._bullets_to_html("- 一つめ\n- 二つめ")
        assert out == "<ul><li>一つめ</li><li>二つめ</li></ul>"

    def test_renders_h3_subheading(self):
        out = pkg._bullets_to_html("### ユニクロ\n- セーター")
        assert out == "<h3 class='sub'>ユニクロ</h3>\n<ul><li>セーター</li></ul>"

    def test_broken_points_heading_is_still_dropped(self):
        # LLM が綴りを間違えた見出しを本文として出さないための守りを壊していないこと
        out = pkg._bullets_to_html("## ポイnts\n- 中身")
        assert "ポイnts" not in out
        assert out == "<ul><li>中身</li></ul>"

    def test_bold_markup_becomes_strong(self):
        out = pkg._bullets_to_html("- ここが**大事**です")
        assert out == "<ul><li>ここが<strong>大事</strong>です</li></ul>"

    def test_escapes_before_bold_so_markup_cannot_be_injected(self):
        out = pkg._bullets_to_html("- **<script>**")
        assert "<script>" not in out
        assert "<strong>&lt;script&gt;</strong>" in out

    def test_leading_minus_in_content_is_kept(self):
        # 記号を文字集合で剥ぐと「- -3kg」が「3kg」になり、符号が静かに消える
        assert pkg._bullets_to_html("- -3kg 落ちた") == "<ul><li>-3kg 落ちた</li></ul>"

    def test_two_bold_pairs_on_one_line_stay_separate(self):
        # 貪欲マッチだと間の地の文まで飲み込み、リテラルの ** が紙面に出る
        out = pkg._bullets_to_html("- **色は3色**に絞り、**柄物は1点**まで")
        assert out == ("<ul><li><strong>色は3色</strong>に絞り、"
                       "<strong>柄物は1点</strong>まで</li></ul>")

    def test_unpaired_asterisks_are_left_alone(self):
        out = pkg._bullets_to_html("- 2**3 の話")
        assert out == "<ul><li>2**3 の話</li></ul>"

    def test_subheading_closes_open_list(self):
        out = pkg._bullets_to_html("- 前\n### 次\n- 後")
        assert out == ("<ul><li>前</li></ul>\n<h3 class='sub'>次</h3>\n"
                       "<ul><li>後</li></ul>")

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
        pkg._write_html(a, "CH", entries, self._cats(entries),
                        video_files={1: "001_2026-01-02_動画A.mp4"})
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

    def test_points_to_html_when_pdf_failed(self, tmp_path, monkeypatch):
        # PDF が作れなかったのに PDF を案内すると、案内そのものが嘘になる
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "00.md"
        pkg._write_readme(out, "CH", entries, n_points=1, n_videos=0, mb=0,
                          n_categories=5, pdf_ok=False)
        text = out.read_text(encoding="utf-8")
        assert "`まとめ.html` をブラウザで開いてください" in text
        assert "`まとめ.pdf` を開いてください" not in text

    def test_video_line_omitted_when_no_videos(self, tmp_path, monkeypatch):
        _build(tmp_path, monkeypatch, [("v1", "動画A", "2026-01-01")])
        entries = pkg._load_entries("CH")
        out = tmp_path / "00.md"
        pkg._write_readme(out, "CH", entries, n_points=1, n_videos=0, mb=0, n_categories=5)
        assert "4_動画/" not in out.read_text(encoding="utf-8")


class TestMainRebuild:
    def _run(self, tmp_path, monkeypatch, out):
        monkeypatch.setattr(sys, "argv",
                            ["package_delivery.py", "CH", "--output", str(out),
                             "--no-videos", "--no-pdf"])
        pkg.main()

    def test_rebuild_does_not_leave_old_numbered_files(self, tmp_path, monkeypatch):
        # 連番は件数で変わる。掃除しないと旧番号のファイルが残り、
        # 「まとめの番号 → フォルダのファイル」の導線が半分食い違う
        _build(tmp_path, monkeypatch, [("v1", "古い", "2024-01-01"),
                                       ("v2", "新しい", "2026-08-01")])
        out = tmp_path / "out"
        self._run(tmp_path, monkeypatch, out)
        assert len(list((out / "2_動画ごとの要点").iterdir())) == 2

        # 1本消して組み直す（番号が繰り上がる）
        idx = tmp_path / "transcripts" / "CH" / "_index.json"
        index = json.loads(idx.read_text(encoding="utf-8"))
        del index["v2"]
        idx.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        self._run(tmp_path, monkeypatch, out)

        names = sorted(p.name for p in (out / "2_動画ごとの要点").iterdir())
        assert names == ["001_2024-01-01_古い.md"], names
        assert sorted(p.name for p in (out / "3_全文").iterdir()) == names
