"""Anthropic-backed :class:`~autoresearch.review.Completer`.

Isolated here so the review logic stays testable without an API key and the
`review` extra stays optional. Structured output is enforced with
``output_config.format``; a policy decline is surfaced as
:class:`RefusalError` rather than an empty result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 32000
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class RefusalError(RuntimeError):
    """The model declined the request (`stop_reason == "refusal"`)."""


class TruncatedError(RuntimeError):
    """The response hit max_tokens; the JSON body is incomplete."""


@dataclass
class AnthropicCompleter:
    """Calls Claude and returns the raw JSON text of a structured response."""

    api_key: str
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS

    def _client(self) -> Any:
        import anthropic  # imported lazily: the `review` extra is optional

        return anthropic.Anthropic(api_key=self.api_key)

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, got {self.effort!r}")
        # Streamed: thinking is on by default and shares max_tokens with the
        # response, so a long review can outlive a non-streaming timeout.
        with self._client().beta.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            # Safety classifiers can decline; route the retry server-side.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise RefusalError(f"model declined the review (category={category})")
        if response.stop_reason == "max_tokens":
            raise TruncatedError("review output hit max_tokens; raise max_tokens or lower effort")
        for block in response.content:
            if block.type == "text":
                return str(block.text)
        raise RuntimeError("no text block in response")
