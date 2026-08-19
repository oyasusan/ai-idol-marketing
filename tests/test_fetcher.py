from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.db.models import get_connection, init_db
from src.video.fetcher import (
    OFFICIAL_CHANNEL_ID,
    _passes_constraints,
    download_video,
    fetch_video_assets,
    probe_video_metadata,
    select_candidate_video_ids,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_content(conn, video_id, title, channel_id=OFFICIAL_CHANNEL_ID, view_count=1000):
    conn.execute(
        """
        INSERT INTO contents (id, title, published_at, channel_id, view_count, like_count, comment_count)
        VALUES (?, ?, '2026-01-01T00:00:00Z', ?, ?, 0, 0)
        """,
        (video_id, title, channel_id, view_count),
    )
    conn.commit()


# ---- select_candidate_video_ids: ローカルDBスコープの検証（第三者チャンネル混入防止） ----


def test_select_candidate_video_ids_matches_keyword_and_official_channel_only(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "空想ロマンス ライブパフォーマンス2026", view_count=5000)
    _insert_content(conn, "v2", "空想ロマンス M/V公開", view_count=3000)
    _insert_content(conn, "v3", "全く関係ないタイトル", view_count=9999)
    # 公式チャンネル以外のデータが万一混入していても対象外になること
    _insert_content(conn, "v4", "ライブパフォーマンス（他チャンネル）", channel_id="UCother", view_count=8000)

    result = select_candidate_video_ids(conn, ["ライブパフォーマンス", "M/V"])
    conn.close()

    assert result == ["v1", "v2"]  # view_count降順、v3/v4は対象外


def test_select_candidate_video_ids_empty_keywords_returns_empty(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "テスト動画")
    result = select_candidate_video_ids(conn, [])
    conn.close()
    assert result == []


def test_select_candidate_video_ids_no_match_returns_empty(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "テスト動画")
    result = select_candidate_video_ids(conn, ["存在しないキーワード"])
    conn.close()
    assert result == []


# ---- _passes_constraints ----


def test_passes_constraints_accepts_valid_video():
    info = {"id": "v1", "channel_id": OFFICIAL_CHANNEL_ID, "duration": 60}
    assert _passes_constraints(info, OFFICIAL_CHANNEL_ID, 15, 180) is True


def test_passes_constraints_rejects_wrong_channel():
    info = {"id": "v1", "channel_id": "UCother", "duration": 60}
    assert _passes_constraints(info, OFFICIAL_CHANNEL_ID, 15, 180) is False


def test_passes_constraints_rejects_too_short():
    info = {"id": "v1", "channel_id": OFFICIAL_CHANNEL_ID, "duration": 8}
    assert _passes_constraints(info, OFFICIAL_CHANNEL_ID, 15, 180) is False


def test_passes_constraints_rejects_too_long():
    info = {"id": "v1", "channel_id": OFFICIAL_CHANNEL_ID, "duration": 300}
    assert _passes_constraints(info, OFFICIAL_CHANNEL_ID, 15, 180) is False


def test_passes_constraints_rejects_missing_duration():
    info = {"id": "v1", "channel_id": OFFICIAL_CHANNEL_ID}
    assert _passes_constraints(info, OFFICIAL_CHANNEL_ID, 15, 180) is False


# ---- probe_video_metadata / download_video (yt_dlp.YoutubeDLをモック) ----


def _mock_youtubedl(monkeypatch, extract_info_result=None, extract_info_side_effect=None):
    mock_instance = MagicMock()
    if extract_info_side_effect is not None:
        mock_instance.extract_info.side_effect = extract_info_side_effect
    else:
        mock_instance.extract_info.return_value = extract_info_result

    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value = mock_instance
    monkeypatch.setattr("src.video.fetcher.yt_dlp.YoutubeDL", mock_cls)
    return mock_instance


def test_probe_video_metadata_returns_info_on_success(monkeypatch):
    _mock_youtubedl(monkeypatch, extract_info_result={"id": "v1", "duration": 60})
    result = probe_video_metadata("v1")
    assert result == {"id": "v1", "duration": 60}


def test_probe_video_metadata_returns_none_on_exception(monkeypatch):
    _mock_youtubedl(monkeypatch, extract_info_side_effect=Exception("network error"))
    result = probe_video_metadata("v1")
    assert result is None


def test_download_video_returns_none_on_exception(monkeypatch, tmp_path):
    _mock_youtubedl(monkeypatch, extract_info_side_effect=Exception("download failed"))
    result = download_video("v1", tmp_path)
    assert result is None


def test_download_video_returns_path_when_file_exists(monkeypatch, tmp_path):
    def fake_extract_info(url, download=True):
        (tmp_path / "v1.mp4").write_bytes(b"fake video data")
        return {"id": "v1"}

    _mock_youtubedl(monkeypatch, extract_info_side_effect=fake_extract_info)
    result = download_video("v1", tmp_path)
    assert result == tmp_path / "v1.mp4"
    assert result.exists()


def test_download_video_returns_none_when_file_missing_after_download(monkeypatch, tmp_path):
    # extract_infoは成功したがファイルが生成されなかった異常系
    _mock_youtubedl(monkeypatch, extract_info_result={"id": "v1"})
    result = download_video("v1", tmp_path)
    assert result is None


# ---- fetch_video_assets (統合的なオーケストレーション) ----


def test_fetch_video_assets_no_candidates_returns_empty(monkeypatch, db_path, tmp_path):
    result = fetch_video_assets(["存在しないキーワード"], dest_dir=tmp_path, db_path=db_path)
    assert result == []


def test_fetch_video_assets_downloads_up_to_max_clips(monkeypatch, db_path, tmp_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "空想ロマンス ライブ 1", view_count=5000)
    _insert_content(conn, "v2", "空想ロマンス ライブ 2", view_count=4000)
    _insert_content(conn, "v3", "空想ロマンス ライブ 3", view_count=3000)
    conn.close()

    def fake_probe(video_id, ffmpeg_location=None):
        return {"id": video_id, "channel_id": OFFICIAL_CHANNEL_ID, "duration": 60}

    def fake_download(video_id, dest_dir, min_height=1080, ffmpeg_location=None):
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"fake")
        return path

    monkeypatch.setattr("src.video.fetcher.probe_video_metadata", fake_probe)
    monkeypatch.setattr("src.video.fetcher.download_video", fake_download)

    result = fetch_video_assets(
        ["ライブ"], max_clips=2, dest_dir=tmp_path, db_path=db_path
    )

    assert len(result) == 2  # max_clips=2 で v3 までは試行されない
    assert result[0].name == "v1.mp4"
    assert result[1].name == "v2.mp4"


