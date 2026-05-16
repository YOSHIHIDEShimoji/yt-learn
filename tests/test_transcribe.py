import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import transcribe


# ── _sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_removes_forbidden_chars(self):
        assert transcribe._sanitize('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_strips_whitespace(self):
        assert transcribe._sanitize("  hello  ") == "hello"

    def test_truncates_at_200(self):
        assert len(transcribe._sanitize("a" * 300)) == 200

    def test_normal_string_unchanged(self):
        assert transcribe._sanitize("メンタリストDAIGO") == "メンタリストDAIGO"


# ── _load_env ─────────────────────────────────────────────────────────────────

class TestLoadEnv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("GEMINI_API_KEY=test123\n")
        monkeypatch.setattr(transcribe, "BASE_DIR", tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        transcribe._load_env()
        assert os.environ["GEMINI_API_KEY"] == "test123"

    def test_skips_comments(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# this is a comment\nFOO=bar\n")
        monkeypatch.setattr(transcribe, "BASE_DIR", tmp_path)
        monkeypatch.delenv("FOO", raising=False)
        transcribe._load_env()
        assert os.environ.get("FOO") == "bar"

    def test_no_file_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "BASE_DIR", tmp_path)
        transcribe._load_env()  # should not raise

    def test_does_not_override_existing_env(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("GEMINI_API_KEY=from_file\n")
        monkeypatch.setattr(transcribe, "BASE_DIR", tmp_path)
        monkeypatch.setenv("GEMINI_API_KEY", "already_set")
        transcribe._load_env()
        assert os.environ["GEMINI_API_KEY"] == "already_set"


# ── _load_channels / _add_channel ─────────────────────────────────────────────

class TestChannels:
    def _setup(self, tmp_path, monkeypatch):
        channels_file = tmp_path / "channels.txt"
        monkeypatch.setattr(transcribe, "CHANNELS_FILE", channels_file)
        return channels_file

    def test_load_empty_file(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        assert transcribe._load_channels() == {}

    def test_load_parses_entries(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("DAIGO | https://youtube.com/@daigo\n# comment\nFoo | https://youtube.com/@foo\n")
        result = transcribe._load_channels()
        assert result == {
            "DAIGO": {"url": "https://youtube.com/@daigo", "lang": "ja"},
            "Foo": {"url": "https://youtube.com/@foo", "lang": "ja"},
        }

    def test_load_parses_lang_field(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("TestCh | https://youtube.com/@test | en\n")
        result = transcribe._load_channels()
        assert result == {"TestCh": {"url": "https://youtube.com/@test", "lang": "en"}}

    def test_load_skips_malformed_lines(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("no pipe here\nOK | https://example.com\n")
        assert transcribe._load_channels() == {"OK": {"url": "https://example.com", "lang": "ja"}}

    def test_load_no_file_returns_empty(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert transcribe._load_channels() == {}

    def test_add_channel_appends(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        transcribe._add_channel("DAIGO", "https://youtube.com/@daigo")
        assert "DAIGO | https://youtube.com/@daigo | ja" in f.read_text()

    def test_add_channel_with_lang(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        transcribe._add_channel("TestCh", "https://youtube.com/@test", "en")
        assert "TestCh | https://youtube.com/@test | en" in f.read_text()

    def test_add_channel_skips_duplicate(self, tmp_path, monkeypatch, capsys):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("DAIGO | https://youtube.com/@daigo | ja\n")
        transcribe._add_channel("DAIGO", "https://youtube.com/@daigo2")
        assert f.read_text().count("DAIGO") == 1
        assert "既に登録済み" in capsys.readouterr().err

    def test_add_multiple_channels(self, tmp_path, monkeypatch):
        f = self._setup(tmp_path, monkeypatch)
        f.write_text("")
        transcribe._add_channel("A", "https://a.com")
        transcribe._add_channel("B", "https://b.com")
        result = transcribe._load_channels()
        assert result == {
            "A": {"url": "https://a.com", "lang": "ja"},
            "B": {"url": "https://b.com", "lang": "ja"},
        }


# ── _save_transcript ──────────────────────────────────────────────────────────

class TestSaveTranscript:
    def test_creates_file_with_correct_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        path = transcribe._save_transcript("DAIGO", "タイトル", "https://youtu.be/xxx", "文字起こし本文", model_size="tiny")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# タイトル" in content
        assert "チャンネル: DAIGO" in content
        assert "URL: https://youtu.be/xxx" in content
        assert "モデル: tiny" in content
        assert "処理日時:" in content
        assert "文字起こし本文" in content

    def test_creates_channel_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        transcribe._save_transcript("NewChannel", "動画", "https://youtu.be/xxx", "text")
        assert (tmp_path / "transcripts" / "NewChannel").is_dir()

    def test_filename_is_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        path = transcribe._save_transcript("CH", "a/b:c", "https://youtu.be/x", "text")
        assert "/" not in path.name
        assert ":" not in path.name

    def test_output_dir_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        custom = tmp_path / "custom"
        path = transcribe._save_transcript("CH", "動画", "https://youtu.be/x", "text", output_dir=custom)
        assert path.parent == custom
        assert not (tmp_path / "transcripts").exists()


# ── _load_view_cache / _save_view_cache / _sort_by_popularity ─────────────────

class TestViewCache:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")

    def test_load_empty_when_no_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert transcribe._load_view_cache("CH") == {}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        cache = {"abc": 1000, "def": 500}
        transcribe._save_view_cache("CH", cache)
        assert transcribe._load_view_cache("CH") == cache

    def test_save_creates_cache_dir(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        transcribe._save_view_cache("NewChannel", {"vid": 100})
        assert (tmp_path / "cache").exists()


class TestSortByPopularity:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")

    def test_sorts_by_view_count_descending(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [
            {"title": "低", "url": "https://youtu.be/low"},
            {"title": "高", "url": "https://youtu.be/high"},
            {"title": "中", "url": "https://youtu.be/mid"},
        ]
        transcribe._save_view_cache("CH", {"low": 100, "high": 9000, "mid": 500})
        result = transcribe._sort_by_popularity(videos, "CH", sample_size=0)
        assert [v["title"] for v in result] == ["高", "中", "低"]

    def test_fetches_counts_for_uncached_videos(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": "動画", "url": "https://youtu.be/abc123"}]
        with patch.object(transcribe, "_fetch_view_count", return_value=42000) as mock_fetch:
            transcribe._sort_by_popularity(videos, "CH", sample_size=10)
        mock_fetch.assert_called_once_with("abc123")

    def test_skips_cached_videos(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": "動画", "url": "https://youtu.be/abc123"}]
        transcribe._save_view_cache("CH", {"abc123": 5000})
        with patch.object(transcribe, "_fetch_view_count") as mock_fetch:
            transcribe._sort_by_popularity(videos, "CH", sample_size=10)
        mock_fetch.assert_not_called()

    def test_sample_size_limits_fetches(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": f"動画{i}", "url": f"https://youtu.be/vid{i}"} for i in range(5)]
        with patch.object(transcribe, "_fetch_view_count", return_value=0) as mock_fetch:
            transcribe._sort_by_popularity(videos, "CH", sample_size=2)
        assert mock_fetch.call_count == 2

    def test_sample_size_zero_fetches_all(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": f"動画{i}", "url": f"https://youtu.be/vid{i}"} for i in range(5)]
        with patch.object(transcribe, "_fetch_view_count", return_value=0) as mock_fetch:
            transcribe._sort_by_popularity(videos, "CH", sample_size=0)
        assert mock_fetch.call_count == 5

    def test_saves_cache_after_fetching(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": "動画", "url": "https://youtu.be/abc"}]
        with patch.object(transcribe, "_fetch_view_count", return_value=999):
            transcribe._sort_by_popularity(videos, "CH", sample_size=10)
        cache = transcribe._load_view_cache("CH")
        assert cache.get("abc") == 999

    def test_fetch_error_defaults_to_zero(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        videos = [{"title": "動画", "url": "https://youtu.be/abc"}]
        with patch.object(transcribe, "_fetch_view_count", side_effect=RuntimeError("API error")):
            result = transcribe._sort_by_popularity(videos, "CH", sample_size=10)
        assert len(result) == 1

    def test_popular_sort_returns_expected_top3(self, tmp_path, monkeypatch):
        # 新着順に並んだリストで人気3本が正しく先頭に来ることを確認
        self._setup(tmp_path, monkeypatch)
        videos = [
            {"title": "新しい動画A",                                          "url": "https://youtu.be/newvid01234"},
            {"title": "新しい動画B",                                          "url": "https://youtu.be/newvid56789"},
            {"title": "新しい動画C",                                          "url": "https://youtu.be/newvidabcde"},
            {"title": "【芸能界の闇】田村淳についてお話します。",               "url": "https://youtu.be/tgk9dFB5e9k"},
            {"title": "DaiGoがつけて人生変わった最強の癖TOP5",                 "url": "https://youtu.be/Ld8x6w9v6_8"},
            {"title": "京アニ実名報道を批判したら【テレビから連絡が来ました】", "url": "https://youtu.be/KyoAni_1234"},
        ]
        transcribe._save_view_cache("CH", {
            "newvid01234": 100,
            "newvid56789": 200,
            "newvidabcde": 300,
            "tgk9dFB5e9k": 5_000_000,
            "Ld8x6w9v6_8": 4_000_000,
            "KyoAni_1234": 3_000_000,
        })
        result = transcribe._sort_by_popularity(videos, "CH", sample_size=0)
        assert [v["title"] for v in result[:3]] == [
            "【芸能界の闇】田村淳についてお話します。",
            "DaiGoがつけて人生変わった最強の癖TOP5",
            "京アニ実名報道を批判したら【テレビから連絡が来ました】",
        ]


# ── _extract_video_id ────────────────────────────────────────────────────────

class TestExtractVideoId:
    def test_youtube_watch_url(self):
        assert transcribe._extract_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"

    def test_youtu_be_short_url(self):
        assert transcribe._extract_video_id("https://youtu.be/abc123") == "abc123"

    def test_youtube_url_with_extra_params(self):
        assert transcribe._extract_video_id("https://www.youtube.com/watch?v=abc123&t=30") == "abc123"

    def test_non_youtube_url_returns_url(self):
        url = "https://vimeo.com/123456"
        assert transcribe._extract_video_id(url) == url


# ── _load_index / _save_index ────────────────────────────────────────────────

class TestIndex:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    def test_load_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert transcribe._load_index("CH") == {}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        index = {"abc123": {"title": "動画", "url": "https://youtu.be/abc123", "file": "動画.md", "transcribed_at": "2025-01-01"}}
        transcribe._save_index("CH", index)
        assert transcribe._load_index("CH") == index

    def test_save_creates_channel_dir(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        transcribe._save_index("NewChannel", {})
        assert (tmp_path / "transcripts" / "NewChannel").exists()

    def test_index_file_is_valid_json(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        transcribe._save_index("CH", {"key": {"title": "タイトル"}})
        p = tmp_path / "transcripts" / "CH" / "_index.json"
        data = json.loads(p.read_text())
        assert data == {"key": {"title": "タイトル"}}


# ── _normalize_channel_url ───────────────────────────────────────────────────

class TestNormalizeChannelUrl:
    def test_appends_videos_tab_to_bare_channel(self):
        url = transcribe._normalize_channel_url("https://www.youtube.com/@daigo")
        assert url.endswith("/videos")

    def test_does_not_duplicate_videos_tab(self):
        url = transcribe._normalize_channel_url("https://www.youtube.com/@daigo/videos")
        assert url.count("/videos") == 1

    def test_strips_trailing_slash_before_appending(self):
        url = transcribe._normalize_channel_url("https://www.youtube.com/@daigo/")
        assert url == "https://www.youtube.com/@daigo/videos"

    def test_leaves_shorts_tab_unchanged(self):
        url = transcribe._normalize_channel_url("https://www.youtube.com/@daigo/shorts")
        assert "/videos" not in url
        assert url.endswith("/shorts")

    def test_leaves_streams_tab_unchanged(self):
        url = transcribe._normalize_channel_url("https://www.youtube.com/@daigo/streams")
        assert url.endswith("/streams")


# ── _get_channel_videos ───────────────────────────────────────────────────────

class TestGetChannelVideos:
    # YouTube video IDs are exactly 11 chars; use realistic-length IDs in fixtures
    def test_returns_video_list(self):
        mock_info = {
            "entries": [
                {"id": "abcdefghijk", "title": "動画1", "url": "https://youtube.com/watch?v=abcdefghijk"},
                {"id": "defghijklmn", "title": "動画2", "url": "https://youtube.com/watch?v=defghijklmn"},
            ]
        }
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = transcribe._get_channel_videos("https://youtube.com/@test")
        assert len(result) == 2
        assert result[0]["title"] == "動画1"
        assert result[1]["url"] == "https://youtube.com/watch?v=defghijklmn"

    def test_skips_none_entries(self):
        mock_info = {"entries": [None, {"id": "abcdefghijk", "title": "動画1", "url": "https://youtube.com/watch?v=abcdefghijk"}]}
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = transcribe._get_channel_videos("https://youtube.com/@test")
        assert len(result) == 1

    def test_skips_channel_tab_entries(self):
        # Channel tab entries have IDs like "UCxxxxxx..." (24 chars), not 11-char video IDs
        mock_info = {
            "entries": [
                {"id": "UCFdBehO71GQaIom4WfVeGSw", "title": "Videos", "url": "https://youtube.com/@test/videos"},
                {"id": "abcdefghijk", "title": "実際の動画", "url": "https://youtube.com/watch?v=abcdefghijk"},
            ]
        }
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = transcribe._get_channel_videos("https://youtube.com/@test")
        assert len(result) == 1
        assert result[0]["title"] == "実際の動画"

    def test_builds_youtube_url_from_id(self):
        mock_info = {"entries": [{"id": "xyz1234abcd", "title": "動画", "url": "xyz1234abcd"}]}
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = transcribe._get_channel_videos("https://youtube.com/@test")
        assert result[0]["url"] == "https://www.youtube.com/watch?v=xyz1234abcd"

    def test_appends_videos_tab_to_url(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = {"entries": []}
            transcribe._get_channel_videos("https://youtube.com/@test")
        called_url = mock_ydl.return_value.__enter__.return_value.extract_info.call_args[0][0]
        assert called_url.endswith("/videos")

    def test_returns_empty_on_failure(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = None
            result = transcribe._get_channel_videos("https://youtube.com/@test")
        assert result == []


# ── _process_url ──────────────────────────────────────────────────────────────

class TestProcessUrl:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")

    def test_skips_existing_by_url(self, tmp_path, monkeypatch):
        # 同じvideo IDが既にインデックスにある場合スキップ
        self._setup(tmp_path, monkeypatch)
        index = {"abc123": {"title": "動画", "url": "https://youtu.be/abc123", "file": "動画.md", "transcribed_at": "2025-01-01"}}
        transcribe._save_index("CH", index)
        result = transcribe._process_url("https://youtu.be/abc123", "CH", title="動画")
        assert result is False

    def test_same_video_different_url_form_is_skipped(self, tmp_path, monkeypatch):
        # youtu.be と youtube.com/watch?v= は同じIDとして扱う
        self._setup(tmp_path, monkeypatch)
        index = {"abc123": {"title": "動画", "url": "https://youtu.be/abc123", "file": "動画.md", "transcribed_at": "2025-01-01"}}
        transcribe._save_index("CH", index)
        result = transcribe._process_url("https://www.youtube.com/watch?v=abc123", "CH", title="動画")
        assert result is False

    def test_processes_new_url(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(transcribe, "_transcribe", return_value="文字起こし結果"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            result = transcribe._process_url("https://youtu.be/newvid", "CH", title="新しい動画")
        assert result is True
        saved = tmp_path / "transcripts" / "CH" / "新しい動画.md"
        assert saved.exists()
        assert "文字起こし結果" in saved.read_text(encoding="utf-8")

    def test_updates_index_after_processing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(transcribe, "_transcribe", return_value="text"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            transcribe._process_url("https://youtu.be/newvid", "CH", title="動画タイトル")
        index = transcribe._load_index("CH")
        assert "newvid" in index
        assert index["newvid"]["title"] == "動画タイトル"
        assert index["newvid"]["url"] == "https://youtu.be/newvid"
        assert "transcribed_at" in index["newvid"]

    def test_output_dir_overrides_default(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        custom_dir = tmp_path / "custom_output"
        with patch.object(transcribe, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(transcribe, "_transcribe", return_value="text"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            transcribe._process_url("https://youtu.be/vid1", "CH", title="動画", output_dir=custom_dir)
        assert (custom_dir / "動画.md").exists()
        assert not (tmp_path / "transcripts" / "CH" / "動画.md").exists()

    def test_fetches_title_if_not_provided(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_video_title", return_value="取得したタイトル") as mock_title, \
             patch.object(transcribe, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(transcribe, "_transcribe", return_value="text"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            transcribe._process_url("https://youtu.be/xxx", "CH")
        mock_title.assert_called_once_with("https://youtu.be/xxx")

    def test_cleans_up_tmpdir_on_error(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_download_audio", side_effect=RuntimeError("DL失敗")), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree") as mock_rm:
            with pytest.raises(RuntimeError):
                transcribe._process_url("https://youtu.be/xxx", "CH", title="動画")
        mock_rm.assert_called_once()


# ── _process_channel ──────────────────────────────────────────────────────────

# ── process CLI（URLファイル / -o オプション）────────────────────────────────

class TestProcessCLI:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "CHANNELS_FILE", tmp_path / "channels.txt")
        return tmp_path

    def _mock_process(self):
        return patch.object(transcribe, "_process_url", return_value=True)

    def test_url_file_is_read(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        url_file = tmp_path / "urls.txt"
        url_file.write_text("https://youtu.be/aaa\n# コメント\nhttps://youtu.be/bbb\n")
        with self._mock_process() as mock_proc, \
             patch("sys.argv", ["transcribe.py", "process", "--channel", "CH", "-f", str(url_file)]):
            transcribe.main()
        called_urls = [c[0][0] for c in mock_proc.call_args_list]
        assert called_urls == ["https://youtu.be/aaa", "https://youtu.be/bbb"]

    def test_urls_and_file_are_merged(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        url_file = tmp_path / "urls.txt"
        url_file.write_text("https://youtu.be/bbb\n")
        with self._mock_process() as mock_proc, \
             patch("sys.argv", ["transcribe.py", "process", "https://youtu.be/aaa",
                                "--channel", "CH", "-f", str(url_file)]):
            transcribe.main()
        called_urls = [c[0][0] for c in mock_proc.call_args_list]
        assert "https://youtu.be/aaa" in called_urls
        assert "https://youtu.be/bbb" in called_urls

    def test_no_urls_exits_with_error(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch("sys.argv", ["transcribe.py", "process", "--channel", "CH"]):
            with pytest.raises(SystemExit) as exc:
                transcribe.main()
        assert exc.value.code == 1

    def test_output_dir_passed_to_process_url(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        custom = tmp_path / "out"
        with self._mock_process() as mock_proc, \
             patch("sys.argv", ["transcribe.py", "process", "https://youtu.be/aaa",
                                "--channel", "CH", "-o", str(custom)]):
            transcribe.main()
        _, kwargs = mock_proc.call_args
        assert kwargs.get("output_dir") == custom

    def test_missing_url_file_exits(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch("sys.argv", ["transcribe.py", "process", "--channel", "CH",
                                "-f", str(tmp_path / "nonexistent.txt")]):
            with pytest.raises(SystemExit) as exc:
                transcribe.main()
        assert exc.value.code == 1


class TestProcessChannel:
    def test_processes_new_skips_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        # インデックスに既存動画を登録済みにしておく
        transcribe._save_index("CH", {
            "existid": {"title": "既存動画", "url": "https://youtu.be/existid", "file": "既存動画.md", "transcribed_at": "2025-01-01"}
        })

        videos = [
            {"title": "既存動画", "url": "https://youtu.be/existid"},
            {"title": "新規動画", "url": "https://youtu.be/newid"},
        ]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_download_audio", return_value="/tmp/audio.wav"), \
             patch.object(transcribe, "_transcribe", return_value="文字起こし"), \
             patch("tempfile.mkdtemp", return_value="/tmp/fake"), \
             patch("shutil.rmtree"):
            count = transcribe._process_channel("CH", "https://youtube.com/@ch", popular_sample=0)

        assert count == 1
        assert (tmp_path / "transcripts" / "CH" / "新規動画.md").exists()
        index = transcribe._load_index("CH")
        assert "newid" in index
        assert "existid" in index  # 既存エントリは保持

    def test_applies_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [{"title": f"動画{i}", "url": f"https://youtu.be/{i}"} for i in range(10)]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_process_url", return_value=True) as mock_proc:
            transcribe._process_channel("CH", "https://youtube.com/@ch", limit=3, popular_sample=0)
        assert mock_proc.call_count == 3

    def test_continues_on_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [
            {"title": "動画A", "url": "https://youtu.be/a"},
            {"title": "動画B", "url": "https://youtu.be/b"},
        ]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_process_url", side_effect=[RuntimeError("失敗"), True]) as mock_proc:
            transcribe._process_channel("CH", "https://youtube.com/@ch", popular_sample=0)
        assert mock_proc.call_count == 2

    def test_popular_sort_calls_sort_by_popularity(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [{"title": "動画A", "url": "https://youtu.be/a"}]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_sort_by_popularity", return_value=videos) as mock_sort, \
             patch.object(transcribe, "_process_url", return_value=True):
            transcribe._process_channel("CH", "https://youtube.com/@ch", sort="popular", popular_sample=100)
        mock_sort.assert_called_once_with(videos, "CH", 100)

    def test_cache_only_skips_processing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        videos = [{"title": "動画A", "url": "https://youtu.be/abcdefghijk"}]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_sort_by_popularity", return_value=videos) as mock_sort, \
             patch.object(transcribe, "_process_url") as mock_proc:
            transcribe._process_channel("CH", "https://youtube.com/@ch",
                                      sort="popular", popular_sample=0, cache_only=True)
        mock_sort.assert_called_once()
        mock_proc.assert_not_called()

    def test_date_sort_does_not_call_sort_by_popularity(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        videos = [{"title": "動画A", "url": "https://youtu.be/a"}]
        with patch.object(transcribe, "_get_channel_videos", return_value=videos), \
             patch.object(transcribe, "_sort_by_popularity") as mock_sort, \
             patch.object(transcribe, "_process_url", return_value=True):
            transcribe._process_channel("CH", "https://youtube.com/@ch", sort="date", popular_sample=0)
        mock_sort.assert_not_called()


# ── _is_wsl ───────────────────────────────────────────────────────────────────

class TestIsWsl:
    def test_returns_true_when_microsoft_in_proc_version(self):
        with patch("transcribe.Path") as mock_path_cls:
            mock_path_cls.return_value.read_text.return_value = "Linux version 5.15.0-microsoft-standard-WSL2"
            assert transcribe._is_wsl() is True

    def test_returns_false_when_no_microsoft(self):
        with patch("transcribe.Path") as mock_path_cls:
            mock_path_cls.return_value.read_text.return_value = "Linux version 5.15.0-generic"
            assert transcribe._is_wsl() is False

    def test_returns_false_on_file_not_found(self):
        with patch("transcribe.Path") as mock_path_cls:
            mock_path_cls.return_value.read_text.side_effect = FileNotFoundError
            assert transcribe._is_wsl() is False

    def test_returns_false_on_any_exception(self):
        with patch("transcribe.Path") as mock_path_cls:
            mock_path_cls.return_value.read_text.side_effect = PermissionError
            assert transcribe._is_wsl() is False


# ── _call_ollama ──────────────────────────────────────────────────────────────

class TestCallOllama:
    def _make_response(self, body: dict):
        import io
        raw = json.dumps(body).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_returns_response_text(self):
        mock_resp = self._make_response({"response": "## ポイント\n- 内容A", "done": True})
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = transcribe._call_ollama("prompt", "http://localhost:11434", "qwen3.5:9b")
        assert result == "## ポイント\n- 内容A"

    def test_returns_none_when_response_empty(self):
        mock_resp = self._make_response({"response": "", "done": True})
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = transcribe._call_ollama("prompt", "http://localhost:11434", "qwen3.5:9b")
        assert result is None

    def test_propagates_exception_on_timeout(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(TimeoutError):
                transcribe._call_ollama("prompt", "http://localhost:11434", "qwen3.5:9b")

    def test_sends_correct_payload(self):
        import urllib.request as urllib_req
        mock_resp = self._make_response({"response": "ok", "done": True})
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["url"] = req.full_url
            return mock_resp
        with patch("urllib.request.urlopen", fake_urlopen):
            transcribe._call_ollama("test prompt", "http://100.85.4.93:11434", "qwen3.5:9b")
        assert captured["payload"]["model"] == "qwen3.5:9b"
        assert captured["payload"]["prompt"] == "test prompt"
        assert captured["payload"]["stream"] is False
        assert captured["payload"]["think"] is False
        assert "100.85.4.93:11434" in captured["url"]


# ── _generate_core_summary (Ollama統合) ──────────────────────────────────────

class TestGenerateCoreSummaryOllama:
    def test_uses_ollama_when_local_url_set(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.5:9b")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch.object(transcribe, "_is_wsl", return_value=False), \
             patch.object(transcribe, "_call_ollama", return_value="## ポイント\n- テスト") as mock_ollama:
            result = transcribe._generate_core_summary("タイトル", "本文")
        assert result == "## ポイント\n- テスト"
        mock_ollama.assert_called_once()

    def test_falls_back_to_gemini_on_ollama_failure(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434")
        monkeypatch.setenv("GEMINI_API_KEY", "dummy_key")
        mock_gemini_resp = MagicMock()
        mock_gemini_resp.text = "## ポイント\n- Gemini結果"
        mock_genai = MagicMock()
        mock_genai.Client.return_value.models.generate_content.return_value = mock_gemini_resp
        with patch.object(transcribe, "_is_wsl", return_value=False), \
             patch.object(transcribe, "_call_ollama", side_effect=ConnectionError("refused")), \
             patch.dict("sys.modules", {"google.genai": mock_genai, "google": MagicMock(genai=mock_genai)}):
            result = transcribe._generate_core_summary("タイトル", "本文")
        assert result == "## ポイント\n- Gemini結果"

    def test_uses_ollama_on_wsl(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.5:9b")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch.object(transcribe, "_is_wsl", return_value=True), \
             patch.object(transcribe, "_call_ollama", return_value="## ポイント\n- WSL経由Ollama") as mock_ollama:
            result = transcribe._generate_core_summary("タイトル", "本文")
        mock_ollama.assert_called_once()
        assert result == "## ポイント\n- WSL経由Ollama"

    def test_returns_none_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = transcribe._generate_core_summary("タイトル", "本文")
        assert result is None

    def test_uses_custom_model_from_env(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://100.85.4.93:11434")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "custom-model:latest")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch.object(transcribe, "_is_wsl", return_value=False), \
             patch.object(transcribe, "_call_ollama", return_value="## ポイント\n- テスト") as mock_ollama:
            transcribe._generate_core_summary("タイトル", "本文")
        assert mock_ollama.call_args[0][2] == "custom-model:latest"

    def test_ollama_empty_response_no_gemini_returns_none(self, monkeypatch):
        # Ollama が空返答 + GEMINI_API_KEY 未設定 → None
        monkeypatch.setenv("LOCAL_LLM_URL", "http://100.85.4.93:11434")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch.object(transcribe, "_is_wsl", return_value=False), \
             patch.object(transcribe, "_call_ollama", return_value=None):
            result = transcribe._generate_core_summary("タイトル", "本文")
        assert result is None
