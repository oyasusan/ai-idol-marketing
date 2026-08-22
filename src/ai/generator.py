"""
コンテンツ生成モジュール。

`ai_analyses` の分析結果（勝ちパターン・負けパターン）を根拠として、
X / Instagram / TikTok / YouTube / note の5プラットフォーム向けに
投稿文・動画台本などのコンテンツ案をGroq APIで生成する。

CLAUDE.mdの方針により、SNSへの自動投稿は一切行わない。生成物は
`generated_contents` テーブルへ `status='pending'` として保存されると同時に、
`content/drafts/` 配下にMarkdownファイルとして出力し、人間の承認待ちとする。

いかなる異常時（分析データ不足・API失敗・レスポンス不整合）も例外を送出せず、
ログを出力した上で処理を継続または安全に打ち切る。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from config.settings import DRAFTS_DIR
from src.ai.analyzer import (
    fetch_past_performance_examples,
    format_past_performance_text,
    format_patterns_text,
)
from src.ai.groq_client import DEFAULT_MODEL, build_prompt, generate_json
from src.db.models import ContentStatus, Platform, get_connection

logger = logging.getLogger(__name__)

ALL_PLATFORMS: tuple[Platform, ...] = (
    Platform.X,
    Platform.INSTAGRAM,
    Platform.TIKTOK,
    Platform.YOUTUBE,
    Platform.NOTE,
)

# Groq無料枠のTPM(Tokens Per Minute)制限に短時間で連続到達しないよう、
# プラットフォームごとの呼び出しの間に間隔を空ける。
_INTER_PLATFORM_DELAY_SECONDS = 15

# 「空想ロマンス」メンバー一覧（ユーザー確認済み、2026-08-22時点）。
# 動画タイトルのハッシュタグ集計からの自動抽出は曲名・サブユニット名等を
# 誤って含める恐れがあるため、人手で確認したこの固定リストを正とする。
# メンバー加入・卒業があれば都度更新すること。
GROUP_MEMBERS: tuple[str, ...] = (
    "双葉うゆ",
    "日向琴音",
    "まゆぴ",
    "月野愛実",
    "山田寿々",
    "高梨由衣",
    "瀬奈美玲",
)

_RECENT_TITLE_SAMPLE_LIMIT = 10


def select_featured_member(target_date: Optional[date] = None) -> str:
    """日替わりで特集するメンバーを1名選ぶ（日付ベースの巡回、状態保存不要）。"""

    target_date = target_date or date.today()
    return GROUP_MEMBERS[target_date.toordinal() % len(GROUP_MEMBERS)]


def _extract_title_hook(title: str) -> str:
    """動画タイトルからハッシュタグ部分を除いた「フック」文言を取り出す。"""

    return re.split(r"\s*#", title, maxsplit=1)[0].strip()


def fetch_recent_title_examples(
    conn: sqlite3.Connection,
    featured_member: str,
    limit: int = _RECENT_TITLE_SAMPLE_LIMIT,
) -> list[str]:
    """テーマの偏りを避けるため、実際の過去動画タイトルからフック文言を抽出する。

    特集メンバーの動画を優先しつつ、件数が少なければチャンネル全体から補う。
    生成プロンプトの「参考例」として渡し、LLMが同じような表現ばかりを
    繰り返さないようにするための、実データに基づく多様性確保の仕組み。
    """

    rows = conn.execute(
        "SELECT title FROM contents WHERE title LIKE ? ORDER BY published_at DESC LIMIT ?",
        (f"%{featured_member}%", limit),
    ).fetchall()
    hooks = [_extract_title_hook(r["title"]) for r in rows]

    if len(hooks) < limit:
        extra_rows = conn.execute(
            "SELECT title FROM contents ORDER BY published_at DESC LIMIT ?",
            (limit * 2,),
        ).fetchall()
        for r in extra_rows:
            hook = _extract_title_hook(r["title"])
            if hook and hook not in hooks:
                hooks.append(hook)
            if len(hooks) >= limit:
                break

    return [h for h in hooks if h][:limit]


class GeneratedContentItem(BaseModel):
    platform: str
    content_type: str
    title: Optional[str] = None
    body: str
    target_persona: Optional[str] = None
    hashtags: list[str] = []
    rationale: Optional[str] = None
    # 以下2つはTikTok動画台本の場合のみ使用。src/video/ の自動動画生成に使われる。
    search_keywords: list[str] = []  # 動画素材検索用キーワード
    narration_text: Optional[str] = None  # シーン指定を含まないTTS読み上げ用テキスト


class GenerationResult(BaseModel):
    contents: list[GeneratedContentItem] = []


class NarrationBackfillResult(BaseModel):
    search_keywords: list[str] = []
    narration_text: Optional[str] = None


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
    search_keywords: Optional[list[str]] = None,
    narration_text: Optional[str] = None,
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
    if narration_text:
        lines += [
            "## ナレーション読み上げテキスト（参考情報）",
            "",
            narration_text,
            "",
        ]
    if search_keywords:
        lines += [
            "## 動画素材の検索キーワード（参考情報）",
            "",
            "\n".join(f"- {kw}" for kw in search_keywords),
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
    # platform列は要求したプラットフォームで確定させる（LLMの出力ゆれによる
    # CHECK制約違反や意図しない値の混入を防ぐため、item.platformは信用しない）
    body = _compose_body(item)
    search_keywords_json = json.dumps(item.search_keywords, ensure_ascii=False) if item.search_keywords else None
    cursor = conn.execute(
        """
        INSERT INTO generated_contents (
            analysis_id, platform, content_type, title, body,
            target_persona, status, search_keywords, narration_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            platform.value,
            item.content_type,
            item.title,
            body,
            item.target_persona,
            ContentStatus.PENDING.value,
            search_keywords_json,
            item.narration_text,
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
            search_keywords=item.search_keywords,
            narration_text=item.narration_text,
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
    past_performance_context: str = "",
    featured_member: str = "",
    recent_title_examples: str = "",
) -> Optional[GenerationResult]:
    """1プラットフォーム分のコンテンツ案をGroq APIで生成する。"""

    prompt = build_prompt(
        "generation_prompt",
        platform=platform.value,
        content_type=content_type,
        summary=analysis_row["summary"] or "",
        win_patterns=format_patterns_text(analysis_row["win_patterns"]),
        loss_patterns=format_patterns_text(analysis_row["loss_patterns"]),
        additional_context=additional_context,
        past_performance_context=past_performance_context,
        featured_member=featured_member,
        recent_title_examples=recent_title_examples,
    )
    if prompt is None:
        logger.error("generation_prompt テンプレートの読み込みに失敗しました。")
        return None

    result = generate_json(prompt, response_schema=GenerationResult, model=model)
    if result is None:
        logger.error("Groq APIから有効な生成結果を取得できませんでした。platform=%s", platform.value)
        return None
    return result


