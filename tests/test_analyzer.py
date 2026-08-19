import json

import pytest

from src.ai.analyzer import (
    AnalysisResult,
    WinLossPattern,
    fetch_contents_for_analysis,
    fetch_past_performance_examples,
    format_past_performance_text,
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


# ---- 過去実績コンテキスト (LEVEL 4: フィードバック・学習ループ) ----


def _insert_generated_content_with_result(
    conn, platform="X", body="本文", title="タイトル", views=None, likes=None, comments=None
):
    cursor = conn.execute(
        """
        INSERT INTO generated_contents
            (platform, content_type, title, body, actual_view_count, actual_like_count, actual_comment_count)
        VALUES (?, 'post_text', ?, ?, ?, ?, ?)
        """,
        (platform, title, body, views, likes, comments),
    )
    conn.commit()
    return cursor.lastrowid


def test_fetch_past_performance_examples_no_recorded_results_returns_empty(db_path):
    conn = get_connection(db_path)
    _insert_generated_content_with_result(conn, title="未記録", views=None)  # 実績未記録
    examples = fetch_past_performance_examples(conn)
    assert examples == {"high_performers": [], "low_performers": []}
    conn.close()


def test_fetch_past_performance_examples_splits_high_and_low(db_path):
    conn = get_connection(db_path)
    _insert_generated_content_with_result(conn, title="バズった投稿", views=50000, likes=3000)
    _insert_generated_content_with_result(conn, title="普通の投稿", views=1000, likes=50)
    _insert_generated_content_with_result(conn, title="伸びなかった投稿", views=50, likes=1)

    examples = fetch_past_performance_examples(conn, top_n=1, bottom_n=1)

    assert len(examples["high_performers"]) == 1
    assert examples["high_performers"][0]["title"] == "バズった投稿"
    assert len(examples["low_performers"]) == 1
    assert examples["low_performers"][0]["title"] == "伸びなかった投稿"
    conn.close()


def test_fetch_past_performance_examples_no_duplicate_when_few_records(db_path):
    conn = get_connection(db_path)
    _insert_generated_content_with_result(conn, title="唯一の実績", views=1000)

    examples = fetch_past_performance_examples(conn, top_n=3, bottom_n=3)

    # high側に既に含まれるレコードはlow側に重複して出さない
    assert len(examples["high_performers"]) == 1
    assert examples["low_performers"] == []
    conn.close()


def test_format_past_performance_text_no_records():
    text = format_past_performance_text({"high_performers": [], "low_performers": []})
    assert "(記録なし)" in text


def test_format_past_performance_text_includes_metrics_and_body_preview():
    examples = {
        "high_performers": [
            {
                "id": 1,
                "platform": "X",
                "title": "バズった投稿",
                "body": "これはとても長い本文のテストです。" * 5,
                "actual_view_count": 50000,
                "actual_like_count": 3000,
                "actual_comment_count": None,
                "actual_impression_count": None,
            }
        ],
        "low_performers": [],
    }
    text = format_past_performance_text(examples)
    assert "高パフォーマンス" in text
    assert "バズった投稿" in text
    assert "再生数50000" in text
    assert "いいね3000" in text
    assert "…" in text  # 60字超で省略される


def test_run_analysis_includes_past_performance_context_in_prompt(monkeypatch, db_path):
    conn = get_connection(db_path)
    _insert_content(conn, "v1", "動画1", "2026-01-01T00:00:00Z")
    _insert_generated_content_with_result(conn, title="過去のバズ投稿", views=99999, likes=5000)
    conn.close()

    captured_prompts = []

    def fake_generate_json(prompt, **kwargs):
        captured_prompts.append(prompt)
        return AnalysisResult(summary="テスト", win_patterns=[], loss_patterns=[], recommendations=[])

    monkeypatch.setattr("src.ai.analyzer.generate_json", fake_generate_json)

    result = run_analysis(db_path=db_path)

    assert result["ok"] is True
    assert len(captured_prompts) == 1
    assert "過去のバズ投稿" in captured_prompts[0]
    assert "99999" in captured_prompts[0]
