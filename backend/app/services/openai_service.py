from __future__ import annotations

from dataclasses import dataclass


class OpenAIError(Exception):
    pass


@dataclass(frozen=True)
class OpenAIResult:
    text: str


def generate_text(*, prompt: str, content: str, model: str | None = None, api_key: str | None = None) -> OpenAIResult:
    resolved_key = (api_key or "").strip()
    if not resolved_key:
        raise OpenAIError("missing_openai_api_key")
    try:
        from openai import OpenAI
    except ImportError:
        raise OpenAIError("openai_package_not_installed")

    picked_model = (model or "").strip() or "gpt-4o-mini"
    client = OpenAI(api_key=resolved_key)
    try:
        response = client.chat.completions.create(
            model=picked_model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.7,
        )
    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate limit" in msg.lower() or "quota" in msg.lower():
            raise OpenAIError(f"rate_limited:30") from e
        if "401" in msg or "invalid api key" in msg.lower() or "incorrect api key" in msg.lower():
            raise OpenAIError("invalid_api_key") from e
        raise OpenAIError(f"openai_error:{msg[:120]}") from e

    text = (response.choices[0].message.content or "").strip() if response.choices else ""
    if not text:
        raise OpenAIError("empty_response")
    return OpenAIResult(text=text)
