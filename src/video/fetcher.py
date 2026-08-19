"""
動画素材の自動取得モジュール。

TikTok動画生成のための背景素材を、空想ロマンス公式チャンネル（このプロジェクトが
既に `contents` テーブル(`src/collectors/youtube_analytics_import.py`経由)へ
収集済みの唯一のチャンネル）の過去投稿の中からキーワードで検索し、`yt-dlp` で
ダウンロードする。

【重要: 取得元スコープについて】
第三者の著作権・YouTube利用規約への抵触を避けるため、検索対象は常に
ローカルDBの `contents` テーブル（= 公式チャンネルの既知の動画のみ）に限定する。
YouTube全体を対象にした公開検索(yt-dlpの `ytsearch:` 等)は一切行わない。
さらに、yt-dlpから取得した実際のメタデータでも channel_id を再検証し、
万一ローカルDBに他チャンネルのデータが混入していた場合でも安全側に倒す。

いかなる異常時（動画非公開・年齢制限・ネットワークエラー等）も例外を送出せず、
ログを出力した上でその候補をスキップし、次点候補を試す。

単体実行:
    python -m src.video.fetcher "ライブパフォーマンス" "M/V"
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from config.settings import DATA_DIR
from src.db.models import get_connection

logger = logging.getLogger(__name__)

# 空想ロマンス公式チャンネル。このプロジェクトが唯一収集対象としているチャンネルであり、
# 動画素材の取得元も常にこの範囲に限定する（著作権・利用規約リスクへの安全策）。
OFFICIAL_CHANNEL_ID = "UC4d3yIgfLvqtdPUAeV6neOw"

DOWNLOADS_DIR = DATA_DIR / "raw_assets" / "downloads"

DEFAULT_MIN_DURATION_SECONDS = 15
DEFAULT_MAX_DURATION_SECONDS = 180
DEFAULT_MIN_HEIGHT = 1080
DEFAULT_MAX_CLIPS = 2
# メタデータ確認の母数（フィルタで脱落する分を見込み max_clips より多めに検討する）
_CANDIDATE_POOL_SIZE = 8


def get_ffmpeg_location() -> Optional[str]:
    """system PATH上のffmpegを優先し、無ければ moviepy が依存する
    imageio_ffmpeg のバンドル版にフォールバックする。"""

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.warning("ffmpegが見つかりませんでした。動画のダウンロード/結合が失敗する可能性があります。")
        return None


def select_candidate_video_ids(
    conn: sqlite3.Connection,
    keywords: list[str],
    channel_id: str = OFFICIAL_CHANNEL_ID,
    limit: int = _CANDIDATE_POOL_SIZE,
) -> list[str]:
    """公式チャンネルの既知動画(`contents`テーブル)から、キーワードにタイトルが
    一致するものを再生数の多い順に選ぶ。

    YouTube全体への公開検索は行わず、常にローカルDBに既に収集済みの
    （= 公式チャンネルの）動画のみを対象とする。
    """

    if not keywords:
        return []

    conditions = " OR ".join(["title LIKE ?"] * len(keywords))
    like_params: list[Any] = [f"%{kw}%" for kw in keywords]

    rows = conn.execute(
        f"""
        SELECT id FROM contents
        WHERE ({conditions}) AND channel_id = ?
        ORDER BY view_count DESC
        LIMIT ?
        """,
        (*like_params, channel_id, limit),
    ).fetchall()

    return [row["id"] for row in rows]


def probe_video_metadata(
    video_id: str, ffmpeg_location: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """ダウンロードせずに動画のメタデータ（長さ・チャンネルID等）のみ取得する。"""

    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True}
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        logger.warning(
            "動画メタデータの取得に失敗したためスキップします。video_id=%s", video_id, exc_info=True
        )
        return None


def _passes_constraints(
    info: dict[str, Any],
    channel_id: str,
    min_duration: int,
    max_duration: int,
) -> bool:
    if info.get("channel_id") != channel_id:
        logger.warning(
            "想定チャンネル以外の動画のためスキップします（安全のため）。video_id=%s channel_id=%s",
            info.get("id"),
            info.get("channel_id"),
        )
        return False

    duration = info.get("duration")
    if duration is None or not (min_duration <= duration <= max_duration):
        return False

    return True


def download_video(
    video_id: str,
    dest_dir: Path,
    min_height: int = DEFAULT_MIN_HEIGHT,
    ffmpeg_location: Optional[str] = None,
) -> Optional[Path]:
    """指定動画IDを dest_dir へダウンロードする。解像度は min_height 以上を優先する。"""

    dest_dir.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "format": (
            f"bestvideo[height>={min_height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height>={min_height}]/best"
        ),
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    except Exception:
        logger.warning(
            "動画のダウンロードに失敗したためスキップします。video_id=%s", video_id, exc_info=True
        )
        return None

    filepath = dest_dir / f"{video_id}.mp4"
    return filepath if filepath.exists() else None


def fetch_video_assets(
    keywords: list[str],
    max_clips: int = DEFAULT_MAX_CLIPS,
    channel_id: str = OFFICIAL_CHANNEL_ID,
    min_duration: int = DEFAULT_MIN_DURATION_SECONDS,
    max_duration: int = DEFAULT_MAX_DURATION_SECONDS,
    min_height: int = DEFAULT_MIN_HEIGHT,
    dest_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> list[Path]:
    """キーワードに基づき、公式チャンネルの動画素材を検索・条件フィルタ・ダウンロードする。

    どのステップで失敗しても例外を送出せず、それまでにダウンロードできた
    範囲のファイルパス一覧を返す（空リストの可能性あり）。
    """

    dest_dir = dest_dir or DOWNLOADS_DIR
    ffmpeg_location = get_ffmpeg_location()

    conn = get_connection(db_path)
    try:
        candidate_ids = select_candidate_video_ids(conn, keywords, channel_id=channel_id)
    finally:
        conn.close()

    if not candidate_ids:
        logger.warning("キーワードに一致する候補動画が見つかりませんでした。keywords=%s", keywords)
        return []

    downloaded: list[Path] = []
    for video_id in candidate_ids:
        if len(downloaded) >= max_clips:
            break

        info = probe_video_metadata(video_id, ffmpeg_location=ffmpeg_location)
        if info is None:
            continue
        if not _passes_constraints(info, channel_id, min_duration, max_duration):
            continue

        filepath = download_video(
            video_id, dest_dir, min_height=min_height, ffmpeg_location=ffmpeg_location
        )
        if filepath is not None:
            downloaded.append(filepath)

    return downloaded


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cli_keywords = sys.argv[1:] or ["ライブ"]
    result_paths = fetch_video_assets(cli_keywords)
    print(json.dumps([str(p) for p in result_paths], ensure_ascii=False, indent=2))
