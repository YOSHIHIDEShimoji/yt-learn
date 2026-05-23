import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import summarize


# ── _load_processed / _save_processed ────────────────────────────────────────

class TestProcessedTracking:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "SUMMARIES_DIR", tmp_path / "summaries")
        return tmp_path

    def test_load_empty_when_no_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert summarize._load_processed("CH") == set()

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "summaries").mkdir()
        processed = {"動画A.md", "動画B.md"}
        summarize._save_processed("CH", processed)
        loaded = summarize._load_processed("CH")
        assert loaded == processed

    def test_save_creates_summaries_dir(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        summarize._save_processed("CH", {"動画.md"})
        assert (tmp_path / "summaries").exists()

    def test_processed_file_is_sorted_json(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "summaries").mkdir()
        summarize._save_processed("CH", {"c.md", "a.md", "b.md"})
        p = tmp_path / "summaries" / "CH_processed.json"
        data = json.loads(p.read_text())
        assert data == ["a.md", "b.md", "c.md"]

    def test_sanitized_channel_name_used_for_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "summaries").mkdir()
        summarize._save_processed("CH/bad:name", {"x.md"})
        files = list((tmp_path / "summaries").iterdir())
        assert all("/" not in f.name for f in files)


# ── _update_summary ───────────────────────────────────────────────────────────

