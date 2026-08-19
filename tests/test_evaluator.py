import pytest

from src.ai.evaluator import (
    EvaluationResult,
    evaluate_pending_contents,
)
from src.db.models import get_connection, init_db


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_generated_content(conn, platform="X", body="テスト本文"):
    cursor = conn.execute(
        """
        INSERT INTO generated_contents (platform, content_type, title, body, target_persona)
        VALUES (?, 'post_text', 'タイトル', ?, '10代女性')
        """,
        (platform, body),
    )
    conn.commit()
    return cursor.lastrowid


def test_evaluate_pending_contents_no_pending_returns_zero(db_path):
    result = evaluate_pending_contents(db_path=db_path)
    assert result == {"ok": True, "evaluated": 0, "skipped": 0}


def test_evaluate_pending_contents_success_updates_score(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    fake_result = EvaluationResult(
        evaluation_score=85,
        evaluation_reason="勝ちパターンに合致している",
        strengths=["共感を呼ぶ表現"],
        risks=["誇張表現の可能性"],
        improvement_suggestions=["具体的な日時を追加する"],
        recommended_status="approved",
    )
    monkeypatch.setattr("src.ai.evaluator.generate_json", lambda *a, **k: fake_result)

    result = evaluate_pending_contents(db_path=db_path)

    assert result == {"ok": True, "evaluated": 1, "skipped": 0}

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM generated_contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["evaluation_score"] == 85
    assert "[AI推奨: approved]" in row["evaluation_reason"]
    assert "勝ちパターンに合致している" in row["evaluation_reason"]
    assert "共感を呼ぶ表現" in row["evaluation_reason"]
    assert "誇張表現の可能性" in row["evaluation_reason"]
    # statusは人間が変更するまで変わらない（承認は自動化しない）
    assert row["status"] == "pending"
    conn.close()


def test_evaluate_pending_contents_skips_on_api_failure(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_generated_content(conn)
    conn.close()

    monkeypatch.setattr("src.ai.evaluator.generate_json", lambda *a, **k: None)

    result = evaluate_pending_contents(db_path=db_path)

    assert result == {"ok": True, "evaluated": 0, "skipped": 1}

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT evaluation_score FROM generated_contents WHERE id = ?", (content_id,)
    ).fetchone()
    assert row["evaluation_score"] is None
    conn.close()


def test_evaluate_pending_contents_respects_limit(monkeypatch, db_path):
    conn = get_connection(db_path)
    for i in range(3):
        _insert_generated_content(conn, body=f"本文{i}")
    conn.close()

    fake_result = EvaluationResult(
        evaluation_score=70,
        evaluation_reason="標準的",
        recommended_status="needs_revision",
    )
    monkeypatch.setattr("src.ai.evaluator.generate_json", lambda *a, **k: fake_result)

    result = evaluate_pending_contents(limit=2, db_path=db_path)
    assert result == {"ok": True, "evaluated": 2, "skipped": 0}

    conn = get_connection(db_path)
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM generated_contents WHERE evaluation_score IS NULL"
    ).fetchone()["c"]
    assert remaining == 1
    conn.close()
