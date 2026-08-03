import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import categorize


CATS = ["服装・コーデ", "恋愛・モテ", "マインド", "その他"]


# ── _normalize_category ───────────────────────────────────────────────────────

class TestNormalizeCategory:
    def test_exact_match(self):
        assert categorize._normalize_category("マインド", CATS) == "マインド"

    def test_strips_list_marker_and_number(self):
        assert categorize._normalize_category("- 2. マインド", CATS) == "マインド"

    def test_uses_first_line_only(self):
        assert categorize._normalize_category("恋愛・モテ\nこの動画は…", CATS) == "恋愛・モテ"

    def test_partial_match(self):
        assert categorize._normalize_category("カテゴリは 服装・コーデ です", CATS) == "服装・コーデ"

    def test_unknown_falls_back_to_other(self):
        assert categorize._normalize_category("宇宙開発", CATS) == "その他"

    def test_empty_string_falls_back_to_other(self):
        # 空文字を部分一致に流すと "" in c が全カテゴリで真になり
        # 先頭カテゴリに黙って吸い込まれる。それを防げていること
        assert categorize._normalize_category("", CATS) == "その他"

    def test_none_falls_back_to_other(self):
        assert categorize._normalize_category(None, CATS) == "その他"

    def test_whitespace_only_falls_back_to_other(self):
        assert categorize._normalize_category("  \n  ", CATS) == "その他"

    def test_falls_back_to_last_category_when_other_absent(self):
        cats = ["A", "B", "ZZZ"]
        assert categorize._normalize_category("該当なし", cats) == "ZZZ"


# ── _chunk_items ──────────────────────────────────────────────────────────────

class TestChunkItems:
    def test_groups_within_budget(self, monkeypatch):
        monkeypatch.setattr(categorize, "CHUNK_CHARS", 1000)
        items = [{"points": "x" * 400} for _ in range(5)]
        chunks = categorize._chunk_items(items)
        assert [len(c) for c in chunks] == [2, 2, 1]

    def test_oversize_item_gets_its_own_chunk(self, monkeypatch):
        # 1本で上限を超える動画があっても空チャンクを作らないこと
        monkeypatch.setattr(categorize, "CHUNK_CHARS", 1000)
        items = [{"points": "x" * 5000}, {"points": "y" * 100}]
        chunks = categorize._chunk_items(items)
        assert [len(c) for c in chunks] == [1, 1]
        assert all(c for c in chunks)

    def test_empty_input(self):
        assert categorize._chunk_items([]) == []

    def test_every_item_appears_exactly_once(self, monkeypatch):
        monkeypatch.setattr(categorize, "CHUNK_CHARS", 900)
        items = [{"points": "x" * 300, "id": i} for i in range(7)]
        flat = [it for c in categorize._chunk_items(items) for it in c]
        assert [it["id"] for it in flat] == list(range(7))


# ── _load_items ───────────────────────────────────────────────────────────────

