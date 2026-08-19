import pytest

from src.db.models import ContentStatus, get_connection, init_db
from src.db.record_result import main, record_result


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_generated_content(conn, platform="X", body="テスト本文"):
    cursor = conn.execute(
        "INSERT INTO generated_contents (platform, content_type, body) VALUES (?, 'post_text', ?)",
        (platform, body),
    )
    conn.commit()
    return cursor.lastrowid


def test_record_result_missing_id_returns_error(db_path):
    result = record_result(9999, status=ContentStatus.PUBLISHED, db_path=db_path)
    assert result["ok"] is False
    assert "9999" in result["error"]


def test_record_result_updates_status_and_metrics(db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    result = record_result(
        content_id,
        status=ContentStatus.PUBLISHED,
        views=15000,
        likes=800,
        comments=45,
        impressions=50000,
        db_path=db_path,
    )
    assert result == {"ok": True, "id": content_id, "error": None}

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT status, actual_view_count, actual_like_count, actual_comment_count, "
        "actual_impression_count, actual_result_recorded_at FROM generated_contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    assert row["status"] == "published"
    assert row["actual_view_count"] == 15000
    assert row["actual_like_count"] == 800
    assert row["actual_comment_count"] == 45
    assert row["actual_impression_count"] == 50000
    assert row["actual_result_recorded_at"] is not None
    conn.close()


def test_record_result_status_only_does_not_touch_metrics(db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    result = record_result(content_id, status=ContentStatus.REJECTED, db_path=db_path)
    assert result["ok"] is True

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT status, actual_view_count, actual_result_recorded_at "
        "FROM generated_contents WHERE id = ?",
        (content_id,),
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["actual_view_count"] is None
    assert row["actual_result_recorded_at"] is None
    conn.close()


def test_record_result_metrics_only_does_not_touch_status(db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    result = record_result(content_id, views=5000, db_path=db_path)
    assert result["ok"] is True

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT status, actual_view_count FROM generated_contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["status"] == "pending"  # 変更されていない
    assert row["actual_view_count"] == 5000
    conn.close()


def test_record_result_works_against_legacy_schema(tmp_path):
    """record_result() 自体が init_db() を呼ぶため、旧スキーマのDBに対しても安全に動くこと。"""
    from tests.test_db_models import _build_legacy_generated_contents_db

    db_path = tmp_path / "legacy.sqlite"
    _build_legacy_generated_contents_db(db_path)  # id=1 の既存レコードを含む

    result = record_result(1, status=ContentStatus.PUBLISHED, views=999, db_path=db_path)
    assert result == {"ok": True, "id": 1, "error": None}


# ---- CLIエントリーポイント (main関数) ----


def test_cli_main_success(db_path, capsys):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    exit_code = main([
        "--id", str(content_id),
        "--status", "PUBLISHED",
        "--views", "15000",
        "--likes", "800",
        "--db-path", str(db_path),
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "記録しました" in captured.out
    assert "status = published" in captured.out
    assert "views = 15000" in captured.out


def test_cli_main_missing_id_exits_nonzero(db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    exit_code = main(["--id", str(content_id + 1000), "--status", "PUBLISHED", "--db-path", str(db_path)])
    assert exit_code == 1


def test_cli_main_no_fields_given_exits_nonzero(db_path, capsys):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    exit_code = main(["--id", str(content_id), "--db-path", str(db_path)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "少なくとも1つ指定してください" in captured.err


def test_cli_main_invalid_status_choice_rejected(db_path):
    with pytest.raises(SystemExit):
        main(["--id", "1", "--status", "POSTED", "--db-path", str(db_path)])
