import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.app.dashboard import (
    build_summary_dataframe,
    fetch_available_dates,
    fetch_contents,
    list_video_files,
    parse_video_content_id,
    update_status,
)
from src.db.models import ContentStatus, get_connection, init_db

DASHBOARD_PATH = str(Path(__file__).resolve().parent.parent / "src" / "app" / "dashboard.py")


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_content(
    conn,
    platform="X",
    title="タイトル",
    body="本文",
    status="pending",
    evaluation_score=None,
    narration_text=None,
    search_keywords=None,
):
    cursor = conn.execute(
        """
        INSERT INTO generated_contents
            (platform, content_type, title, body, status, evaluation_score,
             narration_text, search_keywords)
        VALUES (?, 'post_text', ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            title,
            body,
            status,
            evaluation_score,
            narration_text,
            json.dumps(search_keywords) if search_keywords else None,
        ),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# 純粋関数のユニットテスト
# ---------------------------------------------------------------------------


def test_fetch_available_dates_returns_distinct_dates_desc(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, title="今日1")
    _insert_content(conn, title="今日2")
    dates = fetch_available_dates(conn)
    conn.close()
    assert len(dates) == 1  # 同日なので1件に集約される


def test_fetch_contents_filters_by_platform(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, platform="X", title="X投稿")
    _insert_content(conn, platform="TikTok", title="TikTok投稿")
    rows = fetch_contents(conn, platform="TikTok")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["title"] == "TikTok投稿"


def test_fetch_contents_filters_by_status(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, status="pending", title="未承認")
    _insert_content(conn, status="approved", title="承認済み")
    rows = fetch_contents(conn, status="approved")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["title"] == "承認済み"


def test_fetch_contents_search_query_matches_title_and_body(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, title="桜のライブ", body="本文A")
    _insert_content(conn, title="タイトルB", body="桜が満開の本文")
    _insert_content(conn, title="関係ない", body="関係ない本文")
    rows = fetch_contents(conn, search_query="桜")
    conn.close()
    assert len(rows) == 2


def test_fetch_contents_respects_limit(db_path):
    conn = get_connection(db_path)
    for i in range(5):
        _insert_content(conn, title=f"投稿{i}")
    rows = fetch_contents(conn, limit=3)
    conn.close()
    assert len(rows) == 3


def test_update_status_changes_status_and_updated_at(db_path):
    conn = get_connection(db_path)
    content_id = _insert_content(conn, status="pending")
    update_status(conn, content_id, ContentStatus.APPROVED)
    row = conn.execute("SELECT status FROM generated_contents WHERE id = ?", (content_id,)).fetchone()
    conn.close()
    assert row["status"] == "approved"


def test_list_video_files_returns_empty_for_missing_dir(tmp_path):
    assert list_video_files(tmp_path / "does_not_exist") == []


def test_list_video_files_sorted_newest_first(tmp_path):
    import time

    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    old = videos_dir / "old.mp4"
    old.write_bytes(b"a")
    time.sleep(0.01)
    new = videos_dir / "new.mp4"
    new.write_bytes(b"b")

    result = list_video_files(videos_dir)
    assert result == [new, old]


@pytest.mark.parametrize(
    "filename, expected_id",
    [
        ("2026-08-19_tiktok_24.mp4", 24),
        ("2026-08-19_tiktok_1.mp4", 1),
        ("random_name.mp4", None),
        ("no_id_suffix.mp4", None),
    ],
)
def test_parse_video_content_id(tmp_path, filename, expected_id):
    assert parse_video_content_id(tmp_path / filename) == expected_id


def test_build_summary_dataframe_empty_rows_returns_empty_df():
    df = build_summary_dataframe([])
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    assert "platform" in df.columns


def test_build_summary_dataframe_maps_fields(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, platform="TikTok", title="動画企画", evaluation_score=85)
    rows = fetch_contents(conn)
    conn.close()

    df = build_summary_dataframe(rows)
    assert len(df) == 1
    assert df.iloc[0]["platform"] == "TikTok"
    assert df.iloc[0]["title"] == "動画企画"
    assert df.iloc[0]["evaluation_score"] == 85


# ---------------------------------------------------------------------------
# AppTestによるアプリ全体の統合テスト
# ---------------------------------------------------------------------------


def _run_app(db_path, videos_dir=None):
    at = AppTest.from_file(DASHBOARD_PATH)
    at.session_state["_dashboard_db_path"] = str(db_path)
    if videos_dir is not None:
        at.session_state["_dashboard_videos_dir"] = str(videos_dir)
    at.run(timeout=30)
    return at


def test_app_loads_without_exception_on_empty_db(db_path, tmp_path):
    at = _run_app(db_path, tmp_path / "videos")
    assert not at.exception
    assert len(at.tabs) == 4


def test_app_shows_no_data_message_when_db_empty(db_path, tmp_path):
    at = _run_app(db_path, tmp_path / "videos")
    assert not at.exception
    assert any("まだ生成されたコンテンツがありません" in i.value for i in at.tabs[0].info)


def test_app_sidebar_filters_have_expected_options(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, platform="TikTok")
    conn.close()

    at = _run_app(db_path)
    assert not at.exception

    platform_select = next(sb for sb in at.sidebar.selectbox if sb.label == "📱 メディア")
    assert platform_select.options == ["すべて", "X", "Instagram", "TikTok", "YouTube", "note"]

    status_select = next(sb for sb in at.sidebar.selectbox if sb.label == "🏷️ ステータス")
    assert status_select.options == [
        "すべて", "DRAFT (未承認)", "APPROVED (承認済み)", "PUBLISHED (投稿済み)",
    ]


def test_app_renders_content_card_with_title_and_body(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, platform="TikTok", title="カードタイトル", body="カード本文です")
    conn.close()

    at = _run_app(db_path)
    assert not at.exception

    markdown_values = [m.value for m in at.tabs[0].markdown]
    assert any("カードタイトル" in v for v in markdown_values)
    code_values = [c.value for c in at.tabs[0].code]
    assert any("カード本文です" in v for v in code_values)

    caption_values = [c.value for c in at.tabs[0].caption]
    assert any("クラウド版でのご利用の場合" in v for v in caption_values)


def test_app_feedback_tab_shows_cloud_write_warning(db_path):
    at = _run_app(db_path)
    caption_values = [c.value for c in at.tabs[3].caption]
    assert any("クラウド版でのご利用の場合" in v for v in caption_values)


def test_app_approve_button_updates_status(db_path):
    conn = get_connection(db_path)
    content_id = _insert_content(conn, status="pending")
    conn.close()

    at = _run_app(db_path)
    at.tabs[0].button(key=f"today_approve_{content_id}").click()
    at.run(timeout=30)
    assert not at.exception

    conn = get_connection(db_path)
    row = conn.execute("SELECT status FROM generated_contents WHERE id = ?", (content_id,)).fetchone()
    conn.close()
    assert row["status"] == "approved"


def test_app_reject_button_updates_status(db_path):
    conn = get_connection(db_path)
    content_id = _insert_content(conn, status="pending")
    conn.close()

    at = _run_app(db_path)
    at.tabs[0].button(key=f"today_reject_{content_id}").click()
    at.run(timeout=30)
    assert not at.exception

    conn = get_connection(db_path)
    row = conn.execute("SELECT status FROM generated_contents WHERE id = ?", (content_id,)).fetchone()
    conn.close()
    assert row["status"] == "rejected"


def test_app_archive_tab_search_filters_results(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, title="桜ライブ配信")
    _insert_content(conn, title="無関係な投稿")
    conn.close()

    at = _run_app(db_path)
    archive_search = next(t for t in at.tabs[1].text_input if t.label == "🔍 キーワード検索（タイトル・本文）")
    archive_search.set_value("桜")
    at.run(timeout=30)
    assert not at.exception

    markdown_values = [m.value for m in at.tabs[1].markdown]
    assert any("桜ライブ配信" in v for v in markdown_values)
    assert not any("無関係な投稿" in v for v in markdown_values)


def test_app_video_tab_shows_empty_state_message(db_path, tmp_path):
    at = _run_app(db_path, tmp_path / "videos")
    assert not at.exception
    assert any("まだ生成された動画がありません" in i.value for i in at.tabs[2].info)


def test_app_video_tab_plays_video_with_matched_title(db_path, tmp_path):
    conn = get_connection(db_path)
    content_id = _insert_content(conn, title="動画のタイトル")
    conn.close()

    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    (videos_dir / f"2026-08-19_tiktok_{content_id}.mp4").write_bytes(b"fake video content")

    at = _run_app(db_path, videos_dir)
    assert not at.exception

    markdown_values = [m.value for m in at.tabs[2].markdown]
    assert any("動画のタイトル" in v for v in markdown_values)
    assert len(at.tabs[2].get("video")) == 1


def test_app_feedback_tab_records_result(db_path):
    conn = get_connection(db_path)
    content_id = _insert_content(conn)
    conn.close()

    at = _run_app(db_path)
    tab4 = at.tabs[3]

    def find_ni(label):
        return next(ni for ni in tab4.number_input if ni.label == label)

    find_ni("コンテンツID (generated_contents.id)").set_value(content_id)
    find_ni("再生数").set_value(9999)

    tab4.button[0].click()
    at.run(timeout=30)
    assert not at.exception
    assert len(at.tabs[3].success) == 1

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT actual_view_count FROM generated_contents WHERE id = ?", (content_id,)
    ).fetchone()
    conn.close()
    assert row["actual_view_count"] == 9999


def test_app_feedback_tab_missing_id_shows_error(db_path):
    at = _run_app(db_path)
    tab4 = at.tabs[3]

    def find_ni(label):
        return next(ni for ni in tab4.number_input if ni.label == label)

    find_ni("コンテンツID (generated_contents.id)").set_value(9999)
    find_ni("再生数").set_value(100)

    tab4.button[0].click()
    at.run(timeout=30)
    assert not at.exception
    assert len(at.tabs[3].error) == 1
