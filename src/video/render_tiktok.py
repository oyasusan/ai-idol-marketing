"""
TikTok動画 全自動生成CLI。

指定した `generated_contents.id`（`platform='TikTok'` の投稿案）を元に、
【キーワード抽出 → 素材自動取得 → ナレーション音声生成 → 縦型動画合成】を
1コマンドで実行する。

生成された動画・下書きはあくまで人間が確認した上でTikTokへ手動投稿するための
素材であり、本システムはSNSへの自動投稿を一切行わない（CLAUDE.mdの方針）。
動画素材は空想ロマンス公式チャンネルの既知動画のみに限定して取得される
（詳細は src/video/fetcher.py を参照）。

対象レコードに narration_text/search_keywords が欠けている（この2カラム導入前に
生成された等）場合は、既存の body を根拠に Groq API で自動補完してから続行する
（詳細は src/ai/generator.py の backfill_tiktok_video_fields() を参照）。

`--target capcut` を指定すると、mp4への書き出しは行わず、動画素材・ナレーション音声・
字幕をタイムラインに配置しただけの CapCut Desktop 下書きプロジェクトを生成する
（詳細は src/video/capcut_draft.py を参照。CapCut Desktopがインストールされた
マシン上での実行を前提とする）。

使用例:
    python src/video/render_tiktok.py --content-id 5
    python src/video/render_tiktok.py --content-id 5 --target capcut
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

# `python src/video/render_tiktok.py` のように直接実行された場合、
# プロジェクトルートがsys.pathに入らず src パッケージを解決できないため補正する。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import DATA_DIR, VIDEOS_DIR  # noqa: E402
from src.ai.generator import backfill_tiktok_video_fields  # noqa: E402
from src.db.models import get_connection  # noqa: E402
from src.video.capcut_draft import build_capcut_draft  # noqa: E402
from src.video.fetcher import DOWNLOADS_DIR, fetch_video_assets  # noqa: E402
from src.video.generator import BGM_DIR, compose_tiktok_video, generate_narration_audio  # noqa: E402

logger = logging.getLogger(__name__)

NARRATION_DIR = DATA_DIR / "raw_assets" / "narration"


def _fetch_generated_content(conn: sqlite3.Connection, content_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM generated_contents WHERE id = ?", (content_id,)
    ).fetchone()


def _fail(content_id: int, message: str) -> dict[str, Any]:
    logger.error(message)
    return {
        "ok": False,
        "content_id": content_id,
        "output_path": None,
        "draft_path": None,
        "error": message,
    }


def render_tiktok_video(
    content_id: int,
    db_path: Optional[Path] = None,
    videos_dir: Optional[Path] = None,
    downloads_dir: Optional[Path] = None,
    bgm_dir: Optional[Path] = None,
    target: str = "render",
    narration_dir: Optional[Path] = None,
    capcut_draft_root: Optional[Path] = None,
) -> dict[str, Any]:
    """指定 content_id のTikTok投稿案から動画素材を取得し、`target` に応じた
    成果物を全自動生成する。

    - `target="render"`（既定）: 縦型mp4を書き出す。
    - `target="capcut"`: mp4への書き出しは行わず、動画素材・ナレーション音声・
      字幕を配置したCapCut Desktop下書きプロジェクトを生成する。

    どのステップで失敗しても例外を送出せず、`ok: False` とエラー内容を含む
    辞書を返す。

    Returns:
        {"ok": bool, "content_id": int, "output_path": Optional[str],
         "draft_path": Optional[str], "error": Optional[str]}
    """

    if target not in ("render", "capcut"):
        return _fail(content_id, f"target='{target}' は不正な値です（render または capcut）。")

    videos_dir = videos_dir or VIDEOS_DIR
    downloads_dir = downloads_dir or DOWNLOADS_DIR
    bgm_dir = bgm_dir or BGM_DIR
    narration_dir = narration_dir or NARRATION_DIR

    conn = get_connection(db_path)
    try:
        row = _fetch_generated_content(conn, content_id)
    finally:
        conn.close()

    if row is None:
        return _fail(content_id, f"generated_contents.id={content_id} が見つかりません。")

    if row["platform"] != "TikTok":
        return _fail(
            content_id,
            f"platform='{row['platform']}' はTikTok動画生成の対象外です（TikTokのみ対応）。",
        )

    # narration_text/search_keywordsが欠けている（この2カラム導入前に生成された等）
    # 場合は、bodyを根拠にGroq APIで自動補完してから続行する。
    if not (row["narration_text"] and row["narration_text"].strip()) or not row["search_keywords"]:
        logger.info(
            "narration_text/search_keywordsが不足しているため自動補完します。content_id=%s",
            content_id,
        )
        backfill_result = backfill_tiktok_video_fields(content_id, db_path=db_path)
        if not backfill_result["ok"]:
            return _fail(
                content_id,
                backfill_result["error"] or "narration_text/search_keywordsの自動補完に失敗しました。",
            )
        conn = get_connection(db_path)
        try:
            row = _fetch_generated_content(conn, content_id)
        finally:
            conn.close()
        if row is None:
            return _fail(content_id, f"generated_contents.id={content_id} が見つかりません。")

    narration_text = row["narration_text"] or ""
    if not narration_text.strip():
        return _fail(
            content_id,
            "narration_text が空です。generator.py が生成したTikTok案のみ対応しています。",
        )
    # bodyがあれば台本の「」/『』のセリフ・テロップ行を字幕として使う
    # （narration_textだけでは取りこぼす行があるため。resolve_caption_chunks参照）。
    body = row["body"] or ""

    keywords: list[str] = []
    if row["search_keywords"]:
        try:
            keywords = json.loads(row["search_keywords"])
        except json.JSONDecodeError:
            logger.warning(
                "search_keywords のJSONパースに失敗しました。content_id=%s", content_id
            )

    if not keywords:
        return _fail(content_id, "動画素材検索用キーワード(search_keywords)がありません。")

    logger.info("STEP 1/3: 素材取得を開始します。keywords=%s", keywords)
    clip_paths = fetch_video_assets(keywords, dest_dir=downloads_dir, db_path=db_path)
    if not clip_paths:
        return _fail(content_id, "条件に合う動画素材が見つかりませんでした。")
    logger.info("素材取得完了: %d件", len(clip_paths))

    if target == "render":
        output_path = videos_dir / f"{date.today().isoformat()}_tiktok_{content_id}.mp4"
        logger.info("STEP 2-3/3: ナレーション音声生成・動画合成を開始します。output=%s", output_path)
        result_path = compose_tiktok_video(
            clip_paths, narration_text, output_path, bgm_dir=bgm_dir, body=body
        )

        if result_path is None:
            return _fail(content_id, "ナレーション音声生成または動画合成に失敗しました。")

        logger.info("動画生成が完了しました: %s", result_path)
        return {
            "ok": True,
            "content_id": content_id,
            "output_path": str(result_path),
            "draft_path": None,
            "error": None,
        }

    # target == "capcut"
    # CapCutの下書きは音声ファイルを絶対パスで参照し続けるため、compose_tiktok_video内の
    # ナレーションと異なりレンダリング後も削除せず data/raw_assets/narration/ に残す。
    narration_path = narration_dir / f"{content_id}_narration.wav"
    logger.info("STEP 2/2: ナレーション音声生成・CapCut下書き生成を開始します。")
    narration_path = generate_narration_audio(narration_text, narration_path)
    if narration_path is None:
        return _fail(content_id, "ナレーション音声の生成に失敗しました。")

    draft_result = build_capcut_draft(
        content_id,
        clip_paths,
        narration_path,
        narration_text,
        draft_root=capcut_draft_root,
        body=body,
    )
    if not draft_result["ok"]:
        return _fail(content_id, draft_result["error"] or "CapCut下書きの生成に失敗しました。")

    logger.info("CapCut下書きの生成が完了しました: %s", draft_result["draft_path"])
    return {
        "ok": True,
        "content_id": content_id,
        "output_path": None,
        "draft_path": draft_result["draft_path"],
        "error": None,
    }


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "TikTok動画をキーワード抽出→素材取得→音声生成→縦型動画合成まで"
            "1コマンドで自動生成する。"
        )
    )
    parser.add_argument(
        "--content-id", type=int, required=True, help="generated_contents.id (platform=TikTok)"
    )
    parser.add_argument(
        "--db-path", type=str, default=None, help="対象DBファイルパス（省略時は既定のDBを使用）"
    )
    parser.add_argument(
        "--target",
        type=str,
        choices=["render", "capcut"],
        default="render",
        help=(
            "render: 縦型mp4を書き出す（既定）。"
            "capcut: mp4は書き出さず、CapCut Desktop下書きプロジェクトを生成する。"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    result = render_tiktok_video(
        args.content_id,
        db_path=Path(args.db_path) if args.db_path else None,
        target=args.target,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
