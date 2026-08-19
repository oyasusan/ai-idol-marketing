import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.db.models import get_connection, init_db
from src.main import run_pipeline


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


@pytest.fixture()
def drafts_dir(tmp_path):
    return tmp_path / "drafts"


TODAY = date.today()


def test_run_pipeline_full_success_writes_report(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO ai_analyses (id, summary, win_patterns, loss_patterns, model_name)
        VALUES (1, 'テスト分析サマリー', ?, ?, 'gemini-2.5-flash')
        """,
        (
            json.dumps([{"pattern": "テキストサムネ", "evidence": "再生数2倍", "supporting_video_ids": ["v1"]}]),
            json.dumps([{"pattern": "無地サムネ", "evidence": "再生数半減", "supporting_video_ids": ["v2"]}]),
        ),
    )
    conn.execute(
        """
        INSERT INTO generated_contents
            (analysis_id, platform, content_type, title, body, target_persona,
             evaluation_score, status, draft_file_path)
        VALUES (1, 'X', 'post_text', 'テスト投稿', '本文です', '10代女性', 88, 'pending', ?)
        """,
        (str(drafts_dir / "0001_X_post_text.md"),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "src.main.youtube.collect_channel_videos",
        lambda db_path=None: {"fetched": 1, "upserted": 1, "video_ids": ["v1"], "ok": True},
    )
    monkeypatch.setattr(
        "src.main.analyzer.run_analysis",
        lambda db_path=None: {"ok": True, "analysis_id": 1, "content_count": 1,
                                "win_pattern_count": 1, "loss_pattern_count": 1},
    )

    generator_mock = MagicMock(
        return_value={
            "ok": True, "analysis_id": 1, "generated_count": 1,
            "failed_platforms": [], "draft_paths": [str(drafts_dir / "0001_X_post_text.md")],
        }
    )
    monkeypatch.setattr("src.main.generator.generate_contents_for_all_platforms", generator_mock)

    evaluator_mock = MagicMock(return_value={"ok": True, "evaluated": 1, "skipped": 0})
    monkeypatch.setattr("src.main.evaluator.evaluate_pending_contents", evaluator_mock)

    result = run_pipeline(run_date=TODAY, db_path=db_path, drafts_dir=drafts_dir)

    assert result["collect"]["ok"] is True
    assert result["analysis"]["ok"] is True
    assert result["generation"]["generated_count"] == 1
    assert result["evaluation"]["evaluated"] == 1

    # generatorはanalysis_id=1で呼ばれ、evaluatorは既定上限(20)以上を維持していること
    generator_mock.assert_called_once_with(analysis_id=1, db_path=db_path)
    _, eval_kwargs = evaluator_mock.call_args
    assert eval_kwargs["limit"] == 20

    md_path = drafts_dir / f"{TODAY.isoformat()}.md"
    json_path = drafts_dir / f"{TODAY.isoformat()}.json"
    assert md_path.exists()
    assert json_path.exists()

    md_content = md_path.read_text(encoding="utf-8")
    assert f"# 日次パイプラインレポート — {TODAY.isoformat()}" in md_content
    assert "テスト分析サマリー" in md_content
    assert "テキストサムネ" in md_content
    assert "無地サムネ" in md_content
    assert "テスト投稿" in md_content
    assert "| X | post_text |" in md_content
    assert "88" in md_content
    assert "自動投稿を行いません" in md_content

    json_content = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_content["run_date"] == TODAY.isoformat()
    assert len(json_content["generated_contents"]) == 1
    assert json_content["generated_contents"][0]["platform"] == "X"


def test_run_pipeline_analysis_failure_skips_generation(monkeypatch, db_path, drafts_dir):
    monkeypatch.setattr(
        "src.main.youtube.collect_channel_videos",
        lambda db_path=None: {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False},
    )
    monkeypatch.setattr(
        "src.main.analyzer.run_analysis",
        lambda db_path=None: {"ok": False, "analysis_id": None, "content_count": 0,
                                "win_pattern_count": 0, "loss_pattern_count": 0},
    )

    generator_mock = MagicMock()
    monkeypatch.setattr("src.main.generator.generate_contents_for_all_platforms", generator_mock)

    monkeypatch.setattr(
        "src.main.evaluator.evaluate_pending_contents",
        lambda limit=None, db_path=None: {"ok": True, "evaluated": 0, "skipped": 0},
    )

    result = run_pipeline(run_date=TODAY, db_path=db_path, drafts_dir=drafts_dir)

    generator_mock.assert_not_called()
    assert result["generation"]["ok"] is False
    assert result["generation"]["generated_count"] == 0

    md_content = (drafts_dir / f"{TODAY.isoformat()}.md").read_text(encoding="utf-8")
    assert "(分析データなし)" in md_content
    assert "(本日生成されたコンテンツはありません)" in md_content


def test_write_daily_report_returns_empty_dict_on_write_failure(monkeypatch, db_path, tmp_path):
    from src.main import write_daily_report

    # drafts_dirとして既存の「通常ファイル」を渡すことで mkdir(FileExistsError) を誘発する
    colliding_path = tmp_path / "not_a_directory"
    colliding_path.write_text("dummy", encoding="utf-8")

    summary = {
        "run_date": TODAY.isoformat(),
        "started_at": "2026-08-19T00:00:00Z",
        "finished_at": "2026-08-19T00:01:00Z",
        "collect": {"ok": True, "fetched": 0, "upserted": 0},
        "analysis": {"ok": False, "analysis_id": None, "content_count": 0},
        "generation": {"ok": False, "generated_count": 0, "failed_platforms": []},
        "evaluation": {"ok": True, "evaluated": 0, "skipped": 0},
    }

    report_paths = write_daily_report(TODAY, summary, drafts_dir=colliding_path, db_path=db_path)
    assert report_paths == {}
