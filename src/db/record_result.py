"""
成果記録CLI（LEVEL 4: フィードバック・学習ループ）。

生成済みコンテンツ (`generated_contents.id`) に対して、実際の投稿ステータスと
実績データ（再生数・いいね数・コメント数・インプレッション数）を記録する。

本システムはSNSへの自動投稿を行わない。実際の投稿はあくまで人間が手動で行い、
その結果をこのスクリプトで事後的に記録する。記録された実績は
src/ai/analyzer.py の分析・コンテンツ生成時に過去の成功/失敗事例として
参照される（学習ループ）。

使用例:
    python src/db/record_result.py --id 12 --status PUBLISHED --views 15000 --likes 800
    python src/db/record_result.py --id 12 --status REJECTED
    python src/db/record_result.py --id 12 --views 20000 --likes 950 --comments 30
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

# `python src/db/record_result.py` のように直接実行された場合、
# プロジェクトルートがsys.pathに入らず src パッケージを解決できないため補正する。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.db.models import ContentStatus, get_connection, init_db  # noqa: E402


def record_result(
    content_id: int,
    status: Optional[ContentStatus] = None,
    views: Optional[int] = None,
    likes: Optional[int] = None,
    comments: Optional[int] = None,
    impressions: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """generated_contents.id に対して投稿ステータス・実績データを記録する。

    Returns:
        {"ok": bool, "id": int, "error": Optional[str]}
    """

    init_db(db_path)  # 旧スキーマのDBでも安全に動くよう、必要なら移行しておく
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM generated_contents WHERE id = ?", (content_id,)
        ).fetchone()
        if existing is None:
            return {
                "ok": False,
                "id": content_id,
                "error": f"generated_contents.id={content_id} が見つかりません。",
            }

        set_clauses = ["updated_at = datetime('now')"]
        params: list[Any] = []

        if status is not None:
            set_clauses.append("status = ?")
            params.append(status.value)

        actual_fields = {
            "actual_view_count": views,
            "actual_like_count": likes,
            "actual_comment_count": comments,
            "actual_impression_count": impressions,
        }
        for column, value in actual_fields.items():
            if value is not None:
                set_clauses.append(f"{column} = ?")
                params.append(value)

        if any(v is not None for v in actual_fields.values()):
            set_clauses.append("actual_result_recorded_at = datetime('now')")

        params.append(content_id)
        conn.execute(
            f"UPDATE generated_contents SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
        return {"ok": True, "id": content_id, "error": None}
    except sqlite3.Error as exc:
        conn.rollback()
        return {"ok": False, "id": content_id, "error": str(exc)}
    finally:
        conn.close()


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成済みコンテンツの実際の投稿結果を記録する（SNSへの自動投稿は行わない）。"
    )
    parser.add_argument("--id", type=int, required=True, help="generated_contents.id")
    parser.add_argument(
        "--status",
        type=str,
        default=None,
        choices=[s.value.upper() for s in ContentStatus],
        help="投稿ステータス (PENDING/APPROVED/REJECTED/NEEDS_REVISION/PUBLISHED)。省略時は変更しない。",
    )
    parser.add_argument("--views", type=int, default=None, help="実際の再生数")
    parser.add_argument("--likes", type=int, default=None, help="実際のいいね数")
    parser.add_argument("--comments", type=int, default=None, help="実際のコメント数")
    parser.add_argument("--impressions", type=int, default=None, help="実際のインプレッション数")
    parser.add_argument(
        "--db-path", type=str, default=None, help="対象DBファイルパス（省略時は既定のDBを使用）"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    status = ContentStatus(args.status.lower()) if args.status else None

    if status is None and all(
        v is None for v in (args.views, args.likes, args.comments, args.impressions)
    ):
        print("エラー: --status か実績データ(--views/--likes/--comments/--impressions)を"
              "少なくとも1つ指定してください。", file=sys.stderr)
        return 1

    result = record_result(
        content_id=args.id,
        status=status,
        views=args.views,
        likes=args.likes,
        comments=args.comments,
        impressions=args.impressions,
        db_path=Path(args.db_path) if args.db_path else None,
    )

    if not result["ok"]:
        print(f"エラー: {result['error']}", file=sys.stderr)
        return 1

    print(f"記録しました: generated_contents.id={args.id}")
    if status is not None:
        print(f"  status = {status.value}")
    for label, value in (
        ("views", args.views),
        ("likes", args.likes),
        ("comments", args.comments),
        ("impressions", args.impressions),
    ):
        if value is not None:
            print(f"  {label} = {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
