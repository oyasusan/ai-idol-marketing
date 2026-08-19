"""
YouTube Data API v3 コレクター。

指定チャンネルの直近動画のメタデータ・実績指標（再生数/高評価/コメント数/公開日時）を取得し、
`contents` テーブルへ冪等に UPSERT する。

APIエラーやレスポンス不整合が発生してもシステム全体を落とさないよう、
各ステップでログを出力しつつ安全にフォールバック（空リスト返却）する。

単体実行:
    python -m src.collectors.youtube
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import settings
from src.db.models import Content, get_connection, upsert_content

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 50
_PLAYLIST_PAGE_SIZE = 50  # playlistItems.list の1回あたり最大件数
_VIDEOS_BATCH_SIZE = 50  # videos.list に一度に渡せるID数の上限

_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)
_THUMBNAIL_PRIORITY = ("maxres", "standard", "high", "medium", "default")


def parse_duration_seconds(iso_duration: Optional[str]) -> Optional[int]:
    """ISO8601 duration (例: 'PT4M13S') を秒数に変換する。パース不能なら None。"""

    if not iso_duration:
        return None
    match = _DURATION_RE.match(iso_duration)
    if not match:
        logger.warning("動画時間のパースに失敗しました: %s", iso_duration)
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _pick_thumbnail_url(thumbnails: dict) -> Optional[str]:
    for key in _THUMBNAIL_PRIORITY:
        thumb = thumbnails.get(key)
        if thumb and thumb.get("url"):
            return thumb["url"]
    return None


def _build_youtube_client(api_key: str):
    try:
        return build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    except Exception:
        logger.exception("YouTube APIクライアントの初期化に失敗しました。")
        return None


def _get_uploads_playlist_id(youtube, channel_id: str) -> Optional[str]:
    try:
        response = (
            youtube.channels()
            .list(part="contentDetails", id=channel_id)
            .execute()
        )
    except HttpError:
        logger.exception("チャンネル情報の取得に失敗しました。channel_id=%s", channel_id)
        return None

    items = response.get("items") or []
    if not items:
        logger.error("指定チャンネルが見つかりませんでした。channel_id=%s", channel_id)
        return None

    try:
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except KeyError:
        logger.exception("uploadsプレイリストIDの取得に失敗しました。channel_id=%s", channel_id)
        return None


def _fetch_recent_video_ids(youtube, uploads_playlist_id: str, max_results: int) -> list[str]:
    video_ids: list[str] = []
    page_token: Optional[str] = None

    while len(video_ids) < max_results:
        remaining = max_results - len(video_ids)
        try:
            response = (
                youtube.playlistItems()
                .list(
                    part="contentDetails",
                    playlistId=uploads_playlist_id,
                    maxResults=min(_PLAYLIST_PAGE_SIZE, remaining),
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError:
            logger.exception(
                "動画一覧の取得に失敗しました。playlist_id=%s", uploads_playlist_id
            )
            break

        for item in response.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return video_ids[:max_results]


def _fetch_video_details(youtube, video_ids: list[str]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []

    for i in range(0, len(video_ids), _VIDEOS_BATCH_SIZE):
        batch = video_ids[i : i + _VIDEOS_BATCH_SIZE]
        try:
            response = (
                youtube.videos()
                .list(part="snippet,contentDetails,statistics", id=",".join(batch))
                .execute()
            )
        except HttpError:
            logger.exception("動画詳細の取得に失敗しました。video_ids=%s", batch)
            continue

        videos.extend(response.get("items", []))

    return videos


def _to_content(video: dict[str, Any]) -> Optional[Content]:
    try:
        video_id = video["id"]
        snippet = video.get("snippet", {})
        statistics = video.get("statistics", {})
        content_details = video.get("contentDetails", {})

        return Content(
            id=video_id,
            title=snippet.get("title", ""),
            description=snippet.get("description"),
            published_at=snippet.get("publishedAt", ""),
            channel_id=snippet.get("channelId", ""),
            duration_seconds=parse_duration_seconds(content_details.get("duration")),
            thumbnail_url=_pick_thumbnail_url(snippet.get("thumbnails", {})),
            tags=json.dumps(snippet.get("tags", []), ensure_ascii=False),
            view_count=int(statistics.get("viewCount", 0)),
            like_count=int(statistics.get("likeCount", 0)),
            comment_count=int(statistics.get("commentCount", 0)),
        )
    except (KeyError, ValueError, TypeError):
        logger.exception("動画データの変換に失敗したためスキップします。video=%s", video.get("id"))
        return None


def collect_channel_videos(
    channel_id: Optional[str] = None,
    api_key: Optional[str] = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """指定チャンネルの直近動画を収集し contents テーブルへ冪等にUPSERTする。

    どのステップで失敗しても例外を送出せず、ログを出力した上で
    それまでに取得できた範囲の結果をまとめて返す。
    """

    channel_id = channel_id or settings.youtube_channel_id
    api_key = api_key or settings.youtube_api_key

    if not api_key or not channel_id:
        logger.error("YOUTUBE_API_KEY / YOUTUBE_CHANNEL_ID が設定されていません。")
        return {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False}

    youtube = _build_youtube_client(api_key)
    if youtube is None:
        return {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False}

    uploads_playlist_id = _get_uploads_playlist_id(youtube, channel_id)
    if not uploads_playlist_id:
        return {"fetched": 0, "upserted": 0, "video_ids": [], "ok": False}

    video_ids = _fetch_recent_video_ids(youtube, uploads_playlist_id, max_results)
    if not video_ids:
        logger.warning("収集対象の動画が見つかりませんでした。channel_id=%s", channel_id)
        return {"fetched": 0, "upserted": 0, "video_ids": [], "ok": True}

    videos_data = _fetch_video_details(youtube, video_ids)
    contents = [c for c in (_to_content(v) for v in videos_data) if c is not None]

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
        "fetched": len(videos_data),
        "upserted": upserted,
        "video_ids": [c.id for c in contents],
        "ok": True,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = collect_channel_videos()
    print(json.dumps(result, ensure_ascii=False, indent=2))