def test_fetch_video_assets_skips_candidates_failing_constraints(monkeypatch, db_path, tmp_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "空想ロマンス ライブ 短すぎ", view_count=5000)
    _insert_content(conn, "v2", "空想ロマンス ライブ 適切", view_count=4000)
    conn.close()

    def fake_probe(video_id, ffmpeg_location=None):
        duration = 5 if video_id == "v1" else 60  # v1は短すぎて弾かれるはず
        return {"id": video_id, "channel_id": OFFICIAL_CHANNEL_ID, "duration": duration}

    def fake_download(video_id, dest_dir, min_height=1080, ffmpeg_location=None):
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"fake")
        return path

    monkeypatch.setattr("src.video.fetcher.probe_video_metadata", fake_probe)
    monkeypatch.setattr("src.video.fetcher.download_video", fake_download)

    result = fetch_video_assets(["ライブ"], max_clips=2, dest_dir=tmp_path, db_path=db_path)

    assert len(result) == 1
    assert result[0].name == "v2.mp4"


def test_fetch_video_assets_metadata_failure_tries_next_candidate(monkeypatch, db_path, tmp_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "空想ロマンス ライブ A", view_count=5000)
    _insert_content(conn, "v2", "空想ロマンス ライブ B", view_count=4000)
    conn.close()

    def fake_probe(video_id, ffmpeg_location=None):
        if video_id == "v1":
            return None  # メタデータ取得失敗(非公開・年齢制限等)をシミュレート
        return {"id": video_id, "channel_id": OFFICIAL_CHANNEL_ID, "duration": 60}

    def fake_download(video_id, dest_dir, min_height=1080, ffmpeg_location=None):
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"fake")
        return path

    monkeypatch.setattr("src.video.fetcher.probe_video_metadata", fake_probe)
    monkeypatch.setattr("src.video.fetcher.download_video", fake_download)

    result = fetch_video_assets(["ライブ"], max_clips=2, dest_dir=tmp_path, db_path=db_path)

    assert len(result) == 1
    assert result[0].name == "v2.mp4"
