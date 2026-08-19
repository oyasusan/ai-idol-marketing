"""
youtube-analytics リポジトリ取り込みコレクター。

対象チャンネル（空想ロマンス）の実績データは、別リポジトリ
(https://github.com/oyasusan/youtube-analytics) が独自にYouTube Data API v3で
毎時収集し、SQLite DB (`data/youtube_analytics.db`) としてGitHubへコミットしている。
本モジュールはそのpublicなDBファイルをダウンロードし、動画メタデータと
最新の実績スナップショット（再生数・高評価数・コメント数）を突き合わせて、
本プロジェクトの `contents` テーブルへ冪等に UPSERT する。

そのため本モジュールの利用に YOUTUBE_API_KEY / YOUTUBE_CHANNEL_ID は不要。
（直接YouTube Data APIを叩く代替実装として src/collectors/youtube.py が別途存在する）

ダウンロード元DBには `duration_seconds` / `tags` に相当する列が存在しないため、
これらは常に None として取り込まれる。

ネットワークエラー・ダウンロード失敗・DB読み取り不整合が発生してもシステム全体を
落とさないよう、ログを出力した上で安全にフォールバックする。

単体実行:
    python -m src.collectors.youtube_analytics_import
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from config.settings import settings
from src.db.models import Content, get_connection, upsert_content

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS = 60

# ダウンロード元DBは動画本体(videos)と1時間おきスナップショット(video_snapshots)に
# 分かれているため、video_idごとに最新スナップショットを突き合わせて取得する。
_SELECT_LATEST_VIDEOS_SQL = """
SELECT
    v.video_id,
    v.channel_id,
    v.title,
    v.description,
    v.published_at,
    v.thumbnail_url,
    COALESCE(vs.view_count, 0) AS view_count,
    COALESCE(vs.like_count, 0) AS like_count,
    COALESCE(vs.comment_count, 0) AS comment_count
FROM videos v
LEFT JOIN (
    SELECT s.video_id, s.view_count, s.like_count, s.comment_count
    FROM video_snapshots s
    INNER JOIN (
        SELECT video_id, MAX(recorded_at) AS max_recorded_at
        FROM video_snapshots
        GROUP BY video_id
    ) latest
    ON s.video_id = latest.video_id AND s.recorded_at = latest.max_recorded_at
) vs ON v.video_id = vs.video_id
"""


def download_db(url: Optional[str] = None, dest_path: Optional[Path] = None) -> Optional[Path]:
    """youtube-analytics の公開DBファイルを一時ファイルへダウンロードする。

    失敗時はログを出力し `None` を返す。
    """

    url = url or settings.youtube_analytics_db_url
    if dest_path is None:
        fd, tmp_name = tempfile.mkstemp(suffix=".sqlite", prefix="youtube_analytics_")
        os.close(fd)
        dest_path = Path(tmp_name)

    try:
        urllib.request.urlretrieve(url, dest_path)
    except (urllib.error.URLError, OSError):
        logger.exception("youtube-analytics DBのダウンロードに失敗しました。url=%s", url)
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return None

    return dest_path


def _fetch_videos_with_latest_stats(db_path: Path) -> list[sqlite3.Row]:
    # 読み取り専用でオープンし、ダウンロードしたファイルを誤って書き換えないようにする
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(_SELECT_LATEST_VIDEOS_SQL).fetchall()
    finally:
        conn.close()


def _row_to_content(row: sqlite3.Row) -> Optional[Content]:
    try:
        return Content(
            id=row["video_id"],
            title=row["title"],
            description=row["description"] or None,
            published_at=row["published_at"],
            channel_id=row["channel_id"],
            duration_seconds=None,
            thumbnail_url=row["thumbnail_url"] or None,
            tags=None,
            view_count=row["view_count"],
            like_count=row["like_count"],
            comment_count=row["comment_count"],
        )
    except (KeyError, ValueError, TypeError):
        logger.exception("動画データの変換に失敗したためスキップします。row=%s", dict(row))
        return None


def import_from_youtube_analytics(
    url: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """youtube-analytics リポジトリのDBを取り込み、contents テーブルへ冪等にUPSERTする。

    Returns:
        {"ok": bool, "fetched": int, "upserted": int, "video_ids": list[str]}
    """

    downloaded_path = download_db(url)
    if downloaded_path is None:
        return {"ok": False, "fetched": 0, "upserted": 0, "video_ids": []}

    try:
        try:
            rows = _fetch_videos_with_latest_stats(downloaded_path)
        except sqlite3.Error:
            logger.exception("youtube-analytics DBの読み取りに失敗しました。")
            return {"ok": False, "fetched": 0, "upserted": 0, "video_ids": []}

        contents = [c for c in (_row_to_content(row) for row in rows) if c is not None]

        upserted = 0
        conn = get_connection(db_path)
        try:
            for content in contents:
                upsert_content(conn, content)
                upserted += 1
            conn.commit()
        except sqlite3.Error:
            logger.exception("DBへの書き込み中にエラーが発生したためロールバックします。")
            conn.rollback()
        finally:
            conn.close()

        return {
            "ok": True,
            "fetched": len(rows),
            "upserted": upserted,
            "video_ids": [c.id for c in contents],
        }
    finally:
        downloaded_path.unlink(missing_ok=True)


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = import_from_youtube_analytics()
    print(json.dumps(result, ensure_ascii=False, indent=2))
