"""显示流分层：origin → display_hint 映射与 SSE 出口集成测试。"""

from __future__ import annotations

import pytest

from app.services.agent.mixins.events import EventMixin
from app.services.agent.runtime_backends.aiasys.llm_clients.message_protocol import (
    compute_display_hint,
    to_anthropic_messages,
)
from app.services.agent.runtime_backends.aiasys.session_stream import _TurnBegin
from app.services.agent.runtime_backends.base import AgentRuntimeEvent


class _FakeAgentService(EventMixin):
    """最小可实例化 EventMixin 的桩服务，用于隔离测试 projection 逻辑。"""

    pass


_SVC = _FakeAgentService()


# ---------------------------------------------------------------------------
# 1. 纯函数映射表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("user", "visible"),
        ("assistant", "visible"),
        ("tool", "visible"),
        ("system", "hidden"),
        ("compaction_summary", "hidden"),
        ("system_notice", "visible"),
        ("contextual_user", "collapsed"),
        ("forked", "collapsed"),
    ],
)
def test_compute_display_hint_known_origins(origin: str, expected: str) -> None:
    assert compute_display_hint(origin) == expected


def test_compute_display_hint_unknown_origin_falls_back_to_visible() -> None:
    assert compute_display_hint("some_new_origin") == "visible"


def test_compute_display_hint_none_origin_returns_visible() -> None:
    assert compute_display_hint(None) == "visible"


# ---------------------------------------------------------------------------
# 2. SSE 投影层集成：display_hint 透传 + turn_begin 继承
# ---------------------------------------------------------------------------


def test_project_content_event_carries_display_hint() -> None:
    item = AgentRuntimeEvent(
        kind="content", content_type="text", text="hello", display_hint="collapsed"
    )
    state = _SVC._new_event_projection_state()
    events = _SVC._project_output_item(item, state)
    # host 事件会自动触发 turn_begin
    assert len(events) == 2
    assert events[0]["type"] == "turn_begin"
    assert events[0]["display_hint"] == "collapsed"
    assert events[1]["type"] == "content"
    assert events[1]["display_hint"] == "collapsed"


def test_project_tool_result_event_carries_display_hint() -> None:
    item = AgentRuntimeEvent(
        kind="tool_result",
        tool_call_id="call_1",
        tool_name="read",
        content="ok",
        display_hint="visible",
    )
    state = _SVC._new_event_projection_state()
    events = _SVC._project_output_item(item, state)
    assert len(events) == 2
    assert events[0]["type"] == "turn_begin"
    assert events[0]["display_hint"] == "visible"
    assert events[1]["type"] == "tool_result"
    assert events[1]["display_hint"] == "visible"


# ---------------------------------------------------------------------------
# 3. 关键：混合序列不串味
# ---------------------------------------------------------------------------
# 验证 state["display_hint"] 被当前 item 覆盖后，
# 下一个 turn_begin 继承新值，而非残留旧值。


def test_mixed_sequence_display_hint_does_not_leak() -> None:
    state = _SVC._new_event_projection_state()

    # --- turn 1: collapsed ---
    item_collapsed = AgentRuntimeEvent(
        kind="content", content_type="text", text="persistent context", display_hint="collapsed"
    )
    events = _SVC._project_output_item(item_collapsed, state)
    assert events[0]["display_hint"] == "collapsed"

    turn_begin_1 = _SVC._project_output_item(_TurnBegin(), state)
    assert turn_begin_1[0]["type"] == "turn_begin"
    assert turn_begin_1[0]["display_hint"] == "collapsed"  # 继承上一条

    # --- turn 2: hidden（压缩摘要） ---
    item_hidden = AgentRuntimeEvent(
        kind="content", content_type="text", text="summary", display_hint="hidden"
    )
    events = _SVC._project_output_item(item_hidden, state)
    assert events[0]["display_hint"] == "hidden"

    turn_begin_2 = _SVC._project_output_item(_TurnBegin(), state)
    assert turn_begin_2[0]["type"] == "turn_begin"
    assert turn_begin_2[0]["display_hint"] == "hidden"  # 必须切换到 hidden，不能还是 collapsed

    # --- turn 3: 回到 visible ---
    item_visible = AgentRuntimeEvent(
        kind="content", content_type="text", text="normal reply", display_hint="visible"
    )
    events = _SVC._project_output_item(item_visible, state)
    assert events[0]["display_hint"] == "visible"

    turn_begin_3 = _SVC._project_output_item(_TurnBegin(), state)
    assert turn_begin_3[0]["type"] == "turn_begin"
    assert turn_begin_3[0]["display_hint"] == "visible"


# ---------------------------------------------------------------------------
# 4. 模型调用路径不受影响
# ---------------------------------------------------------------------------


def test_to_anthropic_messages_does_not_consume_display_hint() -> None:
    """证明 to_anthropic_messages 不消费 display_hint，且 origin 白名单剥离逻辑未变。"""
    messages = [
        {"role": "user", "content": "hi", "origin": "user", "display_hint": "visible"},
        {"role": "assistant", "content": "hello", "origin": "assistant", "display_hint": "visible"},
        {"role": "system", "content": "sys", "origin": "system", "display_hint": "hidden"},
    ]
    system_prompt, anthropic_messages = to_anthropic_messages(messages)
    # system prompt 被正确抽离
    assert system_prompt == "sys"
    # 输出消息里没有 display_hint 字段（模型路径不关心它）
    for msg in anthropic_messages:
        assert "display_hint" not in msg
    #  roles 转换正常
    assert anthropic_messages[0]["role"] == "user"
    assert anthropic_messages[1]["role"] == "assistant"
