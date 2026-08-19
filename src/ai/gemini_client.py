"""
Gemini API 共通クライアント。

- プロンプトテンプレート(`prompts/*.md`)の読み込み・変数展開
- Gemini APIを安全に呼び出し、構造化データ(JSON)として受け取る

APIエラー・レスポンス不整合(JSONパース失敗・スキーマ不一致等)が発生してもシステム全体を
落とさないよう、ログを出力した上で `None` を返して安全にフォールバックする。
呼び出し元は戻り値が `None` の場合の扱い（スキップ・再試行・人間への通知等）を判断すること。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, TypeVar

from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ValidationError

from config.settings import settings, PROMPTS_DIR

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_RETRIES = 2  # 合計試行回数
_RETRY_BACKOFF_SECONDS = 1.5

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def load_prompt_template(name: str) -> Optional[str]:
    """prompts/{name} を読み込む。拡張子省略時は `.md` を補完する。"""

    filename = name if name.endswith(".md") else f"{name}.md"
    path = PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("プロンプトテンプレートの読み込みに失敗しました: %s", path)
        return None


def render_prompt(template: str, **variables: object) -> str:
    """`{{key}}` 形式のプレースホルダーを置換する軽量テンプレート（Jinja2等は不使用）。"""

    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", "" if value is None else str(value))
    return rendered


def build_prompt(template_name: str, **variables: object) -> Optional[str]:
    """テンプレートの読み込みと変数展開をまとめて行う。"""

    template = load_prompt_template(template_name)
    if template is None:
        return None
    return render_prompt(template, **variables)


def _get_client(api_key: Optional[str] = None) -> Optional[genai.Client]:
    key = api_key or settings.gemini_api_key
    if not key:
        logger.error("GEMINI_API_KEY が設定されていません。")
        return None
    try:
        return genai.Client(api_key=key)
    except Exception:
        logger.exception("Gemini APIクライアントの初期化に失敗しました。")
        return None


def generate_json(
    prompt: str,
    response_schema: Optional[type[SchemaT]] = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    api_key: Optional[str] = None,
) -> Optional[SchemaT | dict]:
    """Gemini APIを呼び出し、構造化JSONを取得する。

    `response_schema` にpydanticモデルを渡すとそのインスタンスを返す（バリデーション込み）。
    未指定の場合は `dict` を返す。

    APIエラー・JSONパース失敗・スキーマ不一致など、いかなる異常時も例外を送出せず
    ログ出力の上で `None` を返す。
    """

    client = _get_client(api_key)
    if client is None:
        return None

    config = types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=response_schema,
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            return _parse_response(response, response_schema)
        except APIError as exc:
            last_error = exc
            logger.warning(
                "Gemini API呼び出しに失敗しました（試行 %d/%d）: %s",
                attempt,
                max_retries,
                exc,
            )
        except Exception:
            logger.exception("Gemini API呼び出し中に予期しないエラーが発生しました。")
            return None

        if attempt < max_retries:
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    logger.error("Gemini API呼び出しがリトライ上限に達しました。最終エラー: %s", last_error)
    return None


def _parse_response(
    response: types.GenerateContentResponse,
    response_schema: Optional[type[SchemaT]],
) -> Optional[SchemaT | dict]:
    # SDKがスキーマに沿って自動パース済みならそれを優先する
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return parsed

    text = response.text
    if not text:
        logger.error("Gemini APIレスポンスにテキストが含まれていません。response=%s", response)
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Gemini APIレスポンスのJSONパースに失敗しました。raw_text=%s", text[:500])
        return None

    if response_schema is None:
        return data

    try:
        return response_schema.model_validate(data)
    except ValidationError:
        logger.exception(
            "Gemini APIレスポンスが期待スキーマと一致しません。schema=%s data=%s",
            response_schema.__name__,
            data,
        )
        return None