class TestLoadItems:
    def _write(self, tmp_path, monkeypatch, files: dict):
        ch = tmp_path / "transcripts" / "CH"
        ch.mkdir(parents=True)
        for name, body in files.items():
            (ch / name).write_text(body, encoding="utf-8")
        monkeypatch.setattr(categorize, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    def test_collects_files_with_points(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, {
            "動画A.md": "# A\n\n## ポイント\n- あ\n- い\n\n---\n\n本文",
        })
        items = categorize._load_items("CH")
        assert len(items) == 1
        assert items[0]["title"] == "動画A"
        assert "- あ" in items[0]["points"]

    def test_skips_files_without_points(self, tmp_path, monkeypatch):
        # transcribe 側で Ollama が落ちた動画はポイントが無い。集約できないので除外する
        self._write(tmp_path, monkeypatch, {
            "有り.md": "# 有り\n\n## ポイント\n- あ\n\n---\n\n本文",
            "無し.md": "# 無し\n\n---\n\n本文だけ",
        })
        items = categorize._load_items("CH")
        assert [it["title"] for it in items] == ["有り"]

    def test_missing_channel_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        assert categorize._load_items("NOPE") == []


# ── _propose_categories ───────────────────────────────────────────────────────

class TestProposeCategories:
    ITEMS = [{"title": f"動画{i}", "points": "- x"} for i in range(3)]

    def test_parses_plain_lines(self):
        with patch.object(categorize, "_call_llm", return_value="服装\n恋愛\nマインド\nその他"):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats == ["服装", "恋愛", "マインド", "その他"]

    def test_strips_numbering_and_bullets(self):
        with patch.object(categorize, "_call_llm", return_value="1. 服装\n- 恋愛\n* その他"):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats == ["服装", "恋愛", "その他"]

    def test_appends_other_when_missing(self):
        with patch.object(categorize, "_call_llm", return_value="服装\n恋愛"):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats[-1] == "その他"

    def test_deduplicates(self):
        with patch.object(categorize, "_call_llm", return_value="服装\n服装\n恋愛"):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats.count("服装") == 1

    def test_falls_back_when_llm_returns_nothing(self):
        with patch.object(categorize, "_call_llm", return_value=None):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats == categorize.FALLBACK_CATEGORIES

    def test_falls_back_when_result_is_unusable(self):
        with patch.object(categorize, "_call_llm", return_value="カテゴリを提案します"):
            cats = categorize._propose_categories("CH", self.ITEMS, "u", "m")
        assert cats == categorize.FALLBACK_CATEGORIES


# ── _classify_all ─────────────────────────────────────────────────────────────

class TestClassifyAll:
    def _items(self, n):
        return [{"file": f"v{i}.md", "title": f"動画{i}", "points": "- x"} for i in range(n)]

    def test_skips_already_assigned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "CACHE_DIR", tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        categorize._save_json(categorize._assign_path("CH"), {"v0.md": "マインド"})
        with patch.object(categorize, "_classify", return_value="その他") as m:
            assign = categorize._classify_all("CH", self._items(2), CATS, "u", "m", force=False)
        assert m.call_count == 1  # v0 は再分類しない
        assert assign["v0.md"] == "マインド"

    def test_force_reclassifies_everything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "CACHE_DIR", tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        categorize._save_json(categorize._assign_path("CH"), {"v0.md": "マインド"})
        with patch.object(categorize, "_classify", return_value="その他") as m:
            assign = categorize._classify_all("CH", self._items(2), CATS, "u", "m", force=True)
        assert m.call_count == 2
        assert assign["v0.md"] == "その他"

    def test_timeout_leaves_video_unassigned(self, tmp_path, monkeypatch):
        # タイムアウトした動画を誤ったカテゴリに入れず、次回に持ち越すこと
        monkeypatch.setattr(categorize, "CACHE_DIR", tmp_path / "cache")
        with patch.object(categorize, "_classify", side_effect=TimeoutError("timed out")):
            assign = categorize._classify_all("CH", self._items(1), CATS, "u", "m", force=False)
        assert assign == {}

    def test_persists_assignments(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "CACHE_DIR", tmp_path / "cache")
        with patch.object(categorize, "_classify", return_value="マインド"):
            categorize._classify_all("CH", self._items(3), CATS, "u", "m", force=False)
        saved = json.loads(categorize._assign_path("CH").read_text(encoding="utf-8"))
        assert saved == {"v0.md": "マインド", "v1.md": "マインド", "v2.md": "マインド"}


# ── 出力 ──────────────────────────────────────────────────────────────────────

class TestSampleTitles:
    def test_returns_all_when_within_budget(self):
        items = [{"title": f"動画{i}"} for i in range(5)]
        out = categorize._sample_titles(items, 1000)
        assert out.count("\n") == 4

    def test_samples_evenly_instead_of_truncating_tail(self, ):
        # 後ろを切り捨てると古い時期の話題がカテゴリ候補から丸ごと消え、
        # その時期の動画がまとめて「その他」に落ちる
        items = [{"title": f"タイトル{i:03d}"} for i in range(200)]
        out = categorize._sample_titles(items, 300)
        assert "タイトル000" in out
        assert "タイトル199" in out or "タイトル19" in out
        assert len(out) <= 400

    def test_never_returns_empty(self):
        items = [{"title": "あ" * 500}]
        assert categorize._sample_titles(items, 50).strip()


class TestReduceSummaries:
    def test_single_partial_is_returned_as_is(self):
        with patch.object(categorize, "_call_llm", return_value="統合") as m:
            out = categorize._reduce_summaries("CH", "服装", ["- 一つだけ"], "u", "m")
        assert out == "- 一つだけ"
        assert m.call_count == 0

    def test_reduces_hierarchically_when_over_budget(self, monkeypatch):
        # 単純に切り詰めると後ろのチャンクが黙って消える。段階的に畳むこと
        monkeypatch.setattr(categorize, "REDUCE_CHARS", 100)
        partials = [f"- 要点{i}" + "x" * 60 for i in range(8)]
        calls = []

        def fake(prompt, *a):
            calls.append(prompt)
            return "- 統合結果"

        monkeypatch.setattr(categorize, "_call_llm", fake)
        out = categorize._reduce_summaries("CH", "服装", partials, "u", "m")
        assert out == "- 統合結果"
        # 全チャンクがどこかの統合プロンプトに現れる＝取りこぼしていない
        joined = "\n".join(calls)
        for i in range(8):
            assert f"要点{i}" in joined

    def test_empty_partials_returns_none(self):
        assert categorize._reduce_summaries("CH", "服装", [], "u", "m") is None
        assert categorize._reduce_summaries("CH", "服装", [None, ""], "u", "m") is None

    def test_llm_failure_falls_back_to_concatenation(self, monkeypatch):
        monkeypatch.setattr(categorize, "REDUCE_CHARS", 100)
        with patch.object(categorize, "_call_llm", return_value=None):
            out = categorize._reduce_summaries("CH", "服装", ["- A", "- B"], "u", "m")
        assert "- A" in out and "- B" in out

    def test_terminates_when_input_cannot_shrink(self, monkeypatch):
        # 1件だけで予算を超える場合でも無限ループしないこと
        monkeypatch.setattr(categorize, "REDUCE_CHARS", 10)
        with patch.object(categorize, "_call_llm", return_value="x" * 500):
            out = categorize._reduce_summaries("CH", "服装", ["y" * 500, "z" * 500], "u", "m")
        assert out


class TestSummarizeCategories:
    """カテゴリ集約の統括処理。下位部品は個別にテスト済みなので、ここでは
    グルーピング・順序・失敗の閉じ込めという「統括だけが持つ責務」を固定する。
    """

    ITEMS = [
        {"file": "a.md", "title": "動画A", "points": "- あ"},
        {"file": "b.md", "title": "動画B", "points": "- い"},
        {"file": "c.md", "title": "動画C", "points": "- う"},
    ]
    ASSIGN = {"a.md": "服装", "b.md": "恋愛", "c.md": "服装"}
    CATS = ["服装", "恋愛", "その他"]

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "SUMMARIES_DIR", tmp_path / "summaries")
        monkeypatch.setattr(categorize, "_copy_file_to_drive", lambda p: None)
        return tmp_path / "summaries" / "CH"

    def test_writes_one_file_per_populated_category(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        with patch.object(categorize, "_summarize_chunk", return_value="- まとめ"):
            categorize._summarize_categories("CH", self.ITEMS, self.ASSIGN, self.CATS, "u", "m")
        assert (out / "服装.md").exists()
        assert (out / "恋愛.md").exists()
        assert not (out / "その他.md").exists()  # 所属0のカテゴリはファイルを作らない

    def test_groups_members_correctly(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        with patch.object(categorize, "_summarize_chunk", return_value="- まとめ"):
            categorize._summarize_categories("CH", self.ITEMS, self.ASSIGN, self.CATS, "u", "m")
        text = (out / "服装.md").read_text(encoding="utf-8")
        assert "対象動画: 2本" in text
        assert "- 動画A" in text and "- 動画C" in text and "- 動画B" not in text

    def test_index_follows_proposal_order(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        with patch.object(categorize, "_summarize_chunk", return_value="- まとめ"):
            categorize._summarize_categories("CH", self.ITEMS, self.ASSIGN, self.CATS, "u", "m")
        idx = (out / "index.md").read_text(encoding="utf-8")
        assert idx.index("服装") < idx.index("恋愛")

    def test_unknown_category_goes_last(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        assign = {**self.ASSIGN, "b.md": "未知カテゴリ"}
        with patch.object(categorize, "_summarize_chunk", return_value="- まとめ"):
            categorize._summarize_categories("CH", self.ITEMS, assign, self.CATS, "u", "m")
        idx = (out / "index.md").read_text(encoding="utf-8")
        assert idx.index("服装") < idx.index("未知カテゴリ")

    def test_timeout_in_one_category_does_not_block_others(self, tmp_path, monkeypatch):
        # Ollama が GPU 占有でタイムアウトするのは日常。1カテゴリの失敗で
        # 他のカテゴリのまとめまで失わないこと
        out = self._setup(tmp_path, monkeypatch)

        def _chunk(channel, category, chunk, *a):
            if category == "服装":
                raise TimeoutError("timed out")
            return "- まとめ"

        with patch.object(categorize, "_summarize_chunk", side_effect=_chunk):
            categorize._summarize_categories("CH", self.ITEMS, self.ASSIGN, self.CATS, "u", "m")
        assert not (out / "服装.md").exists()
        assert (out / "恋愛.md").exists()
        assert (out / "index.md").exists()

    def test_falls_back_to_partials_when_reduce_fails(self, tmp_path, monkeypatch):
        # 統合に失敗しても、チャンクごとのまとめは捨てないこと
        out = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(categorize, "CHUNK_CHARS", 1)  # 1本1チャンクに割る
        items = [{"file": f"{i}.md", "title": f"動画{i}", "points": "- x" * 5} for i in range(3)]
        assign = {f"{i}.md": "服装" for i in range(3)}
        with patch.object(categorize, "_summarize_chunk", side_effect=["- A", "- B", "- C"]), \
             patch.object(categorize, "_reduce_summaries", side_effect=RuntimeError("boom")):
            categorize._summarize_categories("CH", items, assign, ["服装"], "u", "m")
        text = (out / "服装.md").read_text(encoding="utf-8")
        assert "- A" in text and "- B" in text and "- C" in text

    def test_unassigned_videos_are_omitted(self, tmp_path, monkeypatch):
        # 分類がタイムアウトした動画を勝手にどこかへ入れないこと
        out = self._setup(tmp_path, monkeypatch)
        with patch.object(categorize, "_summarize_chunk", return_value="- まとめ"):
            categorize._summarize_categories("CH", self.ITEMS, {"a.md": "服装"},
                                             self.CATS, "u", "m")
        assert "全1本" in (out / "index.md").read_text(encoding="utf-8")


class TestWriteOutputs:
    def test_category_file_lists_member_videos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "SUMMARIES_DIR", tmp_path / "summaries")
        items = [{"title": "動画A"}, {"title": "動画B"}]
        p = categorize._write_category_file("CH", "服装・コーデ", "- まとめ本文", items)
        text = p.read_text(encoding="utf-8")
        assert "# 服装・コーデ" in text
        assert "対象動画: 2本" in text
        assert "- 動画A" in text and "- 動画B" in text

    def test_index_links_each_category(self, tmp_path, monkeypatch):
        monkeypatch.setattr(categorize, "SUMMARIES_DIR", tmp_path / "summaries")
        p = categorize._write_index("CH", {"服装": [1, 2], "恋愛": [3]})
        text = p.read_text(encoding="utf-8")
        assert "[服装](服装.md) — 2本" in text
        assert "[恋愛](恋愛.md) — 1本" in text
        assert "全3本" in text

    def test_category_filename_is_sanitized(self, tmp_path, monkeypatch):
        # カテゴリ名にスラッシュが混ざってもディレクトリを掘らないこと
        monkeypatch.setattr(categorize, "SUMMARIES_DIR", tmp_path / "summaries")
        p = categorize._write_category_file("CH", "服装/コーデ", "- 本文", [{"title": "A"}])
        assert p.parent == tmp_path / "summaries" / "CH"
        assert "/" not in p.name.replace(".md", "")
