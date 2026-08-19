import os
import shutil
import sqlite3
import tempfile
import urllib.error
from pathlib import Path

import pytest

from src.collectors.youtube_analytics_import import (
    _fetch_videos_with_latest_stats,
    _row_to_content,
    download_db,
    import_from_youtube_analytics,
)
from src.db.models import get_connection, init_db


def _build_source_fixture_db(path):
    """youtube-analytics リポジトリのDBスキーマを模したfixtureを作成する。"""

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            published_at DATETIME NOT NULL,
            video_url TEXT NOT NULL DEFAULT '',
            thumbnail_url TEXT NOT NULL DEFAULT '',
            video_type TEXT NOT NULL DEFAULT 'regular',
            content_type TEXT NOT NULL DEFAULT 'other'
        );
        CREATE TABLE video_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            recorded_at DATETIME NOT NULL,
            view_count INTEGER NOT NULL DEFAULT 0,
            like_count INTEGER NOT NULL DEFAULT 0,
            comment_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, description, published_at, thumbnail_url)
        VALUES ('v1', 'UCtest', 'テスト動画', '説明文', '2026-08-01T10:00:00', 'https://example.com/thumb.jpg')
        """
    )
    # 古いスナップショットと最新スナップショットを両方入れ、最新側が採用されることを検証する
    conn.execute(
        "INSERT INTO video_snapshots (video_id, recorded_at, view_count, like_count, comment_count) "
        "VALUES ('v1', '2026-08-01T11:00:00', 100, 10, 1)"
    )
    conn.execute(
        "INSERT INTO video_snapshots (video_id, recorded_at, view_count, like_count, comment_count) "
        "VALUES ('v1', '2026-08-02T11:00:00', 500, 50, 5)"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def source_db_path(tmp_path):
    path = tmp_path / "source.sqlite"
    _build_source_fixture_db(path)
    return path


@pytest.fixture()
def dest_db_path(tmp_path):
    path = tmp_path / "contents.sqlite"
    init_db(path)
    return path


def test_fetch_videos_with_latest_stats_picks_latest_snapshot(source_db_path):
    rows = _fetch_videos_with_latest_stats(source_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["video_id"] == "v1"
    assert row["view_count"] == 500
    assert row["like_count"] == 50
    assert row["comment_count"] == 5


def test_row_to_content_maps_fields_and_nulls_duration_tags(source_db_path):
    row = _fetch_videos_with_latest_stats(source_db_path)[0]
    content = _row_to_content(row)
    assert content is not None
    assert content.id == "v1"
    assert content.title == "テスト動画"
    assert content.published_at == "2026-08-01T10:00:00"
    assert content.channel_id == "UCtest"
    assert content.duration_seconds is None
    assert content.tags is None
    assert content.view_count == 500


def test_download_db_success(monkeypatch, source_db_path, tmp_path):
    dest = tmp_path / "downloaded.sqlite"

    def fake_urlretrieve(url, filename):
        shutil.copy(source_db_path, filename)

    monkeypatch.setattr(
        "src.collectors.youtube_analytics_import.urllib.request.urlretrieve",
        fake_urlretrieve,
    )

    result = download_db(url="https://example.com/dummy.sqlite", dest_path=dest)
    assert result == dest
    assert dest.exists()


def test_download_db_failure_returns_none(monkeypatch, tmp_path):
    dest = tmp_path / "downloaded.sqlite"

    def fake_urlretrieve(url, filename):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(
        "src.collectors.youtube_analytics_import.urllib.request.urlretrieve",
        fake_urlretrieve,
    )

    result = download_db(url="https://example.com/dummy.sqlite", dest_path=dest)
    assert result is None
    assert not dest.exists()


def test_import_from_youtube_analytics_end_to_end(monkeypatch, source_db_path, dest_db_path):
    monkeypatch.setattr(
        "src.collectors.youtube_analytics_import.download_db",
        lambda url=None: _copy_to_temp(source_db_path),
    )

    result = import_from_youtube_analytics(db_path=dest_db_path)

    assert result == {"ok": True, "fetched": 1, "upserted": 1, "video_ids": ["v1"]}

    conn = get_connection(dest_db_path)
    row = conn.execute("SELECT * FROM contents WHERE id = 'v1'").fetchone()
    assert row is not None
    assert row["view_count"] == 500
    assert row["duration_seconds"] is None
    conn.close()


def test_import_from_youtube_analytics_download_failure_returns_ok_false(monkeypatch, dest_db_path):
    monkeypatch.setattr(
        "src.collectors.youtube_analytics_import.download_db", lambda url=None: None
    )

    result = import_from_youtube_analytics(db_path=dest_db_path)
    assert result == {"ok": False, "fetched": 0, "upserted": 0, "video_ids": []}


def _copy_to_temp(source_path: Path) -> Path:
    """import_from_youtube_analytics は戻り値のファイルを最後に削除するため、
    テストのfixtureファイル自体を渡さず、使い捨てのコピーを渡す。"""
    fd, tmp_name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    shutil.copy(source_path, tmp_name)
    return Path(tmp_name)
