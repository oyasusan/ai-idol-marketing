import json

import pytest

from src.ai.generator import (
    GeneratedContentItem,
    GenerationResult,
    NarrationBackfillResult,
    backfill_tiktok_video_fields,
    generate_contents_for_all_platforms,
)
from src.db.models import ContentStatus, Platform, get_connection, init_db


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


@pytest.fixture()
def drafts_dir(tmp_path, monkeypatch):
    path = tmp_path / "drafts"
    monkeypatch.setattr("src.ai.generator.DRAFTS_DIR", path)
    # プラットフォーム間のレート制限用ウェイトはテストでは待たない
    monkeypatch.setattr("src.ai.generator.time.sleep", lambda *_: None)
    return path


def _insert_analysis(conn):
    cursor = conn.execute(
        """
        INSERT INTO ai_analyses (model_name, summary, win_patterns, loss_patterns)
        VALUES ('openai/gpt-oss-20b', 'テスト要約', '[]', '[]')
        """
    )
    conn.commit()
    return cursor.lastrowid


def test_generate_contents_no_analysis_returns_ok_false(db_path, drafts_dir):
    result = generate_contents_for_all_platforms(db_path=db_path)
    assert result["ok"] is False
    assert result["generated_count"] == 0


def test_generate_contents_success_writes_db_and_draft_files(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    analysis_id = _insert_analysis(conn)
    conn.close()

    fake_result = GenerationResult(
        contents=[
            GeneratedContentItem(
                platform="TikTok",  # 意図的に誤ったplatformを返すモック(要求と不一致)
                content_type="post_text",
                title="テストタイトル",
                body="テスト本文です",
                target_persona="10代女性ファン",
                hashtags=["idol", "live"],
                rationale="勝ちパターンに基づく",
            )
        ]
    )
    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: fake_result)

    result = generate_contents_for_all_platforms(
        analysis_id=analysis_id,
        platforms=[Platform.X],
        db_path=db_path,
    )

    assert result["ok"] is True
    assert result["generated_count"] == 1
    assert result["failed_platforms"] == []
    assert len(result["draft_paths"]) == 1

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM generated_contents").fetchone()
    # item.platform="TikTok"を返しても、要求したPlatform.Xで保存されること
    assert row["platform"] == "X"
    assert row["status"] == ContentStatus.PENDING.value
    assert "#idol" in row["body"]
    assert "#live" in row["body"]
    assert row["draft_file_path"] is not None
    conn.close()

    draft_path = drafts_dir / f"{row['id']:04d}_X_post_text.md"
    assert draft_path.exists()
    content = draft_path.read_text(encoding="utf-8")
    assert "テストタイトル" in content
    assert "テスト本文です" in content
    assert "勝ちパターンに基づく" in content
    assert "pending" in content


