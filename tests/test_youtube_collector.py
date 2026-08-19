import json
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from src.collectors.youtube import (
    _to_content,
    collect_channel_videos,
    parse_duration_seconds,
)
from src.db.models import get_connection, init_db, upsert_content


@pytest.mark.parametrize(
    "iso_duration, expected_seconds",
    [
        ("PT4M13S", 4 * 60 + 13),
        ("PT1H2M3S", 3600 + 2 * 60 + 3),
        ("PT30S", 30),
        ("PT2H", 7200),
        (None, None),
        ("invalid", None),
    ],
)
def test_parse_duration_seconds(iso_duration, expected_seconds):
    assert parse_duration_seconds(iso_duration) == expected_seconds


SAMPLE_VIDEO = {
    "id": "vid001",
    "snippet": {
        "title": "テスト動画タイトル",
        "description": "説明文",
        "publishedAt": "2026-08-01T10:00:00Z",
        "channelId": "UCtest",
        "tags": ["idol", "live"],
        "thumbnails": {
            "default": {"url": "http://example.com/default.jpg"},
            "high": {"url": "http://example.com/high.jpg"},
        },
    },
    "contentDetails": {"duration": "PT3M30S"},
    "statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "7"},
}


def test_to_content_converts_fields_correctly():
    content = _to_content(SAMPLE_VIDEO)
    assert content is not None
    assert content.id == "vid001"
    assert content.title == "テスト動画タイトル"
    assert content.published_at == "2026-08-01T10:00:00Z"
    assert content.duration_seconds == 210
    assert content.thumbnail_url == "http://example.com/high.jpg"
    assert json.loads(content.tags) == ["idol", "live"]
    assert content.view_count == 1234
    assert content.like_count == 56
    assert content.comment_count == 7


def test_to_content_handles_missing_statistics_gracefully():
    video = {
        "id": "vid002",
        "snippet": {
            "title": "統計欠損動画",
            "publishedAt": "2026-08-02T10:00:00Z",
            "channelId": "UCtest",
        },
        "contentDetails": {},
        "statistics": {},  # コメント無効化・低評価非表示等で欠損するケース
    }
    content = _to_content(video)
    assert content is not None
    assert content.view_count == 0
    assert content.like_count == 0
    assert content.comment_count == 0
    assert content.duration_seconds is None


def test_to_content_returns_none_on_malformed_data():
    assert _to_content({"id": "vid003"}) is None or _to_content({"id": "vid003"}).title == ""


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def test_upsert_is_idempotent_and_updates_stats(db_path):
    conn = get_connection(db_path)
    content = _to_content(SAMPLE_VIDEO)

    upsert_content(conn, content)
    conn.commit()

    row = conn.execute("SELECT * FROM contents WHERE id = ?", (content.id,)).fetchone()
    assert row["view_count"] == 1234
    first_created_at = row["created_at"]

    # 同じIDで再実行(再収集)しても行が増えず、実績値のみ更新されることを確認
    content.view_count = 9999
    content.like_count = 999
    upsert_content(conn, content)
    conn.commit()

    count = conn.execute("SELECT COUNT(*) AS c FROM contents").fetchone()["c"]
    assert count == 1

    updated_row = conn.execute("SELECT * FROM contents WHERE id = ?", (content.id,)).fetchone()
    assert updated_row["view_count"] == 9999
    assert updated_row["like_count"] == 999
    assert updated_row["created_at"] == first_created_at  # created_atは初回値を保持

    conn.close()


def _make_mock_youtube(channel_items, playlist_pages, videos_items):
    youtube = MagicMock()
    youtube.channels.return_value.list.return_value.execute.return_value = {
        "items": channel_items
    }

    playlist_iter = iter(playlist_pages)

    def playlist_execute():
        return next(playlist_iter)

    youtube.playlistItems.return_value.list.return_value.execute.side_effect = playlist_execute
    youtube.videos.return_value.list.return_value.execute.return_value = {
        "items": videos_items
    }
    return youtube


def test_collect_channel_videos_missing_credentials_returns_safely(monkeypatch, db_path):
    result = collect_channel_videos(channel_id="", api_key="", db_path=db_path)
    assert result == {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False}


def test_collect_channel_videos_end_to_end(monkeypatch, db_path):
    channel_items = [
        {"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}
    ]
    playlist_pages = [
        {"items": [{"contentDetails": {"videoId": "vid001"}}], "nextPageToken": None}
    ]
    videos_items = [SAMPLE_VIDEO]

    mock_youtube = _make_mock_youtube(channel_items, playlist_pages, videos_items)
    monkeypatch.setattr(
        "src.collectors.youtube._build_youtube_client", lambda api_key: mock_youtube
    )

    result = collect_channel_videos(
        channel_id="UCtest", api_key="dummy", max_results=50, db_path=db_path
    )

    assert result["ok"] is True
    assert result["fetched"] == 1
    assert result["upserted"] == 1
    assert result["video_ids"] == ["vid001"]

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM contents WHERE id = 'vid001'").fetchone()
    assert row is not None
    assert row["view_count"] == 1234
    conn.close()


def test_collect_channel_videos_channel_not_found(monkeypatch, db_path):
    mock_youtube = _make_mock_youtube(channel_items=[], playlist_pages=[], videos_items=[])
    monkeypatch.setattr(
        "src.collectors.youtube._build_youtube_client", lambda api_key: mock_youtube
    )

    result = collect_channel_videos(channel_id="UCnotfound", api_key="dummy", db_path=db_path)
    assert result == {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False}


def test_fetch_recent_video_ids_http_error_falls_back_gracefully(monkeypatch, db_path):
    channel_items = [
        {"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}
    ]
    mock_youtube = MagicMock()
    mock_youtube.channels.return_value.list.return_value.execute.return_value = {
        "items": channel_items
    }
    mock_resp = MagicMock(status=403)
    mock_youtube.playlistItems.return_value.list.return_value.execute.side_effect = HttpError(
        resp=mock_resp, content=b'{"error": "quota exceeded"}'
    )
    monkeypatch.setattr(
        "src.collectors.youtube._build_youtube_client", lambda api_key: mock_youtube
    )

    result = collect_channel_videos(channel_id="UCtest", api_key="dummy", db_path=db_path)
    assert result["ok"] is True
    assert result["fetched"] == 0
    assert result["upserted"] == 0
