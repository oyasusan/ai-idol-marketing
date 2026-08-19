"""
TikTok動画台本の narration_text/search_keywords 事後補完CLI。

`render_tiktok.py` は narration_text（TTS読み上げ用テキスト）と
search_keywords（動画素材検索用キーワード）が必須だが、これらのカラムが
導入される前に生成された `generated_contents` レコードには存在しない。
このCLIは、既存の body を根拠にGroq APIでこの2項目だけを補完し、DBへ書き戻す。

新しい企画を追加するものではなく、既に承認フローに載っている台本の
言い換え・抽出に徹する（`prompts/narration_backfill_prompt.md` 参照）。

使用例:
    python src/video/backfill_narration.py --content-id 21
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

# `python src/video/backfill_narration.py` のように直接実行された場合、
# プロジェクトルートがsys.pathに入らず src パッケージを解決できないため補正する。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.ai.generator import backfill_tiktok_video_fields  # noqa: E402


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "TikTok動画台本(generated_contents)のnarration_text/search_keywordsを、"
            "既存bodyを根拠にGroq APIで補完してDBへ書き戻す。"
        )
    )
    parser.add_argument(
        "--content-id", type=int, required=True, help="generated_contents.id (platform=TikTok)"
    )
    parser.add_argument(
        "--db-path", type=str, default=None, help="対象DBファイルパス（省略時は既定のDBを使用）"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    result = backfill_tiktok_video_fields(
        args.content_id, db_path=Path(args.db_path) if args.db_path else None
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
