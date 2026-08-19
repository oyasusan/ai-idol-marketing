"""
分析モジュール。

`contents` テーブルに蓄積された実績データを取得し、Groq APIで
勝ちパターン・負けパターンを抽出して `ai_analyses` テーブルへ保存する。

いかなる異常時（データ不足・API失敗・レスポンス不整合）も例外を送出せず、
ログを出力した上で `ok: False` を含むサマリー辞書を返す。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from src.ai.groq_client import DEFAULT_MODEL, build_prompt, generate_json
from src.db.models import get_connection

logger = logging.getLogger(__name__)

# Groq無料枠のTPM(Tokens Per Minute)上限(既定モデルで8,000)に収まるよう、
# 実測で安定して成功する件数に抑えている。増やす場合はGroqのレート制限に注意すること。
DEFAULT_CONTENT_LIMIT = 50

# 過去実績コンテキスト(高/低パフォーマンス事例)としてプロンプトへ埋め込む件数。
# トークン消費を抑えるため少数に絞る。
DEFAULT_PAST_PERFORMANCE_LIMIT = 3


class WinLossPattern(BaseModel):
    pattern: str
    evidence: str
    supporting_video_ids: list[str] = []


class AnalysisResult(BaseModel):
    summary: str
    win_patterns: list[WinLossPattern] = []
    loss_patterns: list[WinLossPattern] = []
    recommendations: list[str] = []


def format_patterns_text(patterns_json: Optional[str]) -> str:
    """ai_analyses.win_patterns / loss_patterns (JSON文字列) を人間可読なテキストに整形する。

    generator.py / evaluator.py がプロンプトへ埋め込む際に共通で利用する。
    """

    if not patterns_json:
        return "(分析データなし)"
    try:
        patterns = json.loads(patterns_json)
    except json.JSONDecodeError:
        return patterns_json

    lines = [
        f"- {p.get('pattern', '')}（根拠: {p.get('evidence', '')}）"
        for p in patterns
        if isinstance(p, dict)
    ]
    return "\n".join(lines) if lines else "(パターンなし)"


def fetch_past_performance_examples(
    conn: sqlite3.Connection,
    top_n: int = DEFAULT_PAST_PERFORMANCE_LIMIT,
    bottom_n: int = DEFAULT_PAST_PERFORMANCE_LIMIT,
) -> dict[str, list[dict[str, Any]]]:
    """実際に投稿され成果が記録済み（`record_result.py` で `actual_view_count` 等が
    記録された）コンテンツから、高パフォーマンス/低パフォーマンスの実例を抽出する。

    分析(analysis_prompt)・生成(generation_prompt)の両方で、AIが実際の投稿結果に
    基づいて「勝ちパターンを再現し、負けパターンを回避する」ための根拠として使う
    （LEVEL 4: フィードバック・学習ループ）。
    """

    rows = conn.execute(
        """
        SELECT id, platform, content_type, title, body,
               actual_view_count, actual_like_count, actual_comment_count, actual_impression_count
        FROM generated_contents
        WHERE actual_view_count IS NOT NULL
        ORDER BY actual_view_count DESC
        """
    ).fetchall()

    examples = [dict(r) for r in rows]
    total = len(examples)

    if total <= top_n + bottom_n:
        # 記録件数が少ない場合、素直にtop_n/bottom_nを適用すると片方に全件寄ってしまう
        # （例: 総数2件でtop_n=bottom_n=3だと両方ともhighに入りlowが空になる）。
        # 上位/下位でバランスよく按分する（総数が奇数の場合は上位を1件多くする）。
        high_count = (total + 1) // 2
        low_count = total - high_count
    else:
        high_count = top_n
        low_count = bottom_n

    high_performers = examples[:high_count]
    low_performers = list(reversed(examples[total - low_count :])) if low_count else []

    return {"high_performers": high_performers, "low_performers": low_performers}


def format_past_performance_text(examples: dict[str, list[dict[str, Any]]]) -> str:
    """fetch_past_performance_examples() の結果をプロンプト埋め込み用テキストに整形する。"""

    def _format_list(items: list[dict[str, Any]]) -> str:
        if not items:
            return "(記録なし)"
        lines = []
        for item in items:
            metrics = []
            if item.get("actual_view_count") is not None:
                metrics.append(f"再生数{item['actual_view_count']}")
            if item.get("actual_like_count") is not None:
                metrics.append(f"いいね{item['actual_like_count']}")
            if item.get("actual_comment_count") is not None:
                metrics.append(f"コメント{item['actual_comment_count']}")
            if item.get("actual_impression_count") is not None:
                metrics.append(f"インプレッション{item['actual_impression_count']}")
            metrics_text = "・".join(metrics) if metrics else "実績値なし"

            body_preview = (item.get("body") or "").replace("\n", " ").strip()
            if len(body_preview) > 60:
                body_preview = body_preview[:60] + "…"

            title = item.get("title") or "(タイトルなし)"
            lines.append(
                f"- [{item['platform']}] {title}（{metrics_text}）\n  本文抜粋: {body_preview}"
            )
        return "\n".join(lines)

    return (
        "【高パフォーマンスだった投稿】\n"
        + _format_list(examples.get("high_performers", []))
        + "\n\n【低パフォーマンスだった投稿】\n"
        + _format_list(examples.get("low_performers", []))
    )


def _row_to_analysis_input(row: sqlite3.Row) -> dict[str, Any]:
    tags: list[str] = []
    if row["tags"]:
        try:
            tags = json.loads(row["tags"])
        except json.JSONDecodeError:
            tags = []
    return {
        "id": row["id"],
        "title": row["title"],
        "published_at": row["published_at"],
        "duration_seconds": row["duration_seconds"],
        "tags": tags,
        "view_count": row["view_count"],
        "like_count": row["like_count"],
        "comment_count": row["comment_count"],
    }


def fetch_contents_for_analysis(
    conn: sqlite3.Connection,
    period_days: Optional[int] = None,
    limit: int = DEFAULT_CONTENT_LIMIT,
) -> list[sqlite3.Row]:
    """分析対象の contents 行を取得する（公開日時の新しい順）。"""

    query = "SELECT * FROM contents"
    params: list[Any] = []

    if period_days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        query += " WHERE published_at >= ?"
        params.append(since)

    query += " ORDER BY published_at DESC LIMIT ?"
    params.append(limit)

    return conn.execute(query, params).fetchall()


def _save_analysis(
    conn: sqlite3.Connection,
    result: AnalysisResult,
    model: str,
    period_start: Optional[str],
    period_end: Optional[str],
    target_content_ids: list[str],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ai_analyses (
            period_start, period_end, target_content_ids,
            win_patterns, loss_patterns, summary, model_name, raw_response
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            period_start,
            period_end,
            json.dumps(target_content_ids, ensure_ascii=False),
            json.dumps([p.model_dump() for p in result.win_patterns], ensure_ascii=False),
            json.dumps([p.model_dump() for p in result.loss_patterns], ensure_ascii=False),
            result.summary,
            model,
            result.model_dump_json(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def run_analysis(
    channel_name: str = "",
    period_days: Optional[int] = None,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
    model: str = DEFAULT_MODEL,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """contents テーブルを分析し、結果を ai_analyses へ保存する。

    Returns:
        {"ok": bool, "analysis_id": Optional[int], "content_count": int,
         "win_pattern_count": int, "loss_pattern_count": int}
    """

    conn = get_connection(db_path)
    try:
        rows = fetch_contents_for_analysis(conn, period_days=period_days, limit=content_limit)
        if not rows:
            logger.warning("分析対象のcontentsが見つかりませんでした。")
            return {"ok": False, "analysis_id": None, "content_count": 0,
                     "win_pattern_count": 0, "loss_pattern_count": 0}

        contents_input = [_row_to_analysis_input(row) for row in rows]
        published_ats = [c["published_at"] for c in contents_input if c["published_at"]]
        period_start = min(published_ats) if published_ats else None
        period_end = max(published_ats) if published_ats else None

        past_performance_context = format_past_performance_text(
            fetch_past_performance_examples(conn)
        )

        prompt = build_prompt(
            "analysis_prompt",
            channel_name=channel_name,
            period_start=period_start or "",
            period_end=period_end or "",
            contents_json=json.dumps(contents_input, ensure_ascii=False, indent=2),
            past_performance_context=past_performance_context,
        )
        if prompt is None:
            logger.error("analysis_prompt テンプレートの読み込みに失敗したため分析を中止します。")
            return {"ok": False, "analysis_id": None, "content_count": len(rows),
                     "win_pattern_count": 0, "loss_pattern_count": 0}

        result = generate_json(prompt, response_schema=AnalysisResult, model=model)
        if result is None:
            logger.error("Groq APIから有効な分析結果を取得できませんでした。")
            return {"ok": False, "analysis_id": None, "content_count": len(rows),
                     "win_pattern_count": 0, "loss_pattern_count": 0}

        analysis_id = _save_analysis(
            conn,
            result,
            model=model,
            period_start=period_start,
            period_end=period_end,
            target_content_ids=[c["id"] for c in contents_input],
        )

        return {
            "ok": True,
            "analysis_id": analysis_id,
            "content_count": len(rows),
            "win_pattern_count": len(result.win_patterns),
            "loss_pattern_count": len(result.loss_patterns),
        }
    except sqlite3.Error:
        logger.exception("分析結果のDB保存中にエラーが発生しました。")
        conn.rollback()
        return {"ok": False, "analysis_id": None, "content_count": 0,
                 "win_pattern_count": 0, "loss_pattern_count": 0}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = run_analysis()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