def generate_contents_for_all_platforms(
    analysis_id: Optional[int] = None,
    platforms: Optional[list[Platform]] = None,
    content_type: str = "",
    additional_context: str = "",
    model: str = DEFAULT_MODEL,
    db_path: Optional[Path] = None,
    featured_member: Optional[str] = None,
) -> dict[str, Any]:
    """指定analysis（未指定なら最新）を根拠に、全プラットフォーム分のコンテンツを生成・保存する。

    1プラットフォームの生成に失敗しても他のプラットフォームの処理は継続する。
    `featured_member` を省略した場合、日付に応じてメンバーを自動で1名選出し
    （`select_featured_member()`）、生成する全プラットフォームの内容をそのメンバー
    中心の内容にする。

    Returns:
        {"ok": bool, "analysis_id": Optional[int], "generated_count": int,
         "failed_platforms": list[str], "draft_paths": list[str],
         "featured_member": str}
    """

    platforms = platforms or list(ALL_PLATFORMS)
    conn = get_connection(db_path)
    try:
        analysis_row = _fetch_analysis(conn, analysis_id)
        if analysis_row is None:
            logger.error("分析データが見つかりません。先に analyzer.run_analysis() を実行してください。")
            return {"ok": False, "analysis_id": None, "generated_count": 0,
                     "failed_platforms": [], "draft_paths": [], "featured_member": None}

        generated_count = 0
        failed_platforms: list[str] = []
        draft_paths: list[str] = []

        past_performance_context = format_past_performance_text(
            fetch_past_performance_examples(conn)
        )

        featured_member = featured_member or select_featured_member()
        recent_title_examples = "\n".join(
            f"- {hook}" for hook in fetch_recent_title_examples(conn, featured_member)
        )
        logger.info("本日の特集メンバー: %s", featured_member)

        for platform in platforms:
            # 直前の分析呼び出し等でTPM枠を使い切っている可能性があるため、
            # 1件目の呼び出し前にも間隔を空ける。
            time.sleep(_INTER_PLATFORM_DELAY_SECONDS)

            result = generate_for_platform(
                analysis_row,
                platform,
                content_type=content_type,
                additional_context=additional_context,
                model=model,
                past_performance_context=past_performance_context,
                featured_member=featured_member,
                recent_title_examples=recent_title_examples,
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
            "featured_member": featured_member,
        }
    finally:
        conn.close()