class TestUpdateSummary:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "SUMMARIES_DIR", tmp_path / "summaries")
        return tmp_path

    def _make_mock_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.text = response_text
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        return mock_client

    def test_creates_summary_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        mock_client = self._make_mock_client("# CH - Learning Summary\n\n## キーインサイト\n- 洞察1")
        with patch("google.genai.Client", return_value=mock_client):
            summarize._update_summary("CH", "文字起こし", "動画タイトル", "fake_key", 1)
        summary = tmp_path / "summaries" / "CH.md"
        assert summary.exists()
        assert "洞察1" in summary.read_text(encoding="utf-8")

    def test_passes_existing_summary_to_prompt(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "summaries").mkdir()
        existing = tmp_path / "summaries" / "CH.md"
        existing.write_text("# 既存サマリー\n\n## キーインサイト\n- 既存の洞察")
        mock_client = self._make_mock_client("# CH - Learning Summary\n\n---\n最終更新: 2025-01-01\n動画数: 2")
        with patch("google.genai.Client", return_value=mock_client):
            summarize._update_summary("CH", "新しいテキスト", "動画", "fake_key", 2)
        prompt_arg = mock_client.models.generate_content.call_args[1]["contents"]
        assert "既存の洞察" in prompt_arg

    def test_prompt_includes_video_title(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        mock_client = self._make_mock_client("# CH\n\n---\n最終更新: 2025-01-01\n動画数: 1")
        with patch("google.genai.Client", return_value=mock_client):
            summarize._update_summary("CH", "テキスト", "特定の動画タイトル", "fake_key", 1)
        prompt_arg = mock_client.models.generate_content.call_args[1]["contents"]
        assert "特定の動画タイトル" in prompt_arg

    def test_uses_correct_model(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        mock_client = self._make_mock_client("result")
        with patch("google.genai.Client", return_value=mock_client):
            summarize._update_summary("CH", "text", "title", "fake_key", 1)
        call_kwargs = mock_client.models.generate_content.call_args[1]
        assert call_kwargs["model"] == summarize.GEMINI_MODEL


# ── _summarize_channel ────────────────────────────────────────────────────────

class TestSummarizeChannel:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(summarize, "SUMMARIES_DIR", tmp_path / "summaries")
        return tmp_path

    def _make_transcripts(self, tmp_path, channel: str, titles: list[str]) -> None:
        ch_dir = tmp_path / "transcripts" / channel
        ch_dir.mkdir(parents=True)
        for title in titles:
            (ch_dir / f"{title}.md").write_text(f"# {title}\n\n---\n\n文字起こし")

    def test_skips_already_processed(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B"])
        (tmp_path / "summaries").mkdir()
        summarize._save_processed("CH", {"動画A.md", "動画B.md"})
        with patch.object(summarize, "_update_summary") as mock_upd:
            summarize._summarize_channel("CH", "fake_key")
        mock_upd.assert_not_called()
        assert "未処理のトランスクリプトがありません" in capsys.readouterr().err

    def test_processes_only_new_transcripts(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B", "動画C"])
        (tmp_path / "summaries").mkdir()
        summarize._save_processed("CH", {"動画A.md"})
        with patch.object(summarize, "_update_summary") as mock_upd:
            summarize._summarize_channel("CH", "fake_key")
        assert mock_upd.call_count == 2
        processed_titles = {call[0][2] for call in mock_upd.call_args_list}
        assert processed_titles == {"動画B", "動画C"}

    def test_updates_processed_file_after_each_video(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B"])
        with patch.object(summarize, "_update_summary"):
            summarize._summarize_channel("CH", "fake_key")
        processed = summarize._load_processed("CH")
        assert "動画A.md" in processed
        assert "動画B.md" in processed

    def test_saves_progress_even_on_partial_error(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B"])
        with patch.object(summarize, "_update_summary", side_effect=[None, RuntimeError("Geminiエラー")]):
            summarize._summarize_channel("CH", "fake_key")
        processed = summarize._load_processed("CH")
        assert "動画A.md" in processed
        assert "動画B.md" not in processed

    def test_force_reprocesses_all(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B"])
        (tmp_path / "summaries").mkdir()
        summarize._save_processed("CH", {"動画A.md", "動画B.md"})
        with patch.object(summarize, "_update_summary") as mock_upd:
            summarize._summarize_channel("CH", "fake_key", force=True)
        assert mock_upd.call_count == 2

    def test_skips_when_no_transcripts_dir(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        summarize._summarize_channel("NONEXISTENT", "fake_key")
        assert "トランスクリプトなし" in capsys.readouterr().err

    def test_skips_when_empty_channel_dir(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "transcripts" / "CH").mkdir(parents=True)
        summarize._summarize_channel("CH", "fake_key")
        assert "0件" in capsys.readouterr().err

    def test_threshold_skips_below_count(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B", "動画C"])
        with patch.object(summarize, "_update_summary") as mock_upd:
            summarize._summarize_channel("CH", "fake_key", threshold=5)
        mock_upd.assert_not_called()
        assert "< 5 件" in capsys.readouterr().err

    def test_threshold_processes_at_or_above_count(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A", "動画B", "動画C"])
        with patch.object(summarize, "_update_summary") as mock_upd, \
             patch.object(summarize, "_notify"):
            summarize._summarize_channel("CH", "fake_key", threshold=3)
        assert mock_upd.call_count == 3

    def test_threshold_zero_always_processes(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A"])
        with patch.object(summarize, "_update_summary") as mock_upd, \
             patch.object(summarize, "_notify"):
            summarize._summarize_channel("CH", "fake_key", threshold=0)
        assert mock_upd.call_count == 1

    def test_notifies_on_new_summary(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A"])
        with patch.object(summarize, "_update_summary"), \
             patch.object(summarize, "_notify") as mock_notify:
            summarize._summarize_channel("CH", "fake_key")
        mock_notify.assert_called_once()
        assert "作成" in mock_notify.call_args[0][0]

    def test_notifies_on_updated_summary(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A"])
        (tmp_path / "summaries").mkdir()
        (tmp_path / "summaries" / "CH.md").write_text("既存サマリー")
        with patch.object(summarize, "_update_summary"), \
             patch.object(summarize, "_notify") as mock_notify:
            summarize._summarize_channel("CH", "fake_key")
        mock_notify.assert_called_once()
        assert "更新" in mock_notify.call_args[0][0]

    def test_no_notify_when_all_errors(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._make_transcripts(tmp_path, "CH", ["動画A"])
        with patch.object(summarize, "_update_summary", side_effect=RuntimeError("失敗")), \
             patch.object(summarize, "_notify") as mock_notify:
            summarize._summarize_channel("CH", "fake_key")
        mock_notify.assert_not_called()


# ── CLI ───────────────────────────────────────────────────────────────────────

class TestCLI:
    def test_exits_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "BASE_DIR", tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        with patch("sys.argv", ["summarize.py", "DAIGO"]):
            with pytest.raises(SystemExit) as exc_info:
                summarize.main()
        assert exc_info.value.code == 1

    def test_exits_with_no_channels_on_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(summarize, "BASE_DIR", tmp_path)
        monkeypatch.setattr(summarize, "CHANNELS_FILE", tmp_path / "channels.txt")
        monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
        (tmp_path / "channels.txt").write_text("")
        with patch("sys.argv", ["summarize.py", "all"]):
            with pytest.raises(SystemExit) as exc_info:
                summarize.main()
        assert exc_info.value.code == 0
