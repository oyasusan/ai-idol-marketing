from unittest.mock import MagicMock

import httpx
import pytest
from openai import APIConnectionError
from pydantic import BaseModel

from src.ai.groq_client import (
    build_prompt,
    generate_json,
    load_prompt_template,
    render_prompt,
)


# ---- プロンプトテンプレートの読み込み・変数展開 ----


@pytest.mark.parametrize(
    "name, expected_snippet",
    [
        ("analysis_prompt", "win_patterns"),
        ("generation_prompt", "target_persona"),
        ("evaluation_prompt", "evaluation_score"),
    ],
)
def test_load_prompt_template_reads_expected_files(name, expected_snippet):
    template = load_prompt_template(name)
    assert template is not None
    assert expected_snippet in template


def test_load_prompt_template_missing_file_returns_none():
    assert load_prompt_template("does_not_exist") is None


def test_render_prompt_substitutes_placeholders():
    template = "platform={{platform}}, count={{count}}, unset={{unset}}"
    rendered = render_prompt(template, platform="X", count=5)
    assert rendered == "platform=X, count=5, unset={{unset}}"


def test_render_prompt_none_value_becomes_empty_string():
    template = "value=[{{value}}]"
    rendered = render_prompt(template, value=None)
    assert rendered == "value=[]"


def test_build_prompt_loads_and_renders():
    prompt = build_prompt("evaluation_prompt", platform="X", content_type="post_text",
                           title="タイトル", body="本文", target_persona="10代女性",
                           win_patterns="サムネにテキストがあると伸びる")
    assert prompt is not None
    assert "{{platform}}" not in prompt
    assert "X" in prompt
    assert "10代女性" in prompt


# ---- Groq API 呼び出し (openai.OpenAI をモック) ----


class DummySchema(BaseModel):
    score: int
    reason: str


def _make_response(content: str):
    """openai の ChatCompletion レスポンスを模したモックを作る。"""
    message = MagicMock(content=content)
    choice = MagicMock(message=message)
    return MagicMock(choices=[choice])


def _make_mock_client(response):
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return client


def _dummy_api_connection_error() -> APIConnectionError:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return APIConnectionError(message="connection failed", request=req)


def test_generate_json_missing_api_key_returns_none(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "")
    result = generate_json("test prompt")
    assert result is None


def test_generate_json_returns_validated_schema_instance(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    response = _make_response('{"score": 90, "reason": "良い"}')
    mock_client = _make_mock_client(response)
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt", response_schema=DummySchema)
    assert isinstance(result, DummySchema)
    assert result.score == 90
    assert result.reason == "良い"

    # base_urlがGroqのOpenAI互換エンドポイントに設定されていること、
    # response_format が json_object 指定になっていることを確認
    create_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert create_kwargs["response_format"] == {"type": "json_object"}
    assert create_kwargs["messages"] == [{"role": "user", "content": "prompt"}]


def test_generate_json_without_schema_returns_dict(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    response = _make_response('{"foo": "bar"}')
    mock_client = _make_mock_client(response)
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt")
    assert result == {"foo": "bar"}


def test_generate_json_invalid_json_returns_none(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    response = _make_response("これはJSONではありません")
    mock_client = _make_mock_client(response)
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt")
    assert result is None


def test_generate_json_empty_content_returns_none(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    response = _make_response(None)
    mock_client = _make_mock_client(response)
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt")
    assert result is None


def test_generate_json_schema_mismatch_returns_none(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    # DummySchemaは score(int)/reason(str) が必須だが、scoreが欠落している
    response = _make_response('{"reason": "理由のみ"}')
    mock_client = _make_mock_client(response)
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt", response_schema=DummySchema)
    assert result is None


def test_generate_json_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    success_response = _make_response('{"foo": "bar"}')
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        _dummy_api_connection_error(),
        success_response,
    ]
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt", max_retries=2)
    assert result == {"foo": "bar"}
    assert mock_client.chat.completions.create.call_count == 2


def test_generate_json_returns_none_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr("src.ai.groq_client.settings.groq_api_key", "dummy-key")
    monkeypatch.setattr("src.ai.groq_client.time.sleep", lambda *_: None)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = _dummy_api_connection_error()
    monkeypatch.setattr(
        "src.ai.groq_client.OpenAI", lambda api_key, base_url, max_retries: mock_client
    )

    result = generate_json("prompt", max_retries=2)
    assert result is None
    assert mock_client.chat.completions.create.call_count == 2
