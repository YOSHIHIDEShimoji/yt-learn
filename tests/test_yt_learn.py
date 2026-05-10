import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import yt_learn


# ── _sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_removes_forbidden_chars(self):
        assert yt_learn._sanitize('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_strips_whitespace(self):
        assert yt_learn._sanitize("  hello  ") == "hello"

    def test_truncates_at_200(self):
        assert len(yt_learn._sanitize("a" * 300)) == 200

    def test_normal_string_unchanged(self):
        assert yt_learn._sanitize("メンタリストDAIGO") == "メンタリストDAIGO"


# ── _load_env ─────────────────────────────────────────────────────────────────

class TestLoadEnv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("GEMINI_API_KEY=test123\n")
        monkeypatch.setattr(yt_learn, "BASE_DIR", tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        yt_learn._load_env()
        assert os.environ["GEMINI_API_KEY"] == "test123"

    def test_skips_comments(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# this is a comment\nFOO=bar\n")
        monkeypatch.setattr(yt_learn, "BASE_DIR", tmp_path)
        monkeypatch.delenv("FOO", raising=False)
        yt_learn._load_env()
        assert os.environ.get("FOO") == "bar"

    def test_no_file_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "BASE_DIR", tmp_path)
        yt_learn._load_env()  # should not raise

    def test_does_not_override_existing_env(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("GEMINI_API_KEY=from_file\n")
        monkeypatch.setattr(yt_learn, "BASE_DIR", tmp_path)
        monkeypatch.setenv("GEMINI_API_KEY", "already_set")
        yt_learn._load_env()
        assert os.environ["GEMINI_API_KEY"] == "already_set"


# ── _load_channels / _add_channel ─────────────────────────────────────────────

class TestChannels:
    def _setup(self, tmp_path, monkeypatch):
        channels_file = tmp_path / "channels.txt"
        monkeypatch.setattr(yt_learn, "CHANNELS_FILE", channels_file)
        return channels_file

    def test_load_empty_file(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        assert yt_learn._load_channels() == {}

    def test_load_parses_entries(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("DAIGO | https://youtube.com/@daigo\n# comment\nFoo | https://youtube.com/@foo\n")
        result = yt_learn._load_channels()
        assert result == {
            "DAIGO": "https://youtube.com/@daigo",
            "Foo": "https://youtube.com/@foo",
        }

    def test_load_skips_malformed_lines(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("no pipe here\nOK | https://example.com\n")
        assert yt_learn._load_channels() == {"OK": "https://example.com"}

    def test_load_no_file_returns_empty(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert yt_learn._load_channels() == {}

    def test_add_channel_appends(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        yt_learn._add_channel("DAIGO", "https://youtube.com/@daigo")
        assert "DAIGO | https://youtube.com/@daigo" in f.read_text()

    def test_add_channel_skips_duplicate(self, tmp_path, monkeypatch, capsys):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("DAIGO | https://youtube.com/@daigo\n")
        yt_learn._add_channel("DAIGO", "https://youtube.com/@daigo2")
        assert f.read_text().count("DAIGO") == 1
        assert "既に登録済み" in capsys.readouterr().err

    def test_add_multiple_channels(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        yt_learn._add_channel("A", "https://a.com")
        yt_learn._add_channel("B", "https://b.com")
        result = yt_learn._load_channels()
        assert result == {"A": "https://a.com", "B": "https://b.com"}


# ── _save_transcript ──────────────────────────────────────────────────────────

class TestSaveTranscript:
    def test_creates_file_with_correct_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        path = yt_learn._save_transcript("DAIGO", "タイトル", "https://youtu.be/xxx", "文字起こし本文")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# タイトル" in content
        assert "チャンネル: DAIGO" in content
        assert "URL: https://youtu.be/xxx" in content
        assert "文字起こし本文" in content

    def test_creates_channel_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        yt_learn._save_transcript("NewChannel", "動画", "https://youtu.be/xxx", "text")
        assert (tmp_path / "transcripts" / "NewChannel").is_dir()

    def test_filename_is_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        path = yt_learn._save_transcript("CH", "a/b:c", "https://youtu.be/x", "text")
        assert "/" not in path.name
        assert ":" not in path.name


# ── _get_channel_videos ───────────────────────────────────────────────────────

class TestGetChannelVideos:
    def test_returns_video_list(self):
        mock_info = {
            "entries": [
                {"id": "abc", "title": "動画1", "url": "https://youtube.com/watch?v=abc"},
                {"id": "def", "title": "動画2", "url": "https://youtube.com/watch?v=def"},
            ]
        }
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = yt_learn._get_channel_videos("https://youtube.com/@test")
        assert len(result) == 2
        assert result[0]["title"] == "動画1"
        assert result[1]["url"] == "https://youtube.com/watch?v=def"

    def test_skips_none_entries(self):
        mock_info = {"entries": [None, {"id": "abc", "title": "動画1", "url": "https://youtube.com/watch?v=abc"}]}
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = yt_learn._get_channel_videos("https://youtube.com/@test")
        assert len(result) == 1

    def test_builds_youtube_url_from_id(self):
        mock_info = {"entries": [{"id": "xyz123", "title": "動画", "url": "xyz123"}]}
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = yt_learn._get_channel_videos("https://youtube.com/@test")
        assert result[0]["url"] == "https://www.youtube.com/watch?v=xyz123"

    def test_returns_empty_on_failure(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = None
            result = yt_learn._get_channel_videos("https://youtube.com/@test")
        assert result == []


# ── _process_url ──────────────────────────────────────────────────────────────

class TestProcessUrl:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    def test_skips_existing_transcript(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        existing = tmp_path / "transcripts" / "CH" / "動画.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("already exists")
        result = yt_learn._process_url("https://youtu.be/xxx", "CH", title="動画")
        assert result is False

    def test_processes_new_url(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(yt_learn, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(yt_learn, "_transcribe", return_value="文字起こし結果"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            result = yt_learn._process_url("https://youtu.be/xxx", "CH", title="新しい動画")
        assert result is True
        saved = tmp_path / "transcripts" / "CH" / "新しい動画.md"
        assert saved.exists()
        assert "文字起こし結果" in saved.read_text(encoding="utf-8")

    def test_fetches_title_if_not_provided(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(yt_learn, "_get_video_title", return_value="取得したタイトル") as mock_title, \
             patch.object(yt_learn, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(yt_learn, "_transcribe", return_value="text"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            yt_learn._process_url("https://youtu.be/xxx", "CH")
        mock_title.assert_called_once_with("https://youtu.be/xxx")

    def test_cleans_up_tmpdir_on_error(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(yt_learn, "_download_audio", side_effect=RuntimeError("DL失敗")), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree") as mock_rm:
            with pytest.raises(RuntimeError):
                yt_learn._process_url("https://youtu.be/xxx", "CH", title="動画")
        mock_rm.assert_called_once()


# ── _process_channel ──────────────────────────────────────────────────────────

class TestProcessChannel:
    def test_processes_new_skips_existing(self, tmp_path, monkeypatch):
        # スキップは _process_url 内部で行われるため _process_channel は両方を呼ぶ
        # _process_url 自体が既存ファイル検出時に False を返す挙動を検証する
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        existing = tmp_path / "transcripts" / "CH" / "既存動画.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("exists")

        videos = [
            {"title": "既存動画", "url": "https://youtu.be/a"},
            {"title": "新規動画", "url": "https://youtu.be/b"},
        ]
        # _process_url をモックせず実際のスキップロジックを通す
        # 外部呼び出し(_download_audio, _transcribe)だけをモック
        with patch.object(yt_learn, "_get_channel_videos", return_value=videos), \
             patch.object(yt_learn, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(yt_learn, "_transcribe", return_value="文字起こし"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            count = yt_learn._process_channel("CH", "https://youtube.com/@ch")

        # 既存動画はスキップ、新規動画のみ処理 → 処理数は1
        assert count == 1
        assert (tmp_path / "transcripts" / "CH" / "新規動画.md").exists()
        assert (tmp_path / "transcripts" / "CH" / "既存動画.md").read_text() == "exists"

    def test_applies_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [{"title": f"動画{i}", "url": f"https://youtu.be/{i}"} for i in range(10)]
        with patch.object(yt_learn, "_get_channel_videos", return_value=videos), \
             patch.object(yt_learn, "_process_url", return_value=True) as mock_proc:
            yt_learn._process_channel("CH", "https://youtube.com/@ch", limit=3)
        assert mock_proc.call_count == 3

    def test_continues_on_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yt_learn, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [
            {"title": "動画A", "url": "https://youtu.be/a"},
            {"title": "動画B", "url": "https://youtu.be/b"},
        ]
        with patch.object(yt_learn, "_get_channel_videos", return_value=videos), \
             patch.object(yt_learn, "_process_url", side_effect=[RuntimeError("失敗"), True]) as mock_proc:
            yt_learn._process_channel("CH", "https://youtube.com/@ch")
        assert mock_proc.call_count == 2
