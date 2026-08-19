"""
Groq API（OpenAI SDK互換）共通クライアント。

- プロンプトテンプレート(`prompts/*.md`)の読み込み・変数展開
- Groq APIを安全に呼び出し、構造化データ(JSON)として受け取る

Groq APIは `https://api.groq.com/openai/v1` でOpenAI Chat Completions API互換の
エンドポイントを提供しているため、`openai` パッケージの `OpenAI` クライアントに
`base_url` を差し替えて利用する。

`response_format={"type": "json_object"}` はJSON構文であることのみを保証し、
Geminiの `response_schema` のようなスキーマレベルの強制はできない。そのため、
取得したJSONは本モジュール側で `response_schema`（pydanticモデル）を用いて
バリデーションする。

APIエラー・レスポンス不整合(JSONパース失敗・スキーマ不一致等)が発生してもシステム全体を
落とさないよう、ログを出力した上で `None` を返して安全にフォールバックする。
呼び出し元は戻り値が `None` の場合の扱い（スキップ・再試行・人間への通知等）を判断すること。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, TypeVar

from openai import APIError, OpenAI
from pydantic import BaseModel, ValidationError

from config.settings import settings, PROMPTS_DIR

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = settings.groq_model
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


def _get_client(api_key: Optional[str] = None) -> Optional[OpenAI]:
    key = api_key or settings.groq_api_key
    if not key:
        logger.error("GROQ_API_KEY が設定されていません。")
        return None
    try:
        # SDK自身のリトライは無効化し、本モジュールのリトライループに統一する
        return OpenAI(api_key=key, base_url=GROQ_BASE_URL, max_retries=0)
    except Exception:
        logger.exception("Groq APIクライアントの初期化に失敗しました。")
        return None


def generate_json(
    prompt: str,
    response_schema: Optional[type[SchemaT]] = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    api_key: Optional[str] = None,
) -> Optional[SchemaT | dict]:
    """Groq APIを呼び出し、構造化JSONを取得する。

    `response_schema` にpydanticモデルを渡すとそのインスタンスを返す（バリデーション込み）。
    未指定の場合は `dict` を返す。

    APIエラー・JSONパース失敗・スキーマ不一致など、いかなる異常時も例外を送出せず
    ログ出力の上で `None` を返す。
    """

    client = _get_client(api_key)
    if client is None:
        return None

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return _parse_response(response, response_schema)
        except APIError as exc:
            last_error = exc
            logger.warning(
                "Groq API呼び出しに失敗しました（試行 %d/%d）: %s",
                attempt,
                max_retries,
                exc,
            )
        except Exception:
            logger.exception("Groq API呼び出し中に予期しないエラーが発生しました。")
            return None

        if attempt < max_retries:
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    logger.error("Groq API呼び出しがリトライ上限に達しました。最終エラー: %s", last_error)
    return None


def _parse_response(
    response: object,
    response_schema: Optional[type[SchemaT]],
) -> Optional[SchemaT | dict]:
    choices = getattr(response, "choices", None)
    if not choices:
        logger.error("Groq APIレスポンスにchoicesが含まれていません。response=%s", response)
        return None

    text = choices[0].message.content
    if not text:
        logger.error("Groq APIレスポンスにテキストが含まれていません。response=%s", response)
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Groq APIレスポンスのJSONパースに失敗しました。raw_text=%s", text[:500])
        return None

    if response_schema is None:
        return data

    try:
        return response_schema.model_validate(data)
    except ValidationError:
        logger.exception(
            "Groq APIレスポンスが期待スキーマと一致しません。schema=%s data=%s",
            response_schema.__name__,
            data,
        )
        return None
