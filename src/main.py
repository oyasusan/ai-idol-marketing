"""
パイプライン統括エントリーポイント。

データ収集 → DB更新 → AI分析 → コンテンツ生成 → 事前評価 → 日次レポート出力
を一連の流れで実行する。

各ステップ（収集・分析・生成・評価）はそれぞれのモジュール内で例外を捕捉し、
失敗時も安全な結果辞書を返す設計になっている。本モジュールはそれを前提に、
あるステップが失敗しても可能な範囲で後続処理・レポート出力を継続する
（例: 分析に失敗した場合はコンテンツ生成をスキップするが、収集結果と
分析失敗の事実はレポートに記録する）。

実行:
    python src/main.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.settings import DRAFTS_DIR as DEFAULT_DRAFTS_DIR
from src.ai import analyzer, evaluator, generator
from src.ai.analyzer import format_patterns_text
from src.collectors import youtube
from src.db.models import get_connection

logger = logging.getLogger(__name__)


def _fetch_analysis_detail(conn: sqlite3.Connection, analysis_id: Optional[int]) -> Optional[sqlite3.Row]:
    if analysis_id is None:
        return None
    return conn.execute("SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone()


def _fetch_generated_contents_for_date(conn: sqlite3.Connection, run_date: date) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM generated_contents
        WHERE date(created_at) = ?
        ORDER BY platform, id
        """,
        (run_date.isoformat(),),
    ).fetchall()


def _render_markdown_report(
    run_date: date,
    summary: dict[str, Any],
    analysis_detail: Optional[sqlite3.Row],
    generated_rows: list[sqlite3.Row],
) -> str:
    collect = summary["collect"]
    analysis = summary["analysis"]
    generation = summary["generation"]
    evaluation = summary["evaluation"]

    lines: list[str] = [
        f"# 日次パイプラインレポート — {run_date.isoformat()}",
        "",
        f"実行時刻: {summary['started_at']} 〜 {summary['finished_at']} (UTC)",
        "",
        "## 1. データ収集（YouTube）",
        "",
        f"- 結果: {'成功' if collect.get('ok') else '失敗'}",
        f"- 取得件数: {collect.get('fetched', 0)}",
        f"- DB反映（UPSERT）件数: {collect.get('upserted', 0)}",
        "",
        "## 2. AI分析",
        "",
        f"- 結果: {'成功' if analysis.get('ok') else '失敗'}",
        f"- 分析対象動画数: {analysis.get('content_count', 0)}",
        f"- 分析ID: {analysis.get('analysis_id')}",
        "",
    ]

    if analysis_detail is not None:
        lines += [
            "### 分析サマリー",
            "",
            analysis_detail["summary"] or "(サマリーなし)",
            "",
            "### 勝ちパターン",
            "",
            format_patterns_text(analysis_detail["win_patterns"]),
            "",
            "### 負けパターン",
            "",
            format_patterns_text(analysis_detail["loss_patterns"]),
            "",
        ]
    else:
        lines += ["(分析データなし)", ""]

    lines += [
        "## 3. コンテンツ生成",
        "",
        f"- 生成件数: {generation.get('generated_count', 0)}",
        f"- 生成失敗プラットフォーム: {', '.join(generation.get('failed_platforms', [])) or 'なし'}",
        "",
    ]

    if generated_rows:
        lines += [
            "| プラットフォーム | 種別 | タイトル | 事前評価スコア | ステータス | ドラフト |",
            "|---|---|---|---|---|---|",
        ]
        for row in generated_rows:
            draft_link = (
                f"[開く]({Path(row['draft_file_path']).name})"
                if row["draft_file_path"]
                else "(ファイルなし)"
            )
            score = row["evaluation_score"]
            score_text = f"{score:.0f}" if score is not None else "未評価"
            title = (row["title"] or "(タイトルなし)").replace("|", "\\|")
            lines.append(
                f"| {row['platform']} | {row['content_type']} | {title} | "
                f"{score_text} | {row['status']} | {draft_link} |"
            )
        lines.append("")
    else:
        lines += ["(本日生成されたコンテンツはありません)", ""]

    lines += [
        "## 4. 事前評価",
        "",
        f"- 評価件数: {evaluation.get('evaluated', 0)}",
        f"- 評価スキップ件数: {evaluation.get('skipped', 0)}",
        "",
        "---",
        "",
        "※ 生成されたコンテンツはすべて `pending` ステータスで保存されています。"
        "本システムはSNSへの自動投稿を行いません。内容を確認の上、人間が承認・編集してから投稿してください。",
        "",
    ]

    return "\n".join(lines)


