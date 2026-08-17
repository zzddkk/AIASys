"""Tests for Auto-Nudge behavior with wrapped execution contract prompts."""

from __future__ import annotations

from typing import Any

from app.services.agent.runtime_backends.aiasys.session_stream import (
    SessionStreamMixin,
)
from app.services.history.session_history_projection import (
    unwrap_user_prompt,
    wrap_user_prompt,
)


class _FakeSessionStream(SessionStreamMixin):
    """Minimal concrete implementation for testing SessionStreamMixin."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._auto_nudge_enabled = True
        self._tools_used_since_user_message = False
        self._auto_nudge_sent_for_current_turn = False
        self._post_list_nudge_sent_for_current_turn = False
        self._last_tool_name = None


class TestAutoNudgeExecutionContract:
    """Verify that Auto-Nudge does not false-positive on simple greetings."""

    def test_get_last_user_text_unwraps_execution_contract(self) -> None:
        session = _FakeSessionStream()
        wrapped = wrap_user_prompt("你好")
        session.messages = [{"role": "user", "content": wrapped}]

        text = session._get_last_user_text()

        assert text == "你好"

    def test_looks_like_actionable_ignores_contract_verbs(self) -> None:
        session = _FakeSessionStream()
        wrapped = wrap_user_prompt("你好")
        session.messages = [{"role": "user", "content": wrapped}]

        text = session._get_last_user_text()
        assert not SessionStreamMixin._looks_like_actionable(text)

    def test_looks_like_actionable_detects_real_task(self) -> None:
        session = _FakeSessionStream()
        wrapped = wrap_user_prompt("请帮我修改 README.md")
        session.messages = [{"role": "user", "content": wrapped}]

        text = session._get_last_user_text()
        assert SessionStreamMixin._looks_like_actionable(text)

    def test_unwrap_user_prompt_plain_text(self) -> None:
        # unwrap_user_prompt returns None for plain text that doesn't match contract
        assert unwrap_user_prompt("你好") is None

    def test_unwrap_user_prompt_wrapped(self) -> None:
        wrapped = wrap_user_prompt("你好")
        assert unwrap_user_prompt(wrapped) == "你好"
