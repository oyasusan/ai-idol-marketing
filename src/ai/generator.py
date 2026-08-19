"""
コンテンツ生成モジュール。

`ai_analyses` の分析結果（勝ちパターン・負けパターン）を根拠として、
X / Instagram / TikTok / YouTube / note の5プラットフォーム向けに
投稿文・動画台本などのコンテンツ案をGemini APIで生成する。

CLAUDE.mdの方針により、SNSへの自動投稿は一切行わない。生成物は
`generated_contents` テーブルへ `status='pending'` として保存されると同時に、
`content/drafts/` 配下にMarkdownファイルとして出力し、人間の承認待ちとする。

いかなる異常時（分析データ不足・API失敗・レスポンス不整合）も例外を送出せず、
ログを出力した上で処理を継続または安全に打ち切る。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from config.settings import DRAFTS_DIR
from src.ai.analyzer import format_patterns_text
from src.ai.gemini_client import DEFAULT_MODEL, build_prompt, generate_json
from src.db.models import ContentStatus, Platform, get_connection

logger = logging.getLogger(__name__)

ALL_PLATFORMS: tuple[Platform, ...] = (
    Platform.X,
    Platform.INSTAGRAM,
    Platform.TIKTOK,
    Platform.YOUTUBE,
    Platform.NOTE,
)


class GeneratedContentItem(BaseModel):
    platform: str
    content_type: str
    title: Optional[str] = None
    body: str
    target_persona: Optional[str] = None
    hashtags: list[str] = []
    rationale: Optional[str] = None


class GenerationResult(BaseModel):
    contents: list[GeneratedContentItem] = []


def _fetch_analysis(conn: sqlite3.Connection, analysis_id: Optional[int]) -> Optional[sqlite3.Row]:
    if analysis_id is not None:
        return conn.execute(
            "SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM ai_analyses ORDER BY analysis_date DESC, id DESC LIMIT 1"
    ).fetchone()


def _compose_body(item: GeneratedContentItem) -> str:
    if not item.hashtags:
        return item.body
    hashtag_line = " ".join(f"#{tag.lstrip('#')}" for tag in item.hashtags)
    return f"{item.body}\n\n{hashtag_line}"


def _write_draft_file(
    content_id: int,
    platform: str,
    content_type: str,
    title: Optional[str],
    body: str,
    target_persona: Optional[str],
    rationale: Optional[str],
    analysis_id: Optional[int],
) -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_content_type = content_type.replace("/", "_").replace(" ", "_")
    filename = f"{content_id:04d}_{platform}_{safe_content_type}.md"
    path = DRAFTS_DIR / filename

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# {title or '(タイトルなし)'}",
        "",
        f"- platform: {platform}",
        f"- content_type: {content_type}",
        f"- target_persona: {target_persona or '(未指定)'}",
        "- status: pending（人間の承認待ち）",
        f"- generated_at: {generated_at}",
        f"- analysis_id: {analysis_id if analysis_id is not None else '(なし)'}",
        "",
        "## 本文",
        "",
        body,
        "",
        "## AIによる企画意図（人間レビュー用の参考情報）",
        "",
        rationale or "(なし)",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _save_generated_content(
    conn: sqlite3.Connection,
    analysis_id: Optional[int],
    platform: Platform,
    item: GeneratedContentItem,
) -> Optional[Path]:
    # platform列は要求したプラットフォームで確定させる（Geminiの出力ゆれによる
    # CHECK制約違反や意図しない値の混入を防ぐため、item.platformは信用しない）
    body = _compose_body(item)
    cursor = conn.execute(
        """
        INSERT INTO generated_contents (
            analysis_id, platform, content_type, title, body,
            target_persona, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            platform.value,
            item.content_type,
            item.title,
            body,
            item.target_persona,
            ContentStatus.PENDING.value,
        ),
    )
    content_id = cursor.lastrowid

    try:
        draft_path = _write_draft_file(
            content_id=content_id,
            platform=platform.value,
            content_type=item.content_type,
            title=item.title,
            body=body,
            target_persona=item.target_persona,
            rationale=item.rationale,
            analysis_id=analysis_id,
        )
    except OSError:
        logger.exception("ドラフトファイルの書き込みに失敗しました。content_id=%s", content_id)
        return None

    conn.execute(
        "UPDATE generated_contents SET draft_file_path = ? WHERE id = ?",
        (str(draft_path), content_id),
    )
    return draft_path


def generate_for_platform(
    analysis_row: sqlite3.Row,
    platform: Platform,
    content_type: str = "",
    additional_context: str = "",
    model: str = DEFAULT_MODEL,
) -> Optional[GenerationResult]:
    """1プラットフォーム分のコンテンツ案をGemini APIで生成する。"""

    prompt = build_prompt(
        "generation_prompt",
        platform=platform.value,
        content_type=content_type,
        summary=analysis_row["summary"] or "",
        win_patterns=format_patterns_text(analysis_row["win_patterns"]),
        loss_patterns=format_patterns_text(analysis_row["loss_patterns"]),
        additional_context=additional_context,
    )
    if prompt is None:
        logger.error("generation_prompt テンプレートの読み込みに失敗しました。")
        return None

    result = generate_json(prompt, response_schema=GenerationResult, model=model)
    if result is None:
        logger.error("Gemini APIから有効な生成結果を取得できませんでした。platform=%s", platform.value)
        return None
    return result


def generate_contents_for_all_platforms(
    analysis_id: Optional[int] = None,
    platforms: Optional[list[Platform]] = None,
    content_type: str = "",
    additional_context: str = "",
    model: str = DEFAULT_MODEL,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """指定analysis（未指定なら最新）を根拠に、全プラットフォーム分のコンテンツを生成・保存する。

    1プラットフォームの生成に失敗しても他のプラットフォームの処理は継続する。

    Returns:
        {"ok": bool, "analysis_id": Optional[int], "generated_count": int,
         "failed_platforms": list[str], "draft_paths": list[str]}
    """

    platforms = platforms or list(ALL_PLATFORMS)
    conn = get_connection(db_path)
    try:
        analysis_row = _fetch_analysis(conn, analysis_id)
        if analysis_row is None:
            logger.error("分析データが見つかりません。先に analyzer.run_analysis() を実行してください。")
            return {"ok": False, "analysis_id": None, "generated_count": 0,
                     "failed_platforms": [], "draft_paths": []}

        generated_count = 0
        failed_platforms: list[str] = []
        draft_paths: list[str] = []

        for platform in platforms:
            result = generate_for_platform(
                analysis_row,
                platform,
                content_type=content_type,
                additional_context=additional_context,
                model=model,
            )
            if result is None or not result.contents:
                failed_platforms.append(platform.value)
                continue

            for item in result.contents:
                try:
                    draft_path = _save_generated_content(conn, analysis_row["id"], platform, item)
                except sqlite3.Error:
                    logger.exception(
                        "生成コンテンツのDB保存に失敗しました。platform=%s", platform.value
                    )
                    conn.rollback()
                    continue
                conn.commit()
                generated_count += 1
                if draft_path is not None:
                    draft_paths.append(str(draft_path))

        return {
            "ok": True,
            "analysis_id": analysis_row["id"],
            "generated_count": generated_count,
            "failed_platforms": failed_platforms,
            "draft_paths": draft_paths,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = generate_contents_for_all_platforms()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