def write_daily_report(
    run_date: date,
    summary: dict[str, Any],
    drafts_dir: Path = DEFAULT_DRAFTS_DIR,
    db_path: Optional[Path] = None,
) -> dict[str, str]:
    """日次レポートを content/drafts/YYYY-MM-DD.md（および同名.json）へ出力する。

    ファイル書き込みに失敗しても例外は送出せず、ログを出力した上で空辞書を返す。
    """

    try:
        drafts_dir.mkdir(parents=True, exist_ok=True)

        conn = get_connection(db_path)
        try:
            analysis_detail = _fetch_analysis_detail(conn, summary["analysis"].get("analysis_id"))
            generated_rows = _fetch_generated_contents_for_date(conn, run_date)
        finally:
            conn.close()

        markdown = _render_markdown_report(run_date, summary, analysis_detail, generated_rows)
        md_path = drafts_dir / f"{run_date.isoformat()}.md"
        md_path.write_text(markdown, encoding="utf-8")

        json_payload = dict(summary)
        json_payload["generated_contents"] = [dict(row) for row in generated_rows]
        json_path = drafts_dir / f"{run_date.isoformat()}.json"
        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        return {"markdown": str(md_path), "json": str(json_path)}
    except OSError:
        logger.exception("日次レポートの書き込みに失敗しました。run_date=%s", run_date)
        return {}


def run_pipeline(
    run_date: Optional[date] = None,
    db_path: Optional[Path] = None,
    drafts_dir: Path = DEFAULT_DRAFTS_DIR,
) -> dict[str, Any]:
    """収集→分析→生成→評価→レポート出力を一連の流れで実行する。"""

    run_date = run_date or date.today()
    started_at = datetime.now(timezone.utc)
    logger.info("=== パイプライン開始: %s ===", run_date.isoformat())

    collect_result = youtube.collect_channel_videos(db_path=db_path)
    logger.info("収集結果: %s", collect_result)

    analysis_result = analyzer.run_analysis(db_path=db_path)
    logger.info("分析結果: %s", analysis_result)

    if analysis_result.get("ok"):
        generation_result = generator.generate_contents_for_all_platforms(
            analysis_id=analysis_result.get("analysis_id"),
            db_path=db_path,
        )
        logger.info("生成結果: %s", generation_result)
    else:
        logger.warning("分析に失敗したためコンテンツ生成をスキップします。")
        generation_result = {
            "ok": False,
            "analysis_id": None,
            "generated_count": 0,
            "failed_platforms": [],
            "draft_paths": [],
        }

    # 今回生成した件数が既定の評価上限を超えていても取りこぼさないようにする
    eval_limit = max(evaluator.DEFAULT_EVALUATION_LIMIT, generation_result.get("generated_count", 0))
    evaluation_result = evaluator.evaluate_pending_contents(limit=eval_limit, db_path=db_path)
    logger.info("評価結果: %s", evaluation_result)

    finished_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "run_date": run_date.isoformat(),
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collect": collect_result,
        "analysis": analysis_result,
        "generation": generation_result,
        "evaluation": evaluation_result,
    }

    summary["report_paths"] = write_daily_report(run_date, summary, drafts_dir=drafts_dir, db_path=db_path)

    logger.info("=== パイプライン終了: %s ===", run_date.isoformat())
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_pipeline()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
