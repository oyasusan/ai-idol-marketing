import json

import pytest

from src.db.models import get_connection, init_db
from src.video.backfill_narration import main


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_tiktok_content(conn, platform="TikTok", narration_text=None, search_keywords=None):
    keywords_json = json.dumps(search_keywords) if search_keywords is not None else None
    cursor = conn.execute(
        """
        INSERT INTO generated_contents
            (platform, content_type, title, body, target_persona, narration_text, search_keywords)
        VALUES (?, 'video_script', 'テストタイトル', '台本本文', '10代女性ファン', ?, ?)
        """,
        (platform, narration_text, keywords_json),
    )
    conn.commit()
    return cursor.lastrowid


def test_cli_main_fills_missing_fields(monkeypatch, db_path, capsys):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn)
    conn.close()

    from src.ai.generator import NarrationBackfillResult

    fake_result = NarrationBackfillResult(
        search_keywords=["ライブ"], narration_text="補完されたナレーション"
    )
    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: fake_result)

    exit_code = main(["--content-id", str(content_id), "--db-path", str(db_path)])
    assert exit_code == 0

    output_json = json.loads(capsys.readouterr().out)
    assert output_json == {"ok": True, "content_id": content_id, "filled": True, "error": None}


def test_cli_main_failure_exits_nonzero(db_path):
    exit_code = main(["--content-id", "9999", "--db-path", str(db_path)])
    assert exit_code == 1
