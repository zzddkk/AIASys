"""A1 双写看守测试：wire 事件日志（message.append）与投影一致性。

设计文档：AIASys-product-design/交互设计/session-event-log-design.md
看守目标：
1. _append_message 双写后，wire 事件流投影出的消息序列与内存消息一致
   （逐字段），这是 A2 切读路径前的防回退闸门；
2. reasoning_content / origin / turn_n 等关键字段不丢；
3. system 角色不落事件流；
4. 读取容忍末行截断（进程死在一半的场景）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agent.runtime_backends.aiasys.session import AiasysRuntimeSession
from app.services.history.wire_event_log import (
    append_wire_event,
    project_messages_from_events,
    read_wire_events,
    wire_log_path,
)


def _make_bare_session(tmp_path, session_id: str = "s1") -> AiasysRuntimeSession:
    """绕过 __init__ 构造最小可用实例，只满足 _append_message 的依赖。"""
    session = AiasysRuntimeSession.__new__(AiasysRuntimeSession)
    session._spec = SimpleNamespace(session_dir=tmp_path)
    session.session_id = session_id
    session.messages = []
    session._current_turn_n = None
    session._current_display_hint = None
    session._pending_token_estimate = 0
    return session


class TestMessageAppendDualWrite:
    def test_projection_matches_in_memory_messages(self, tmp_path):
        session = _make_bare_session(tmp_path)
        session._current_turn_n = 1
        session._append_message({"role": "user", "content": "查一下这个文件"})
        session._append_message(
            {
                "role": "assistant",
                "content": "好的，我先读取文件。",
                "reasoning_content": "用户要查文件，应该先读再答。",
            }
        )
        session._append_message(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
                "reasoning_content": "需要调用读取工具。",
            }
        )
        session._append_message({"role": "tool", "tool_call_id": "tc1", "content": "文件内容"})

        events = read_wire_events(wire_log_path(tmp_path, "s1"))
        projected = project_messages_from_events(events)

        assert len(projected) == len(session.messages) == 4
        for expected, actual in zip(session.messages, projected):
            assert actual == expected

    def test_reasoning_origin_turn_n_preserved(self, tmp_path):
        session = _make_bare_session(tmp_path)
        session._current_turn_n = 3
        session._append_message(
            {
                "role": "assistant",
                "content": "正文",
                "reasoning_content": "思考内容",
                "origin": "assistant",
            }
        )
        events = read_wire_events(wire_log_path(tmp_path, "s1"))
        (msg_event,) = [e for e in events if e["type"] == "message.append"]
        payload = msg_event["message"]
        assert payload["reasoning_content"] == "思考内容"
        assert payload["origin"] == "assistant"
        assert payload["turn_n"] == 3

    def test_system_role_not_logged(self, tmp_path):
        session = _make_bare_session(tmp_path)
        session._append_message({"role": "system", "content": "你是助手"})
        session._append_message({"role": "user", "content": "你好"})
        events = read_wire_events(wire_log_path(tmp_path, "s1"))
        roles = [e["message"]["role"] for e in events if e["type"] == "message.append"]
        assert roles == ["user"]

    def test_no_session_dir_no_write(self, tmp_path):
        session = _make_bare_session(tmp_path)
        session._spec.session_dir = None
        session._append_message({"role": "user", "content": "不应落盘"})
        assert not wire_log_path(tmp_path, "s1").exists()

    def test_write_failure_does_not_raise(self, tmp_path):
        session = _make_bare_session(tmp_path)
        # session_dir 指向一个已存在的文件，mkdir 必失败
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        session._spec.session_dir = blocker / "impossible"
        session._append_message({"role": "user", "content": "写不进去也不能炸"})
        assert session.messages[-1]["content"] == "写不进去也不能炸"


class TestReadToleratesTruncation:
    def test_truncated_last_line_skipped(self, tmp_path):
        wire_file = wire_log_path(tmp_path, "s2")
        append_wire_event(
            tmp_path,
            "s2",
            {"type": "message.append", "message": {"role": "user", "content": "好行"}},
        )
        with open(wire_file, "a", encoding="utf-8") as f:
            f.write('{"type": "message.append", "message": {"role": "user", "con')
        events = read_wire_events(wire_file)
        assert len(events) == 1
        assert events[0]["message"]["content"] == "好行"

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_wire_events(wire_log_path(tmp_path, "ghost")) == []


class TestProbeGuard:
    """探针：投影故意丢掉 reasoning_content 时，一致性测试必须红。

    本测试自身验证「看守不是恒绿」：把投影函数换成丢字段的版本，
    逐字段断言必须失败——证明上面的对比测试真的在检查 reasoning_content。
    """

    def test_broken_projection_fails_comparison(self, tmp_path):
        session = _make_bare_session(tmp_path)
        session._append_message(
            {"role": "assistant", "content": "正文", "reasoning_content": "思考"}
        )
        events = read_wire_events(wire_log_path(tmp_path, "s1"))
        projected = project_messages_from_events(events)
        # 模拟投影丢字段的缺陷
        broken = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in projected]
        with pytest.raises(AssertionError):
            for expected, actual in zip(session.messages, broken):
                assert actual == expected
