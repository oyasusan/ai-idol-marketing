import sqlite3

import pytest

from src.db.models import (
    ContentStatus,
    GeneratedContent,
    Platform,
    get_connection,
    init_db,
)


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.sqlite"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def test_init_db_creates_expected_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    ).fetchall()
    tables = {row["name"] for row in rows}
    assert {"contents", "ai_analyses", "generated_contents"} <= tables


def test_generated_contents_rejects_invalid_platform(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO generated_contents (platform, content_type, body) "
            "VALUES ('Facebook', 'post_text', 'test')"
        )


def test_generated_contents_rejects_invalid_status(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO generated_contents (platform, content_type, body, status) "
            "VALUES ('X', 'post_text', 'test', 'posted')"
        )


def test_generated_contents_default_status_is_pending(conn):
    conn.execute(
        "INSERT INTO generated_contents (platform, content_type, body) "
        "VALUES ('X', 'post_text', 'test')"
    )
    conn.commit()
    row = conn.execute("SELECT status FROM generated_contents").fetchone()
    assert row["status"] == ContentStatus.PENDING.value


def test_generated_content_pydantic_model_defaults():
    model = GeneratedContent(platform=Platform.YOUTUBE, content_type="video_script", body="test")
    assert model.status == ContentStatus.PENDING
    assert model.evaluation_score is None


def test_generated_content_evaluation_score_out_of_range_rejected():
    with pytest.raises(ValueError):
        GeneratedContent(platform=Platform.X, content_type="post_text", body="test", evaluation_score=150)
