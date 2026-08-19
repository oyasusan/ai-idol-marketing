"""
事前評価モジュール。

`generated_contents` テーブルの未評価レコード（`evaluation_score IS NULL`）を対象に、
Groq APIで投稿前チェック観点のスコアリングを行い、結果を同テーブルへ保存する。

この評価はあくまで人間の承認判断を補助する参考情報であり、
`status`（承認ステータス）は本モジュールでは一切変更しない
（承認・却下は必ず人間が行うというCLAUDE.mdの方針に準拠）。

いかなる異常時（API失敗・レスポンス不整合）も例外を送出せず、
ログを出力した上で該当レコードをスキップして処理を継続する。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.ai.analyzer import format_patterns_text
from src.ai.groq_client import DEFAULT_MODEL, build_prompt, generate_json
from src.db.models import get_connection

logger = logging.getLogger(__name__)

DEFAULT_EVALUATION_LIMIT = 20


class EvaluationResult(BaseModel):
    evaluation_score: int = Field(ge=0, le=100)
    evaluation_reason: str
    strengths: list[str] = []
    risks: list[str] = []
    improvement_suggestions: list[str] = []
    recommended_status: Literal["approved", "needs_revision", "rejected"]


def fetch_pending_evaluation(
    conn: sqlite3.Connection, limit: int = DEFAULT_EVALUATION_LIMIT
) -> list[sqlite3.Row]:
    """未評価（evaluation_score IS NULL）の生成コンテンツを古い順に取得する。"""

    return conn.execute(
        """
        SELECT * FROM generated_contents
        WHERE evaluation_score IS NULL
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _fetch_win_patterns_text(conn: sqlite3.Connection, analysis_id: Optional[int]) -> str:
    if analysis_id is None:
        return "(分析データなし)"
    row = conn.execute(
        "SELECT win_patterns FROM ai_analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    if row is None:
        return "(分析データなし)"
    return format_patterns_text(row["win_patterns"])


def evaluate_content(
    content_row: sqlite3.Row,
    win_patterns_text: str,
    model: str = DEFAULT_MODEL,
) -> Optional[EvaluationResult]:
    """1件の生成コンテンツをGroq APIで事前評価する。"""

    prompt = build_prompt(
        "evaluation_prompt",
        platform=content_row["platform"],
        content_type=content_row["content_type"],
        title=content_row["title"] or "",
        body=content_row["body"],
        target_persona=content_row["target_persona"] or "",
        win_patterns=win_patterns_text,
    )
    if prompt is None:
        logger.error("evaluation_prompt テンプレートの読み込みに失敗しました。")
        return None

    result = generate_json(prompt, response_schema=EvaluationResult, model=model)
    if result is None:
        logger.error(
            "Groq APIから有効な評価結果を取得できませんでした。content_id=%s",
            content_row["id"],
        )
    return result


def _compose_evaluation_reason(result: EvaluationResult) -> str:
    parts = [f"[AI推奨: {result.recommended_status}] {result.evaluation_reason}"]
    if result.strengths:
        parts.append("良い点: " + " / ".join(result.strengths))
    if result.risks:
        parts.append("懸念点: " + " / ".join(result.risks))
    if result.improvement_suggestions:
        parts.append("改善提案: " + " / ".join(result.improvement_suggestions))
    return "\n".join(parts)


def save_evaluation(conn: sqlite3.Connection, content_id: int, result: EvaluationResult) -> None:
    conn.execute(
        """
        UPDATE generated_contents
        SET evaluation_score = ?, evaluation_reason = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (result.evaluation_score, _compose_evaluation_reason(result), content_id),
    )


def evaluate_pending_contents(
    limit: int = DEFAULT_EVALUATION_LIMIT,
    model: str = DEFAULT_MODEL,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """未評価の生成コンテンツをまとめて事前評価する。

    Returns:
        {"ok": bool, "evaluated": int, "skipped": int}
    """

    conn = get_connection(db_path)
    try:
        rows = fetch_pending_evaluation(conn, limit=limit)
        if not rows:
            return {"ok": True, "evaluated": 0, "skipped": 0}

        win_patterns_cache: dict[Optional[int], str] = {}
        evaluated = 0
        skipped = 0

        for row in rows:
            analysis_id = row["analysis_id"]
            if analysis_id not in win_patterns_cache:
                win_patterns_cache[analysis_id] = _fetch_win_patterns_text(conn, analysis_id)

            result = evaluate_content(row, win_patterns_cache[analysis_id], model=model)
            if result is None:
                skipped += 1
                continue

            try:
                save_evaluation(conn, row["id"], result)
                conn.commit()
                evaluated += 1
            except sqlite3.Error:
                logger.exception("評価結果のDB保存に失敗しました。content_id=%s", row["id"])
                conn.rollback()
                skipped += 1

        return {"ok": True, "evaluated": evaluated, "skipped": skipped}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = evaluate_pending_contents()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
