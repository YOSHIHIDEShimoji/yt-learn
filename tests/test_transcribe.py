import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
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
             patch.object(transcribe, "_inject_core_summary"), \
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
             patch.object(transcribe, "_inject_core_summary"), \
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
             patch.object(transcribe, "_inject_core_summary"), \
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
             patch.object(transcribe, "_inject_core_summary"), \
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
        # main() は _load_env() で実 .env を os.environ に流し込む。BASE_DIR を差し替えて
        # おかないと LOCAL_LLM_URL がプロセス全体に残り、後続テストが実 Ollama に
        # 到達して「たまたま緑」になる（実行順序に依存する偽の緑）
        monkeypatch.setattr(transcribe, "BASE_DIR", tmp_path)
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
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
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"), \
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


# ── _generate_core_summary (Ollama専用) ──────────────────────────────────────

class TestGenerateCoreSummaryOllama:
    def test_uses_ollama_when_local_url_set(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.5:9b")
        with patch.object(transcribe, "_call_ollama", return_value="## ポイント\n- テスト") as mock_ollama:
            text, backend = transcribe._generate_core_summary("タイトル", "本文")
        assert text == "## ポイント\n- テスト"
        assert "Ollama" in backend
        mock_ollama.assert_called_once()

    def test_raises_when_local_url_unset(self, monkeypatch):
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        with pytest.raises(RuntimeError, match="LOCAL_LLM_URL"):
            transcribe._generate_core_summary("タイトル", "本文")

    def test_raises_on_ollama_failure(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434")
        with patch.object(transcribe, "_call_ollama", side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                transcribe._generate_core_summary("タイトル", "本文")

    def test_raises_on_empty_ollama_response(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://100.85.4.93:11434")
        with patch.object(transcribe, "_call_ollama", return_value=None):
            with pytest.raises(RuntimeError, match="空"):
                transcribe._generate_core_summary("タイトル", "本文")

    def test_uses_custom_model_from_env(self, monkeypatch):
        monkeypatch.setenv("LOCAL_LLM_URL", "http://100.85.4.93:11434")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "custom-model:latest")
        with patch.object(transcribe, "_call_ollama", return_value="## ポイント\n- テスト") as mock_ollama:
            transcribe._generate_core_summary("タイトル", "本文")
        assert mock_ollama.call_args[0][2] == "custom-model:latest"


def _videos(n):
    """新着順（降順）に並んだ動画リストを作る。index が小さいほど新しい。"""
    return [{"title": f"動画{i}", "url": f"https://www.youtube.com/watch?v=vid{i:03d}"}
            for i in range(n)]


class TestResolveVideoIndex:
    def test_finds_by_id(self):
        assert transcribe._resolve_video_index(_videos(5), "vid003") == 3

    def test_finds_by_watch_url(self):
        vs = _videos(5)
        assert transcribe._resolve_video_index(vs, "https://www.youtube.com/watch?v=vid002") == 2

    def test_finds_by_short_url(self):
        # youtu.be 形式でも同じ動画として解決できること
        assert transcribe._resolve_video_index(_videos(5), "https://youtu.be/vid004") == 4

    def test_returns_minus_one_when_absent(self):
        assert transcribe._resolve_video_index(_videos(5), "nosuchvid") == -1


class TestFilterByRange:
    def _ids(self, videos):
        return [transcribe._extract_video_id(v["url"]) for v in videos]

    def test_since_video_keeps_that_video_and_newer(self):
        # 新着順リストなので「以降(=より新しい)」は先頭側。既定は境界を含む
        out = transcribe._filter_by_range(_videos(10), "CH", since_video="vid003")
        assert self._ids(out) == ["vid000", "vid001", "vid002", "vid003"]

    def test_since_video_exclusive_drops_the_boundary_video(self):
        out = transcribe._filter_by_range(_videos(10), "CH", since_video="vid003", exclusive=True)
        assert self._ids(out) == ["vid000", "vid001", "vid002"]

    def test_until_video_keeps_that_video_and_older(self):
        out = transcribe._filter_by_range(_videos(6), "CH", until_video="vid003")
        assert self._ids(out) == ["vid003", "vid004", "vid005"]

    def test_until_video_exclusive_drops_the_boundary_video(self):
        out = transcribe._filter_by_range(_videos(6), "CH", until_video="vid003", exclusive=True)
        assert self._ids(out) == ["vid004", "vid005"]

    def test_since_and_until_intersect(self):
        out = transcribe._filter_by_range(_videos(10), "CH",
                                          since_video="vid006", until_video="vid003")
        assert self._ids(out) == ["vid003", "vid004", "vid005", "vid006"]

    def test_no_filter_returns_everything(self):
        out = transcribe._filter_by_range(_videos(4), "CH")
        assert len(out) == 4

    def test_empty_range_returns_empty_list(self):
        # until が since より新しい側にある＝交差なし
        out = transcribe._filter_by_range(_videos(10), "CH",
                                          since_video="vid002", until_video="vid007")
        assert out == []

    def test_unknown_since_video_raises(self):
        with pytest.raises(ValueError, match="since-video"):
            transcribe._filter_by_range(_videos(5), "CH", since_video="nosuchvid")

    def test_unknown_until_video_raises(self):
        with pytest.raises(ValueError, match="until-video"):
            transcribe._filter_by_range(_videos(5), "CH", until_video="nosuchvid")


class TestResolveDateIndex:
    # 新着順に並んだ10本。index 0..4 が 2024年、5..9 が 2023年
    DATES = ["2024-06-01", "2024-05-01", "2024-04-01", "2024-03-01", "2024-02-01",
             "2023-12-01", "2023-11-01", "2023-10-01", "2023-09-01", "2023-08-01"]

    def _setup(self, tmp_path, monkeypatch, unavailable=()):
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe.time, "sleep", lambda *_: None)
        calls = []

        def fake_fetch(video_id):
            calls.append(video_id)
            i = int(video_id.replace("vid", ""))
            return None if i in unavailable else self.DATES[i]

        monkeypatch.setattr(transcribe, "_fetch_upload_date", fake_fetch)
        return calls

    def test_finds_boundary(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        k = transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01")
        assert k == 5  # videos[0:5] が 2024-01-01 以降

    def test_boundary_date_is_included_when_inclusive(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        k = transcribe._resolve_date_index(_videos(10), "CH", "2024-02-01", inclusive=True)
        assert k == 5  # 当日(index 4)を新しい側に含める

    def test_boundary_date_is_excluded_when_not_inclusive(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        k = transcribe._resolve_date_index(_videos(10), "CH", "2024-02-01", inclusive=False)
        assert k == 4  # 当日(index 4)を古い側に落とす

    def test_uses_far_fewer_fetches_than_full_scan(self, tmp_path, monkeypatch):
        # 二分探索である＝全件取得しない。これがレートリミット回避の肝
        calls = self._setup(tmp_path, monkeypatch)
        transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01")
        assert len(calls) <= 4

    def test_falls_back_to_neighbour_when_video_is_unavailable(self, tmp_path, monkeypatch):
        # メンバー限定などで日付が取れない動画に当たっても境界が求まること
        self._setup(tmp_path, monkeypatch, unavailable={2, 3, 4})
        k = transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01")
        assert k == 5

    def test_judges_by_the_index_the_date_came_from(self, tmp_path, monkeypatch):
        """代用した日付は「その日付が取れた index」で判定すること。

        近傍代用の結果を mid の日付とみなすと、代用元が境界の反対側だった場合に
        境界が新しい側へずれ、DL可能な動画が黙って対象から落ちる。
        不能帯が境界（index 5）をまたぐこの配置でしか差が出ない。
        """
        self._setup(tmp_path, monkeypatch, unavailable={4, 5, 6})
        k = transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01",
                                           inclusive=True, widen="newer")
        # 正しい実装は不能動画を安全側（新しい側）に残して 7。
        # mid で判定する実装は 5 を返し、index 5・6 を取りこぼす
        assert k == 7

    def test_caches_fetched_dates_across_calls(self, tmp_path, monkeypatch):
        calls = self._setup(tmp_path, monkeypatch)
        transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01")
        first = len(calls)
        transcribe._resolve_date_index(_videos(10), "CH", "2024-01-01")
        assert len(calls) == first  # 2回目は1件も取りに行かない

    def test_transient_failure_is_not_cached_as_unavailable(self, tmp_path, monkeypatch):
        # レートリミット等の一時障害を「日付が取れない動画」として恒久保存すると、
        # 以後ずっと二分探索が狂う。キャッシュに毒を入れないこと
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe.time, "sleep", lambda *_: None)
        state = {"fail": True}

        def flaky(video_id):
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("rate-limited")
            return "2024-06-01"

        monkeypatch.setattr(transcribe, "_fetch_upload_date", flaky)
        transcribe._resolve_date_index(_videos(4), "CH", "2024-01-01")
        cache = transcribe._load_date_cache("CH")
        assert None not in cache.values()


class TestResolveDateIndexLongUnavailableRun:
    """探索窓を超える長さの「日付が取れない帯」に当たっても対象を落とさないこと。

    メンバー限定は時期的にクラスタするので、固定幅（±5 等）で近傍代用を打ち切ると
    窓の残りを未探査のまま切り捨て、DL可能な動画が黙って対象から外れる。
    """

    def _dates(self, n):
        from datetime import datetime, timedelta
        base = datetime(2026, 1, 1)
        return [(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    def _setup(self, tmp_path, monkeypatch, n, unavailable):
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe.time, "sleep", lambda *_: None)
        dates = self._dates(n)

        def fake(video_id):
            i = int(video_id.replace("vid", ""))
            return None if i in unavailable else dates[i]

        monkeypatch.setattr(transcribe, "_fetch_upload_date", fake)
        return dates

    def test_boundary_survives_11_consecutive_unavailable(self, tmp_path, monkeypatch):
        dates = self._setup(tmp_path, monkeypatch, 40, set(range(15, 26)))
        # index 10 より新しい（＝ index 0..9）が対象。inclusive=False で境界当日を落とす
        k = transcribe._resolve_date_index(_videos(40), "CH", dates[10],
                                           inclusive=False, widen="older")
        assert k == 10

    def test_all_unavailable_keeps_more_for_after(self, tmp_path, monkeypatch):
        # --after は k=end。判定不能なら k を大きく＝対象を広く残す
        self._setup(tmp_path, monkeypatch, 20, set(range(20)))
        k = transcribe._resolve_date_index(_videos(20), "CH", "2025-01-01",
                                           inclusive=True, widen="newer")
        assert k == 20

    def test_all_unavailable_keeps_more_for_before(self, tmp_path, monkeypatch):
        # --before は k=start。判定不能なら k を小さく＝対象を広く残す
        self._setup(tmp_path, monkeypatch, 20, set(range(20)))
        k = transcribe._resolve_date_index(_videos(20), "CH", "2025-01-01",
                                           inclusive=False, widen="older")
        assert k == 0

    def test_terminates_on_large_input(self, tmp_path, monkeypatch):
        # 無限ループしないこと（半数がランダムに不能でも必ず返る）
        self._setup(tmp_path, monkeypatch, 200, set(range(0, 200, 2)))
        k = transcribe._resolve_date_index(_videos(200), "CH", "2025-09-01")
        assert 0 <= k <= 200


class TestFilterByRangeDates:
    """--after / --before の意味論（境界日の帰属）と widen の向きを固定する。

    _resolve_date_index 単体が正しくても、呼び出し側の inclusive の渡し方を取り違えると
    境界日の動画が黙って落ちる。ここを押さえないと配線の変異が素通りする。
    """
    DATES = ["2024-06-01", "2024-05-01", "2024-04-01", "2024-03-01", "2024-02-01",
             "2023-12-01", "2023-11-01", "2023-10-01", "2023-09-01", "2023-08-01"]

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe.time, "sleep", lambda *_: None)
        monkeypatch.setattr(transcribe, "_fetch_upload_date",
                            lambda vid: self.DATES[int(vid.replace("vid", ""))])

    def _ids(self, videos):
        return [transcribe._extract_video_id(v["url"]) for v in videos]

    def test_after_includes_videos_posted_on_the_boundary_day(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = transcribe._filter_by_range(_videos(10), "CH", after="2024-02-01")
        assert self._ids(out) == [f"vid{i:03d}" for i in range(5)]  # 当日(index 4)を含む

    def test_before_includes_videos_posted_on_the_boundary_day(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = transcribe._filter_by_range(_videos(10), "CH", before="2024-02-01")
        assert self._ids(out) == [f"vid{i:03d}" for i in range(4, 10)]  # 当日(index 4)を含む

    def test_after_and_before_intersect(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = transcribe._filter_by_range(_videos(10), "CH",
                                          after="2024-03-01", before="2024-05-01")
        assert self._ids(out) == ["vid001", "vid002", "vid003"]

    def test_date_and_video_bounds_combine(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = transcribe._filter_by_range(_videos(10), "CH",
                                          since_video="vid005", after="2024-03-01")
        assert self._ids(out) == ["vid000", "vid001", "vid002", "vid003"]


class TestProcessChannelRange:
    """channel 経路にも範囲指定と --dry-run が効いていること。

    --download-only 側は別途テスト済みだが、通常経路の配線が外れると
    `--since-video` が黙って全件処理に化ける（レートリミットとGPU時間の実害）。
    """

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")

    def test_since_video_limits_what_is_processed(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(10)), \
             patch.object(transcribe, "_process_url", return_value=True) as proc:
            transcribe._process_channel("CH", "u", popular_sample=0, since_video="vid002")
        called = [c[0][0] for c in proc.call_args_list]
        assert called == [f"https://www.youtube.com/watch?v=vid{i:03d}" for i in range(3)]

    def test_dry_run_downloads_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(10)), \
             patch.object(transcribe, "_process_url", return_value=True) as proc:
            n = transcribe._process_channel("CH", "u", popular_sample=0, dry_run=True)
        assert proc.call_count == 0
        assert n == 0

    def test_video_quality_is_forwarded(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(1)), \
             patch.object(transcribe, "_process_url", return_value=True) as proc:
            transcribe._process_channel("CH", "u", popular_sample=0, video_quality="360p")
        assert proc.call_args.kwargs["video_quality"] == "360p"


class TestMembersOnlySentinel:
    """メンバー限定を view キャッシュに刻んで次回以降スキップすること。

    刻まないと毎周回 DL を試みてレートリミット枠を浪費する。このチャンネルは
    対象の43%がメンバー限定なので影響が大きい。
    """
    ERR = "ERROR: [youtube] x: Join this channel to get access to members-only content"

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe, "QUEUE_DIR", tmp_path / "queue")

    def test_process_channel_records_sentinel_and_continues(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        calls = []

        def _proc(url, *a, **kw):
            calls.append(url)
            if url.endswith("vid000"):
                raise RuntimeError(self.ERR)
            return True

        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(3)), \
             patch.object(transcribe, "_process_url", side_effect=_proc):
            transcribe._process_channel("CH", "u", popular_sample=0)
        assert len(calls) == 3  # break せず後続へ進む
        assert transcribe._load_view_cache("CH")["vid000"] == -1

    def test_sentinel_excludes_video_on_the_next_run(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        transcribe._save_view_cache("CH", {"vid000": -1})
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(3)), \
             patch.object(transcribe, "_process_url", return_value=True) as proc:
            transcribe._process_channel("CH", "u", popular_sample=0)
        called = [c[0][0] for c in proc.call_args_list]
        assert all("vid000" not in u for u in called)

    def test_rate_limit_takes_precedence_over_members_check(self, tmp_path, monkeypatch):
        # レートリミットは中断、メンバー限定は継続。判定順を入れ替えると全件走り続ける
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(3)), \
             patch.object(transcribe, "_process_url",
                          side_effect=RuntimeError("rate-limited")) as proc:
            transcribe._process_channel("CH", "u", popular_sample=0)
        assert proc.call_count == 1  # 1本目で中断

    def test_queue_path_records_sentinel(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(transcribe, "DELIVER_DIR", tmp_path / "deliver")
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(2)), \
             patch.object(transcribe, "_download_audio", side_effect=RuntimeError(self.ERR)):
            added, limited = transcribe._download_channel_to_queue("CH", "u", sort="date")
        assert (added, limited) == (0, False)
        assert transcribe._load_view_cache("CH") == {"vid000": -1, "vid001": -1}


class TestVideoQualityDownload:
    def test_360p_uses_single_file_format(self):
        # format 18 は映像+音声が1ファイル。マージ不要＝ffmpeg 不要で最速
        assert transcribe._VIDEO_FORMATS["360p"].startswith("18/")

    def test_higher_qualities_merge_video_and_audio(self):
        assert "+bestaudio" in transcribe._VIDEO_FORMATS["720p"]

    @pytest.mark.parametrize("quality", ["360p", "720p", "1080p", "best"])
    def test_every_quality_falls_back_to_audio_only(self, quality):
        # combined フォーマットが無い動画で「フォーマットが無い」と失敗させると、
        # 動画だけでなく文字起こしまで落ちる。必ず音声で拾えること
        assert transcribe._VIDEO_FORMATS[quality].endswith(transcribe._AUDIO_FALLBACK)


class TestDownloadReturnsOwnFile:
    """共有ディレクトリでも「今DLした動画」を返すこと。

    out_dir を走査して拡張子が合った最初のファイルを返す実装だと、
    --download-only の queue/（永続共有）では別動画のファイルが返り、
    「ファイル名は正しいのに中身が別動画」の納品物が無音で量産される。
    """

    def _fake_ydl(self, tmp_path, monkeypatch, produced: str):
        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, urls):
                (tmp_path / produced).write_bytes(b"correct")

        fake_mod = MagicMock()
        fake_mod.YoutubeDL = FakeYDL
        monkeypatch.setitem(sys.modules, "yt_dlp", fake_mod)

    def test_returns_file_matching_video_id(self, tmp_path, monkeypatch):
        # 先に他の動画のファイルが5本ある状態（drain 前の queue の常態）
        for other in ("aaa111", "bbb222", "ccc333", "ddd444", "eee555"):
            (tmp_path / f"{other}.mp4").write_bytes(b"WRONG")
        self._fake_ydl(tmp_path, monkeypatch, "zzz999.mp4")
        out = transcribe._download_audio("https://www.youtube.com/watch?v=zzz999",
                                         str(tmp_path), video_quality="360p")
        assert Path(out).name == "zzz999.mp4"
        assert Path(out).read_bytes() == b"correct"

    def test_audio_path_also_matches_video_id(self, tmp_path, monkeypatch):
        (tmp_path / "aaa111.m4a").write_bytes(b"WRONG")
        self._fake_ydl(tmp_path, monkeypatch, "zzz999.m4a")
        out = transcribe._download_audio("https://youtu.be/zzz999", str(tmp_path))
        assert Path(out).name == "zzz999.m4a"

    def test_falls_back_to_newly_created_file_only(self, tmp_path, monkeypatch):
        # ID が URL から取れない形式でも、既存ファイルを掴まないこと
        (tmp_path / "old.m4a").write_bytes(b"WRONG")
        self._fake_ydl(tmp_path, monkeypatch, "fresh.m4a")
        out = transcribe._download_audio("https://example.com/stream", str(tmp_path))
        assert Path(out).read_bytes() == b"correct"


class TestHasVideoStream:
    def test_trusts_info_json_over_extension(self, tmp_path):
        # 音声のみの .webm を「動画」と誤判定しないこと
        (tmp_path / "v1.info.json").write_text(json.dumps(
            {"requested_downloads": [{"vcodec": "none", "acodec": "opus"}]}), encoding="utf-8")
        (tmp_path / "v1.webm").write_bytes(b"a")
        assert transcribe._has_video_stream(str(tmp_path / "v1.webm"), "v1") is False

    def test_combined_webm_is_recognised_as_video(self, tmp_path):
        # 逆に combined の .webm を捨てないこと
        (tmp_path / "v1.info.json").write_text(json.dumps(
            {"requested_downloads": [{"vcodec": "vp9", "acodec": "opus"}]}), encoding="utf-8")
        (tmp_path / "v1.webm").write_bytes(b"a")
        assert transcribe._has_video_stream(str(tmp_path / "v1.webm"), "v1") is True

    def test_falls_back_to_extension_without_info_json(self, tmp_path):
        (tmp_path / "v1.mp4").write_bytes(b"a")
        assert transcribe._has_video_stream(str(tmp_path / "v1.mp4"), "v1") is True
        (tmp_path / "v2.m4a").write_bytes(b"a")
        assert transcribe._has_video_stream(str(tmp_path / "v2.m4a"), "v2") is False

    def test_suffix_lists_agree_with_package_delivery(self):
        # 片方だけ .webm を含むと、正常に取れた動画が納品から落ちる
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        import package_delivery
        assert set(transcribe._VIDEO_SUFFIXES) == set(package_delivery.VIDEO_SUFFIXES)


class TestInjectCoreSummary:
    def _md(self, tmp_path):
        p = tmp_path / "t.md"
        p.write_text("# タイトル\n\nチャンネル: CH\nURL: u\nモデル: m\n"
                     "処理日時: 2026-08-03 01:00:00\n\n---\n\n本文\n", encoding="utf-8")
        return p

    def test_backslashes_in_summary_do_not_crash(self, tmp_path):
        # LLM 出力を置換文字列に直接埋めると "\U" 等がエスケープ解釈されて re.error。
        # 失敗すると index 更新前に落ちるので、その動画は毎回再DL・再文字起こしされる
        p = self._md(tmp_path)
        summary = "## ポイント\n- 設定は C:\\Users\\name\\AppData に置く\n- \\1 も安全"
        with patch.object(transcribe, "_generate_core_summary", return_value=(summary, "ollama")):
            transcribe._inject_core_summary(p)
        text = p.read_text(encoding="utf-8")
        assert "C:\\Users\\name\\AppData" in text
        assert "\\1 も安全" in text

    def test_adds_heading_when_llm_omits_it(self, tmp_path):
        # 見出しが無いと再実行のたびに二重挿入され、かつ全成果物から抜け落ちる
        p = self._md(tmp_path)
        with patch.object(transcribe, "_generate_core_summary",
                          return_value=("- 見出しなしの箇条書き", "ollama")):
            transcribe._inject_core_summary(p)
        assert p.read_text(encoding="utf-8").count("## ポイント") == 1

    def test_is_idempotent(self, tmp_path):
        p = self._md(tmp_path)
        with patch.object(transcribe, "_generate_core_summary",
                          return_value=("- 箇条書き", "ollama")) as gen:
            transcribe._inject_core_summary(p)
            transcribe._inject_core_summary(p)
        assert gen.call_count == 1
        assert p.read_text(encoding="utf-8").count("## ポイント") == 1


class TestDrainQueueFailureIsolation:
    def test_failed_file_is_quarantined(self, tmp_path, monkeypatch):
        # 失敗ファイルを退けないと同じファイルが毎周回選ばれ、キューが永久に詰まる
        monkeypatch.setattr(transcribe, "QUEUE_DIR", tmp_path / "queue")
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        q = tmp_path / "queue" / "CH"
        q.mkdir(parents=True)
        (q / "bad.m4a").write_bytes(b"x")
        (q / "bad.meta.json").write_text(json.dumps({
            "title": "壊れた", "url": "https://youtu.be/bad", "channel": "CH", "lang": "ja"}),
            encoding="utf-8")
        with patch.object(transcribe, "_transcribe", side_effect=RuntimeError("boom")) as tr:
            transcribe._drain_queue_all(idle_polls=1, idle_sleep=0)
        assert tr.call_count == 1  # 無限リトライしない
        assert (q / "bad.m4a.failed").exists()
        assert not (q / "bad.m4a").exists()

    def test_good_file_after_bad_one_still_processes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "QUEUE_DIR", tmp_path / "queue")
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        q = tmp_path / "queue" / "CH"
        q.mkdir(parents=True)
        for name in ("bad", "good"):
            (q / f"{name}.m4a").write_bytes(b"x")
            (q / f"{name}.meta.json").write_text(json.dumps({
                "title": name, "url": f"https://youtu.be/{name}",
                "channel": "CH", "lang": "ja"}), encoding="utf-8")
        os.utime(q / "bad.m4a", (1, 1))  # bad を最古にして先に選ばせる

        def _tr(path, *a, **kw):
            # tmp_path 自体にテスト名が入るのでベース名だけで判定する
            if Path(path).name.startswith("bad"):
                raise RuntimeError("boom")
            return "text"

        with patch.object(transcribe, "_transcribe", side_effect=_tr), \
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"):
            processed = transcribe._drain_queue_all(idle_polls=1, idle_sleep=0)
        assert processed == 1
        assert "good" in transcribe._load_index("CH")


class TestSaveDeliverVideo:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "DELIVER_DIR", tmp_path / "deliver")

    def test_saves_mp4(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        src = tmp_path / "a.mp4"
        src.write_bytes(b"video")
        assert transcribe._save_deliver_video("CH", "vid1", str(src)) is True
        assert (tmp_path / "deliver" / "CH" / "videos" / "vid1.mp4").read_bytes() == b"video"

    @pytest.mark.parametrize("ext", [".m4a", ".opus"])
    def test_rejects_audio_only_results(self, tmp_path, monkeypatch, ext):
        # 音声フォールバックが効いた動画を「動画」フォルダに混ぜないこと
        self._setup(tmp_path, monkeypatch)
        src = tmp_path / f"a{ext}"
        src.write_bytes(b"audio")
        assert transcribe._save_deliver_video("CH", "vid1", str(src)) is False
        assert not (tmp_path / "deliver").exists()

    def test_rejects_audio_only_webm_using_info_json(self, tmp_path, monkeypatch):
        # .webm は combined でも音声のみでもありうる。拡張子では決められないので
        # info.json の vcodec を見る（実運用では writeinfojson で必ず並んでいる）
        self._setup(tmp_path, monkeypatch)
        (tmp_path / "vid1.webm").write_bytes(b"audio")
        (tmp_path / "vid1.info.json").write_text(json.dumps(
            {"requested_downloads": [{"vcodec": "none"}]}), encoding="utf-8")
        assert transcribe._save_deliver_video("CH", "vid1", str(tmp_path / "vid1.webm")) is False
        assert not (tmp_path / "deliver").exists()

    def test_transcription_still_proceeds_when_video_unavailable(self, tmp_path, monkeypatch):
        # 動画が取れなくてもテキストの納品物は揃うこと
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "DELIVER_DIR", tmp_path / "deliver")
        audio = tmp_path / "only.m4a"
        audio.write_bytes(b"audio")
        with patch.object(transcribe, "_download_audio", return_value=str(audio)), \
             patch.object(transcribe, "_transcribe", return_value="文字起こし"), \
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"), \
             patch("tempfile.mkdtemp", return_value=str(tmp_path / "tmp")), \
             patch("shutil.rmtree"):
            ok = transcribe._process_url("https://youtu.be/newvid", "CH", title="動画",
                                         video_quality="360p")
        assert ok is True
        assert (tmp_path / "transcripts" / "CH" / "動画.md").exists()
        assert not (tmp_path / "deliver").exists()

    def test_audio_only_format_used_when_quality_is_none(self, tmp_path, monkeypatch):
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, urls):
                (tmp_path / "abc.m4a").write_bytes(b"x")

        fake_mod = MagicMock()
        fake_mod.YoutubeDL = FakeYDL
        monkeypatch.setitem(sys.modules, "yt_dlp", fake_mod)
        out = transcribe._download_audio("https://youtu.be/abc", str(tmp_path))
        assert captured["format"].startswith("bestaudio")
        assert out.endswith(".m4a")
        # info.json は納品ファイル名の日付・連番順・動画判定(vcodec)の一次根拠。
        # 落とすと全部が無音で劣化するので契約として固定しておく
        assert captured["writeinfojson"] is True

    def test_video_quality_switches_format_and_prefers_video_container(self, tmp_path, monkeypatch):
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, urls):
                # 動画と音声の両方が残っていても動画コンテナを返すこと
                (tmp_path / "abc.m4a").write_bytes(b"x")
                (tmp_path / "abc.mp4").write_bytes(b"x")

        fake_mod = MagicMock()
        fake_mod.YoutubeDL = FakeYDL
        monkeypatch.setitem(sys.modules, "yt_dlp", fake_mod)
        out = transcribe._download_audio("https://youtu.be/abc", str(tmp_path), video_quality="360p")
        assert captured["format"] == transcribe._VIDEO_FORMATS["360p"]
        assert captured["writeinfojson"] is True
        assert captured["outtmpl"].endswith("%(id)s.%(ext)s")  # ID一致で拾える前提
        assert out.endswith(".mp4")


