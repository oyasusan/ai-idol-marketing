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


def test_generated_contents_accepts_published_status(conn):
    conn.execute(
        "INSERT INTO generated_contents (platform, content_type, body, status) "
        "VALUES ('X', 'post_text', 'test', 'published')"
    )
    conn.commit()
    row = conn.execute("SELECT status FROM generated_contents").fetchone()
    assert row["status"] == "published"


def test_generated_contents_has_actual_result_columns(conn):
    conn.execute(
        "INSERT INTO generated_contents "
        "(platform, content_type, body, actual_view_count, actual_like_count, "
        " actual_comment_count, actual_impression_count) "
        "VALUES ('X', 'post_text', 'test', 15000, 800, 45, 50000)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT actual_view_count, actual_like_count, actual_comment_count, "
        "actual_impression_count, actual_result_recorded_at FROM generated_contents"
    ).fetchone()
    assert row["actual_view_count"] == 15000
    assert row["actual_like_count"] == 800
    assert row["actual_comment_count"] == 45
    assert row["actual_impression_count"] == 50000
    # actual_result_recorded_at はアプリ側（record_result.py）が明示的に設定する列であり、
    # 生INSERTでは自動設定されない
    assert row["actual_result_recorded_at"] is None


def test_init_db_is_idempotent(tmp_path):
    # 既にinit_db済みのDBに対して再度呼んでもエラーにならないこと
    db_path = tmp_path / "idempotent.sqlite"
    init_db(db_path)
    init_db(db_path)  # 2回目もエラーなく完了すること
    c = get_connection(db_path)
    assert c.execute("SELECT COUNT(*) AS n FROM generated_contents").fetchone()["n"] == 0
    c.close()


def _build_legacy_generated_contents_db(db_path):
    """実績記録用カラム追加前(旧スキーマ)のDBを再現するヘルパー。"""

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_date TEXT NOT NULL DEFAULT (datetime('now')),
            period_start TEXT,
            period_end TEXT,
            target_content_ids TEXT,
            win_patterns TEXT,
            loss_patterns TEXT,
            summary TEXT,
            model_name TEXT NOT NULL,
            raw_response TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE generated_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER REFERENCES ai_analyses(id) ON DELETE SET NULL,
            platform TEXT NOT NULL CHECK (platform IN ('X', 'Instagram', 'TikTok', 'YouTube', 'note')),
            content_type TEXT NOT NULL,
            title TEXT,
            body TEXT NOT NULL,
            target_persona TEXT,
            evaluation_score REAL CHECK (evaluation_score IS NULL OR (evaluation_score BETWEEN 0 AND 100)),
            evaluation_reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision')),
            draft_file_path TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO generated_contents (id, platform, content_type, body, status, evaluation_score) "
        "VALUES (1, 'X', 'post_text', '既存の本文', 'approved', 88.0)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_columns_and_preserves_existing_data(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    _build_legacy_generated_contents_db(db_path)

    init_db(db_path)  # 旧スキーマ検出 → 移行が走るはず

    conn = get_connection(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_contents)")}
    assert {
        "actual_view_count", "actual_like_count",
        "actual_comment_count", "actual_impression_count", "actual_result_recorded_at",
    } <= columns

    row = conn.execute("SELECT * FROM generated_contents WHERE id = 1").fetchone()
    assert row["body"] == "既存の本文"
    assert row["status"] == "approved"
    assert row["evaluation_score"] == 88.0
    assert row["actual_view_count"] is None

    # 移行後は新しいstatus値('published')が使えること
    conn.execute("UPDATE generated_contents SET status = 'published' WHERE id = 1")
    conn.commit()

    # AUTOINCREMENTの連番が引き継がれていること（既存id=1の次は2以降になる）
    cur = conn.execute(
        "INSERT INTO generated_contents (platform, content_type, body) VALUES ('X', 'post_text', 'new')"
    )
    assert cur.lastrowid == 2
    conn.close()


def test_migration_is_a_noop_when_already_migrated(tmp_path):
    db_path = tmp_path / "already_migrated.sqlite"
    init_db(db_path)  # 新規作成 = 最初から新スキーマ
    init_db(db_path)  # 再実行しても壊れないこと

    conn = get_connection(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_contents)")}
    assert "actual_view_count" in columns
    assert "search_keywords" in columns
    conn.close()


def test_new_db_has_search_keywords_column(tmp_path):
    db_path = tmp_path / "new.sqlite"
    init_db(db_path)
    conn = get_connection(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_contents)")}
    assert "search_keywords" in columns
    conn.close()


def test_migration_adds_search_keywords_via_alter_table_when_level4_already_applied(tmp_path):
    """actual_view_count等(LEVEL4)は既にあるが search_keywords が無いDBに対しては、
    テーブル再作成ではなく軽量な ALTER TABLE ADD COLUMN で追加されること。"""

    db_path = tmp_path / "level4_only.sqlite"
    init_db(db_path)  # 最新スキーマで作成した後、search_keywords列を手動で剥がして再現する

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        ALTER TABLE generated_contents RENAME TO generated_contents_tmp;
        CREATE TABLE generated_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER REFERENCES ai_analyses(id) ON DELETE SET NULL,
            platform TEXT NOT NULL CHECK (platform IN ('X', 'Instagram', 'TikTok', 'YouTube', 'note')),
            content_type TEXT NOT NULL,
            title TEXT,
            body TEXT NOT NULL,
            target_persona TEXT,
            evaluation_score REAL CHECK (evaluation_score IS NULL OR (evaluation_score BETWEEN 0 AND 100)),
            evaluation_reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision', 'published')),
            draft_file_path TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            actual_view_count INTEGER,
            actual_like_count INTEGER,
            actual_comment_count INTEGER,
            actual_impression_count INTEGER,
            actual_result_recorded_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO generated_contents SELECT
            id, analysis_id, platform, content_type, title, body, target_persona,
            evaluation_score, evaluation_reason, status, draft_file_path,
            reviewed_by, reviewed_at, actual_view_count, actual_like_count,
            actual_comment_count, actual_impression_count, actual_result_recorded_at,
            created_at, updated_at
        FROM generated_contents_tmp;
        DROP TABLE generated_contents_tmp;
        """
    )
    conn.execute(
        "INSERT INTO generated_contents (id, platform, content_type, body, actual_view_count) "
        "VALUES (5, 'TikTok', 'video_script', '既存の台本', 12345)"
    )
    conn.commit()
    conn.close()

    init_db(db_path)  # search_keywords列だけが無い状態 → ALTER TABLE ADD COLUMNで追加されるはず

    conn = get_connection(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_contents)")}
    assert "search_keywords" in columns

    row = conn.execute("SELECT * FROM generated_contents WHERE id = 5").fetchone()
    assert row["body"] == "既存の台本"
    assert row["actual_view_count"] == 12345
    assert row["search_keywords"] is None
    conn.close()
