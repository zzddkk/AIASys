"""会话列表「最后一问」预览的看守测试。

设计依据：参考资料/前端全域借鉴清单.md P0-1（kimi 卡片式会话列表带最后一问预览）。
看守点：
1. 取最后一条真实用户消息，不是第一条；
2. 系统注入（origin=system_notice / contextual_user）不算用户消息；
3. 执行契约包装被剥掉，预览是用户原话；
4. 超长截断；
5. 会话不存在时返回 None，不抛异常（预览是增强不是必需）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.workspace_registry import WorkspaceRegistryService


class _FakeSessionManager:
    def __init__(self, history: list[dict[str, Any]] | Exception) -> None:
        self._history = history

    def get_history(self, session_id: str, user_id: str) -> list[dict[str, Any]]:
        if isinstance(self._history, Exception):
            raise self._history
        return self._history


def _preview(tmp_path, history, **kwargs) -> str | None:
    service = WorkspaceRegistryService.__new__(WorkspaceRegistryService)
    service.session_manager = _FakeSessionManager(history)
    return service._build_last_user_preview("s1", "u1", **kwargs)


class TestLastUserPreview:
    def test_takes_last_user_message_not_first(self, tmp_path):
        history = [
            {"role": "user", "content": "第一个问题", "origin": "user"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "第二个问题", "origin": "user"},
            {"role": "assistant", "content": "回答二"},
        ]
        assert _preview(tmp_path, history) == "第二个问题"

    def test_skips_system_injected_messages(self, tmp_path):
        history = [
            {"role": "user", "content": "真实提问", "origin": "user"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "系统注入的诱导跳出", "origin": "system_notice"},
            {"role": "user", "content": "上下文注入", "origin": "contextual_user"},
        ]
        assert _preview(tmp_path, history) == "真实提问"

    def test_skips_compaction_summary(self, tmp_path):
        history = [
            {"role": "user", "content": "真实提问", "origin": "user"},
            {"role": "user", "content": "压缩摘要正文", "origin": "compaction_summary"},
        ]
        assert _preview(tmp_path, history) == "真实提问"

    def test_unwraps_execution_contract(self, tmp_path):
        from app.services.history.session_history_projection import wrap_user_prompt

        wrapped = wrap_user_prompt("帮我读一下这个文件")
        history = [{"role": "user", "content": wrapped, "origin": "user"}]
        preview = _preview(tmp_path, history)
        assert preview == "帮我读一下这个文件"
        assert "执行契约" not in (preview or "")

    def test_truncates_long_text(self, tmp_path):
        history = [{"role": "user", "content": "问" * 200, "origin": "user"}]
        preview = _preview(tmp_path, history, max_chars=20)
        assert preview is not None
        assert len(preview) == 20
        assert preview.endswith("…")

    def test_takes_first_line_only(self, tmp_path):
        history = [
            {"role": "user", "content": "第一行问题\n第二行细节\n第三行", "origin": "user"},
        ]
        assert _preview(tmp_path, history) == "第一行问题"

    def test_prefers_display_content(self, tmp_path):
        history = [
            {
                "role": "user",
                "content": "[执行契约]...包装后的内容",
                "display_content": "用户看到的原话",
                "origin": "user",
            },
        ]
        assert _preview(tmp_path, history) == "用户看到的原话"

    def test_no_user_message_returns_none(self, tmp_path):
        history = [{"role": "assistant", "content": "只有助手消息"}]
        assert _preview(tmp_path, history) is None

    def test_history_failure_returns_none(self, tmp_path):
        assert _preview(tmp_path, RuntimeError("会话不存在")) is None

    def test_probe_origin_filter_matters(self, tmp_path):
        """探针：不过滤 origin 时会取到系统注入内容（看守不是恒绿）。"""
        history = [
            {"role": "user", "content": "真实提问", "origin": "user"},
            {"role": "user", "content": "系统注入内容", "origin": "system_notice"},
        ]
        # 缺陷版本：只看 role 不看 origin，会取到最后那条系统注入
        broken = next(m for m in reversed(history) if m.get("role") == "user")
        assert broken["content"] == "系统注入内容"
        # 正确版本过滤掉它
        assert _preview(tmp_path, history) == "真实提问"


@pytest.mark.parametrize("origin", ["user", "forked", None])
def test_accepted_origins(tmp_path, origin):
    message: dict[str, Any] = {"role": "user", "content": "提问内容"}
    if origin is not None:
        message["origin"] = origin
    assert _preview(tmp_path, [message]) == "提问内容"