def backfill_tiktok_video_fields(
    content_id: int,
    model: str = DEFAULT_MODEL,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """指定content_idのTikTok動画台本に narration_text/search_keywords が
    欠けている場合、既存の body を根拠にGroq APIで補完しDBへ書き戻す。

    旧仕様（この2カラムが導入される前）で生成されたレコードを
    `src/video/render_tiktok.py` で使えるようにするための事後補完用。
    既に両方揃っている場合は何もせず filled=False を返す（冪等）。

    Returns:
        {"ok": bool, "content_id": int, "filled": bool, "error": Optional[str]}
    """

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM generated_contents WHERE id = ?", (content_id,)
        ).fetchone()
        if row is None:
            return {
                "ok": False, "content_id": content_id, "filled": False,
                "error": f"generated_contents.id={content_id} が見つかりません。",
            }

        if row["platform"] != Platform.TIKTOK.value:
            return {
                "ok": False, "content_id": content_id, "filled": False,
                "error": f"platform='{row['platform']}' はTikTok以外のため補完対象外です。",
            }

        has_narration = bool(row["narration_text"] and row["narration_text"].strip())
        has_keywords = bool(row["search_keywords"])
        if has_narration and has_keywords:
            logger.info("narration_text/search_keywordsは既に揃っています。content_id=%s", content_id)
            return {"ok": True, "content_id": content_id, "filled": False, "error": None}

        prompt = build_prompt(
            "narration_backfill_prompt",
            title=row["title"] or "",
            body=row["body"] or "",
            target_persona=row["target_persona"] or "",
        )
        if prompt is None:
            return {
                "ok": False, "content_id": content_id, "filled": False,
                "error": "narration_backfill_prompt テンプレートの読み込みに失敗しました。",
            }

        result = generate_json(prompt, response_schema=NarrationBackfillResult, model=model)
        if result is None or not result.narration_text or not result.search_keywords:
            return {
                "ok": False, "content_id": content_id, "filled": False,
                "error": "Groq APIから有効な補完結果を取得できませんでした。",
            }

        conn.execute(
            """
            UPDATE generated_contents
            SET search_keywords = ?, narration_text = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (json.dumps(result.search_keywords, ensure_ascii=False), result.narration_text, content_id),
        )
        conn.commit()
        logger.info("narration_text/search_keywordsを補完しました。content_id=%s", content_id)
        return {"ok": True, "content_id": content_id, "filled": True, "error": None}
    except sqlite3.Error as exc:
        conn.rollback()
        return {"ok": False, "content_id": content_id, "filled": False, "error": str(exc)}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = generate_contents_for_all_platforms()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
