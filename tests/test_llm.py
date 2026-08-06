"""Error mapping in the Anthropic completer (no network, no key)."""

from __future__ import annotations

import pytest

anthropic = pytest.importorskip("anthropic")

from autoresearch.llm import AnthropicCompleter, CompleterError  # noqa: E402


def test_api_errors_map_to_completer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth/network/rate-limit failures must surface as an expected failure type,
    not crash the advisory entry point."""

    class FailingClient:
        @property
        def beta(self) -> object:
            raise anthropic.AnthropicError("boom: connection reset")

    monkeypatch.setattr(AnthropicCompleter, "_client", lambda self: FailingClient())
    with pytest.raises(CompleterError, match="boom"):
        AnthropicCompleter(api_key="k").complete("s", "p", {})


def test_unknown_effort_is_rejected_before_any_api_call() -> None:
    with pytest.raises(ValueError, match="effort"):
        AnthropicCompleter(api_key="k", effort="ultra").complete("s", "p", {})


def test_mid_stream_transport_errors_also_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx errors escape the SDK's own wrapping mid-stream; they must still
    become an expected failure."""
    import httpx

    class FailingClient:
        @property
        def beta(self) -> object:
            raise httpx.ReadError("connection reset mid-stream")

    monkeypatch.setattr(AnthropicCompleter, "_client", lambda self: FailingClient())
    with pytest.raises(CompleterError, match="ReadError"):
        AnthropicCompleter(api_key="k").complete("s", "p", {})