class TestProcessUrlKeepsVideo:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "DELIVER_DIR", tmp_path / "deliver")

    def test_video_is_saved_before_tmpdir_is_cleaned(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        media = tmp_path / "src.mp4"
        media.write_bytes(b"video-bytes")
        with patch.object(transcribe, "_download_audio", return_value=str(media)), \
             patch.object(transcribe, "_transcribe", return_value="text"), \
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"), \
             patch("tempfile.mkdtemp", return_value=str(tmp_path / "tmp")), \
             patch("shutil.rmtree"):
            ok = transcribe._process_url("https://youtu.be/newvid", "CH", title="動画",
                                         video_quality="360p")
        assert ok is True
        saved = tmp_path / "deliver" / "CH" / "videos" / "newvid.mp4"
        assert saved.exists()
        assert saved.read_bytes() == b"video-bytes"

    def test_no_video_saved_when_quality_not_requested(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_download_audio", return_value=str(tmp_path / "a.m4a")), \
             patch.object(transcribe, "_transcribe", return_value="text"), \
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"), \
             patch("tempfile.mkdtemp", return_value=str(tmp_path / "tmp")), \
             patch("shutil.rmtree"):
            transcribe._process_url("https://youtu.be/newvid", "CH", title="動画")
        assert not (tmp_path / "deliver").exists()


class TestReadInfoJson:
    def test_parses_upload_date_duration_views(self, tmp_path):
        (tmp_path / "abc.info.json").write_text(json.dumps(
            {"upload_date": "20240720", "duration": 700.4, "view_count": 978051}),
            encoding="utf-8")
        assert transcribe._read_info_json(str(tmp_path), "abc") == {
            "upload_date": "2024-07-20", "duration": 700, "view_count": 978051}

    def test_missing_file_returns_empty(self, tmp_path):
        assert transcribe._read_info_json(str(tmp_path), "abc") == {}

    def test_broken_json_returns_empty(self, tmp_path):
        # 壊れた info.json で処理全体を止めない
        (tmp_path / "abc.info.json").write_text("{not json", encoding="utf-8")
        assert transcribe._read_info_json(str(tmp_path), "abc") == {}

    def test_malformed_upload_date_is_dropped(self, tmp_path):
        (tmp_path / "abc.info.json").write_text(json.dumps({"upload_date": "2024"}),
                                                encoding="utf-8")
        assert "upload_date" not in transcribe._read_info_json(str(tmp_path), "abc")


class TestDownloadChannelToQueue:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe, "QUEUE_DIR", tmp_path / "queue")
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        monkeypatch.setattr(transcribe, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(transcribe, "DELIVER_DIR", tmp_path / "deliver")

    def _fake_download(self, tmp_path):
        """queue に音声（と info.json）を置く _download_audio の代役"""
        def _dl(url, out_dir, video_quality=None):
            vid = transcribe._extract_video_id(url)
            ext = ".mp4" if video_quality else ".m4a"
            p = Path(out_dir) / f"{vid}{ext}"
            p.write_bytes(b"media")
            (Path(out_dir) / f"{vid}.info.json").write_text(
                json.dumps({"upload_date": "20250101", "duration": 60}), encoding="utf-8")
            return str(p)
        return _dl

    def test_range_filter_is_applied(self, tmp_path, monkeypatch):
        # --since-video を渡したのに全件落としにいく、という事故を防ぐ
        self._setup(tmp_path, monkeypatch)
        vids = _videos(10)
        with patch.object(transcribe, "_get_channel_videos", return_value=vids), \
             patch.object(transcribe, "_download_audio", side_effect=self._fake_download(tmp_path)):
            added, limited = transcribe._download_channel_to_queue(
                "CH", "https://youtube.com/@x", sort="date", since_video="vid002")
        assert added == 3
        assert limited is False
        queued = sorted(p.stem for p in (tmp_path / "queue" / "CH").glob("*.m4a"))
        assert queued == ["vid000", "vid001", "vid002"]

    def test_info_json_is_folded_into_meta_and_removed(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(1)), \
             patch.object(transcribe, "_download_audio", side_effect=self._fake_download(tmp_path)):
            transcribe._download_channel_to_queue("CH", "https://youtube.com/@x", sort="date")
        q = tmp_path / "queue" / "CH"
        assert not (q / "vid000.info.json").exists()  # queue に残さない
        meta = json.loads((q / "vid000.meta.json").read_text(encoding="utf-8"))
        assert meta["upload_date"] == "2025-01-01"
        assert meta["duration"] == 60

    def test_video_is_copied_out_before_queue_consumes_it(self, tmp_path, monkeypatch):
        # drain-queue は文字起こし後に queue のファイルを消す。360p は音声と動画が
        # 同一ファイルなので、退避していないと納品物ごと消える
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(1)), \
             patch.object(transcribe, "_download_audio", side_effect=self._fake_download(tmp_path)):
            transcribe._download_channel_to_queue("CH", "https://youtube.com/@x",
                                                  sort="date", video_quality="360p")
        assert (tmp_path / "deliver" / "CH" / "videos" / "vid000.mp4").exists()

    def test_no_video_copied_when_quality_not_requested(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(1)), \
             patch.object(transcribe, "_download_audio", side_effect=self._fake_download(tmp_path)):
            transcribe._download_channel_to_queue("CH", "https://youtube.com/@x", sort="date")
        assert not (tmp_path / "deliver").exists()

    def test_empty_range_downloads_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        with patch.object(transcribe, "_get_channel_videos", return_value=_videos(10)), \
             patch.object(transcribe, "_download_audio") as dl:
            added, limited = transcribe._download_channel_to_queue(
                "CH", "https://youtube.com/@x", sort="date",
                since_video="vid002", until_video="vid007")
        assert (added, limited) == (0, False)
        assert dl.call_count == 0


class TestDrainQueuePropagatesMetadata:
    def test_upload_date_reaches_the_index(self, tmp_path, monkeypatch):
        # 納品時の並び順と日付つきファイル名は _index.json の upload_date が正
        monkeypatch.setattr(transcribe, "QUEUE_DIR", tmp_path / "queue")
        monkeypatch.setattr(transcribe, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
        q = tmp_path / "queue" / "CH"
        q.mkdir(parents=True)
        (q / "abc123.m4a").write_bytes(b"audio")
        (q / "abc123.meta.json").write_text(json.dumps({
            "title": "動画", "url": "https://youtu.be/abc123", "channel": "CH",
            "lang": "ja", "upload_date": "2025-03-04", "duration": 120,
            "view_count": 999}), encoding="utf-8")
        with patch.object(transcribe, "_transcribe", return_value="text"), \
             patch.object(transcribe, "_inject_core_summary"), \
             patch.object(transcribe, "_copy_file_to_drive"):
            transcribe._drain_queue_all(idle_polls=1, idle_sleep=0)
        entry = transcribe._load_index("CH")["abc123"]
        assert entry["upload_date"] == "2025-03-04"
        assert entry["duration"] == 120
        assert entry["view_count"] == 999
