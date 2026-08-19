import json

import pytest

from src.ai.analyzer import (
    AnalysisResult,
    WinLossPattern,
    fetch_contents_for_analysis,
    format_patterns_text,
    run_analysis,
)
from src.db.models import get_connection, init_db


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_content(conn, video_id, title, published_at, view_count=1000, tags=None):
    conn.execute(
        """
        INSERT INTO contents (id, title, published_at, channel_id, view_count, like_count, comment_count, tags)
        VALUES (?, ?, ?, 'UCtest', ?, 10, 2, ?)
        """,
        (video_id, title, published_at, view_count, json.dumps(tags or [])),
    )
    conn.commit()


def test_format_patterns_text_none_returns_placeholder():
    assert format_patterns_text(None) == "(分析データなし)"


def test_format_patterns_text_invalid_json_returns_raw():
    assert format_patterns_text("not json") == "not json"


def test_format_patterns_text_formats_bullets():
    patterns_json = json.dumps(
        [{"pattern": "サムネにテキスト", "evidence": "平均再生数が2倍", "supporting_video_ids": ["a"]}]
    )
    text = format_patterns_text(patterns_json)
    assert "サムネにテキスト" in text
    assert "平均再生数が2倍" in text


def test_fetch_contents_for_analysis_orders_by_published_at_desc(db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "動画1", "2026-01-01T00:00:00Z")
    _insert_content(conn, "v2", "動画2", "2026-03-01T00:00:00Z")
    rows = fetch_contents_for_analysis(conn)
    assert [r["id"] for r in rows] == ["v2", "v1"]
    conn.close()


def test_run_analysis_no_contents_returns_ok_false(db_path):
    result = run_analysis(db_path=db_path)
    assert result["ok"] is False
    assert result["analysis_id"] is None


def test_run_analysis_generate_json_failure_returns_ok_false(monkeypatch, db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "動画1", "2026-01-01T00:00:00Z")
    conn.close()

    monkeypatch.setattr("src.ai.analyzer.generate_json", lambda *a, **k: None)

    result = run_analysis(db_path=db_path)
    assert result["ok"] is False
    assert result["content_count"] == 1


def test_run_analysis_success_saves_to_db(monkeypatch, db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "動画1", "2026-01-01T00:00:00Z", view_count=5000)
    _insert_content(conn, "v2", "動画2", "2026-01-05T00:00:00Z", view_count=100)
    conn.close()

    fake_result = AnalysisResult(
        summary="サムネにテキストがある動画が伸びている",
        win_patterns=[
            WinLossPattern(pattern="テキストサムネ", evidence="v1は5000再生", supporting_video_ids=["v1"])
        ],
        loss_patterns=[
            WinLossPattern(pattern="無地サムネ", evidence="v2は100再生", supporting_video_ids=["v2"])
        ],
        recommendations=["次回はテキスト入りサムネを使う"],
    )
    monkeypatch.setattr("src.ai.analyzer.generate_json", lambda *a, **k: fake_result)

    result = run_analysis(channel_name="テストチャンネル", db_path=db_path)

    assert result["ok"] is True
    assert result["content_count"] == 2
    assert result["win_pattern_count"] == 1
    assert result["loss_pattern_count"] == 1
    assert result["analysis_id"] is not None

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM ai_analyses WHERE id = ?", (result["analysis_id"],)).fetchone()
    assert row["summary"] == fake_result.summary
    assert json.loads(row["target_content_ids"]) == ["v2", "v1"]
    assert "テキストサムネ" in row["win_patterns"]
    conn.close()
