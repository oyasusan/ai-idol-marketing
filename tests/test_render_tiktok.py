import json
from pathlib import Path

import pytest

from src.db.models import get_connection, init_db
from src.video.render_tiktok import main, render_tiktok_video


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


def _insert_tiktok_content(
    conn,
    platform="TikTok",
    narration_text="これはナレーションです。",
    search_keywords=None,
):
    keywords_json = json.dumps(search_keywords) if search_keywords is not None else None
    cursor = conn.execute(
        """
        INSERT INTO generated_contents
            (platform, content_type, body, narration_text, search_keywords)
        VALUES (?, 'video_script', '台本本文', ?, ?)
        """,
        (platform, narration_text, keywords_json),
    )
    conn.commit()
    return cursor.lastrowid


def test_render_tiktok_video_missing_content_id(db_path):
    result = render_tiktok_video(9999, db_path=db_path)
    assert result["ok"] is False
    assert "9999" in result["error"]


def test_render_tiktok_video_rejects_non_tiktok_platform(db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, platform="X", search_keywords=["kw"])
    conn.close()

    result = render_tiktok_video(content_id, db_path=db_path)
    assert result["ok"] is False
    assert "TikTok" in result["error"]


def test_render_tiktok_video_rejects_empty_narration(db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, narration_text="", search_keywords=["kw"])
    conn.close()

    result = render_tiktok_video(content_id, db_path=db_path)
    assert result["ok"] is False
    assert "narration_text" in result["error"]


def test_render_tiktok_video_rejects_missing_keywords(db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, search_keywords=None)
    conn.close()

    result = render_tiktok_video(content_id, db_path=db_path)
    assert result["ok"] is False
    assert "search_keywords" in result["error"]


def test_render_tiktok_video_fetch_failure_returns_error(monkeypatch, db_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, search_keywords=["ライブ"])
    conn.close()

    monkeypatch.setattr("src.video.render_tiktok.fetch_video_assets", lambda *a, **k: [])

    result = render_tiktok_video(content_id, db_path=db_path)
    assert result["ok"] is False
    assert "素材" in result["error"]


def test_render_tiktok_video_compose_failure_returns_error(monkeypatch, db_path, tmp_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, search_keywords=["ライブ"])
    conn.close()

    monkeypatch.setattr(
        "src.video.render_tiktok.fetch_video_assets", lambda *a, **k: [Path("/tmp/clip.mp4")]
    )
    monkeypatch.setattr("src.video.render_tiktok.compose_tiktok_video", lambda *a, **k: None)

    result = render_tiktok_video(content_id, db_path=db_path, videos_dir=tmp_path)
    assert result["ok"] is False
    assert "動画合成" in result["error"] or "音声生成" in result["error"]


def test_render_tiktok_video_success(monkeypatch, db_path, tmp_path):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(
        conn, narration_text="ナレーションテキスト", search_keywords=["ライブ", "M/V"]
    )
    conn.close()

    captured = {}

    def fake_fetch(keywords, dest_dir=None, db_path=None, **kwargs):
        captured["keywords"] = keywords
        return [Path("/tmp/clip1.mp4"), Path("/tmp/clip2.mp4")]

    def fake_compose(clip_paths, narration_text, output_path, bgm_dir=None, **kwargs):
        captured["clip_paths"] = clip_paths
        captured["narration_text"] = narration_text
        captured["output_path"] = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake video")
        return output_path

    monkeypatch.setattr("src.video.render_tiktok.fetch_video_assets", fake_fetch)
    monkeypatch.setattr("src.video.render_tiktok.compose_tiktok_video", fake_compose)

    result = render_tiktok_video(content_id, db_path=db_path, videos_dir=tmp_path)

    assert result["ok"] is True
    assert result["content_id"] == content_id
    assert Path(result["output_path"]).exists()
    assert captured["keywords"] == ["ライブ", "M/V"]
    assert captured["narration_text"] == "ナレーションテキスト"
    assert f"tiktok_{content_id}.mp4" in str(captured["output_path"])


# ---- CLIエントリーポイント ----


def test_cli_main_success(monkeypatch, db_path, tmp_path, capsys):
    conn = get_connection(db_path)
    content_id = _insert_tiktok_content(conn, search_keywords=["ライブ"])
    conn.close()

    def fake_fetch(keywords, dest_dir=None, db_path=None, **kwargs):
        return [Path("/tmp/clip.mp4")]

    def fake_compose(clip_paths, narration_text, output_path, bgm_dir=None, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake")
        return output_path

    monkeypatch.setattr("src.video.render_tiktok.fetch_video_assets", fake_fetch)
    monkeypatch.setattr("src.video.render_tiktok.compose_tiktok_video", fake_compose)
    monkeypatch.setattr("src.video.render_tiktok.VIDEOS_DIR", tmp_path)

    exit_code = main(["--content-id", str(content_id), "--db-path", str(db_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    output_json = json.loads(captured.out)
    assert output_json["ok"] is True


def test_cli_main_failure_exits_nonzero(db_path):
    exit_code = main(["--content-id", "9999", "--db-path", str(db_path)])
    assert exit_code == 1