def test_generate_contents_partial_failure_continues_other_platforms(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    analysis_id = _insert_analysis(conn)
    conn.close()

    def fake_generate_json(prompt, **kwargs):
        # プラットフォーム比較表に全プラットフォーム名が列挙されているため、
        # 実際に指定された行(対象プラットフォーム: X)で判定する
        if "対象プラットフォーム: TikTok" in prompt:
            return None  # TikTokだけ生成失敗をシミュレート
        return GenerationResult(
            contents=[
                GeneratedContentItem(
                    platform="X",
                    content_type="post_text",
                    body="本文",
                )
            ]
        )

    monkeypatch.setattr("src.ai.generator.generate_json", fake_generate_json)

    result = generate_contents_for_all_platforms(
        analysis_id=analysis_id,
        platforms=[Platform.X, Platform.TIKTOK],
        db_path=db_path,
    )

    assert result["ok"] is True
    assert result["generated_count"] == 1
    assert result["failed_platforms"] == ["TikTok"]


def test_generate_contents_includes_past_performance_context_in_prompt(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    analysis_id = _insert_analysis(conn)
    conn.execute(
        """
        INSERT INTO generated_contents
            (platform, content_type, title, body, actual_view_count, actual_like_count)
        VALUES ('X', 'post_text', '過去のバズ投稿', '過去の本文', 99999, 5000)
        """
    )
    conn.commit()
    conn.close()

    captured_prompts = []

    def fake_generate_json(prompt, **kwargs):
        captured_prompts.append(prompt)
        return GenerationResult(
            contents=[GeneratedContentItem(platform="X", content_type="post_text", body="新しい本文")]
        )

    monkeypatch.setattr("src.ai.generator.generate_json", fake_generate_json)

    result = generate_contents_for_all_platforms(
        analysis_id=analysis_id, platforms=[Platform.X], db_path=db_path
    )

    assert result["ok"] is True
    assert len(captured_prompts) == 1
    assert "過去のバズ投稿" in captured_prompts[0]
    assert "99999" in captured_prompts[0]


def test_generate_contents_persists_search_keywords_for_tiktok(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    analysis_id = _insert_analysis(conn)
    conn.close()

    fake_result = GenerationResult(
        contents=[
            GeneratedContentItem(
                platform="TikTok",
                content_type="video_script",
                title="台本タイトル",
                body="シーン1: フック\nシーン2: 本編",
                search_keywords=["空想ロマンス ライブ", "空想ロマンス M/V", "空想ロマンス ショート"],
            )
        ]
    )
    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: fake_result)

    result = generate_contents_for_all_platforms(
        analysis_id=analysis_id, platforms=[Platform.TIKTOK], db_path=db_path
    )
    assert result["ok"] is True
    assert result["generated_count"] == 1

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM generated_contents").fetchone()
    conn.close()

    import json as _json

    assert _json.loads(row["search_keywords"]) == [
        "空想ロマンス ライブ", "空想ロマンス M/V", "空想ロマンス ショート",
    ]

    draft_path = drafts_dir / f"{row['id']:04d}_TikTok_video_script.md"
    assert draft_path.exists()
    content = draft_path.read_text(encoding="utf-8")
    assert "動画素材の検索キーワード" in content
    assert "空想ロマンス ライブ" in content


def test_generate_contents_no_search_keywords_omits_draft_section(monkeypatch, db_path, drafts_dir):
    conn = get_connection(db_path)
    analysis_id = _insert_analysis(conn)
    conn.close()

    fake_result = GenerationResult(
        contents=[GeneratedContentItem(platform="X", content_type="post_text", body="本文のみ")]
    )
    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: fake_result)

    result = generate_contents_for_all_platforms(
        analysis_id=analysis_id, platforms=[Platform.X], db_path=db_path
    )
    assert result["ok"] is True

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM generated_contents").fetchone()
    conn.close()
    assert row["search_keywords"] is None

    draft_path = drafts_dir / f"{row['id']:04d}_X_post_text.md"
    content = draft_path.read_text(encoding="utf-8")
    assert "動画素材の検索キーワード" not in content


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


def test_backfill_missing_content_id_returns_ok_false(db_path):
    result = backfill_tiktok_video_fields(9999, db_path=db_path)
    assert result["ok"] is False
    assert "9999" in result["error"]


def test_backfill_rejects_non_tiktok_platform(db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, platform="X")
    conn.close()

    result = backfill_tiktok_video_fields(content_id, db_path=db_path)
    assert result["ok"] is False
    assert "TikTok" in result["error"]


def test_backfill_noop_when_already_filled(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(
        conn, narration_text="既存のナレーション", search_keywords=["既存キーワード"]
    )
    conn.close()

    called = False

    def _fail_if_called(*_a, **_k):
        nonlocal called
        called = True

    monkeypatch.setattr("src.ai.generator.generate_json", _fail_if_called)

    result = backfill_tiktok_video_fields(content_id, db_path=db_path)
    assert result == {"ok": True, "content_id": content_id, "filled": False, "error": None}
    assert called is False


def test_backfill_fills_missing_fields_and_persists(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn)
    conn.close()

    fake_result = NarrationBackfillResult(
        search_keywords=["ライブ", "笑顔"],
        narration_text="これは補完されたナレーションです。",
    )
    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: fake_result)

    result = backfill_tiktok_video_fields(content_id, db_path=db_path)
    assert result == {"ok": True, "content_id": content_id, "filled": True, "error": None}

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM generated_contents WHERE id = ?", (content_id,)).fetchone()
    conn.close()
    assert row["narration_text"] == "これは補完されたナレーションです。"
    assert json.loads(row["search_keywords"]) == ["ライブ", "笑顔"]


def test_backfill_returns_ok_false_when_groq_fails(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn)
    conn.close()

    monkeypatch.setattr("src.ai.generator.generate_json", lambda *a, **k: None)

    result = backfill_tiktok_video_fields(content_id, db_path=db_path)
    assert result["ok"] is False
    assert result["filled"] is False
