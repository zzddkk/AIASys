"""`message_protocol` 的归一化边界与三协议投影测试。

与既有两个文件的分工（避免重复覆盖）：
- `test_display_hint.py` 管 origin → display_hint 映射与事件投影；
- `test_aiasys_message_ir.py` 管 thinking / redacted_thinking 的签名三态与 finish_reason；
- 本文件管**剩下三块**：
  1. `normalize_internal_message` 的输入污染边界（非法 role/origin、类型错位、空白值）；
  2. 三个协议投影各自的结构契约（system 抽取、tool 形态、空消息取舍）；
  3. **内部字段不得泄露给 LLM** 这条跨协议不变量。

第 3 类是本文件的核心价值。`origin` / `turn_n` / `id` 是后端内部概念，
一旦有人把投影图省事写成 `{**message}`，这些键就会随请求发给服务商——
轻则被严格校验的端点拒绝，重则污染上下文。该不变量对三个协议同时成立，
因此用参数化对三者一起断言，而不是只测当前最常用的那个。
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from app.services.agent.runtime_backends.aiasys.llm_clients.message_protocol import (
    normalize_internal_message,
    to_anthropic_messages,
    to_openai_chat_messages,
    to_responses_input_messages,
)

# ---------------------------------------------------------------- 1. 归一化边界


@pytest.mark.parametrize("raw_role", ["developer", "function", "", None, 42, "USER"])
def test_unknown_role_is_coerced_to_user(raw_role: Any) -> None:
    """任何不在白名单内的 role 都降级为 user，绝不原样透传。

    大小写敏感是有意的："USER" 也算未知值——协议里的角色是字面量，
    放宽大小写会让下游分支判断出现两种写法。
    """
    assert normalize_internal_message({"role": raw_role})["role"] == "user"


@pytest.mark.parametrize("role", ["system", "assistant", "tool", "user"])
def test_valid_roles_are_preserved(role: str) -> None:
    assert normalize_internal_message({"role": role})["role"] == role


def test_none_content_becomes_empty_string() -> None:
    """content 缺失或为 None 时补成空串，下游投影不必再判空。"""
    assert normalize_internal_message({"role": "user"})["content"] == ""
    assert normalize_internal_message({"role": "user", "content": None})["content"] == ""


@pytest.mark.parametrize("field", ["id", "tool_call_id"])
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_identifiers_are_dropped_not_kept_as_empty(field: str, blank: str) -> None:
    """空白 id 必须整键丢弃，而不是留一个空串。

    留空串会让下游 `message.get("id", default)` 拿到 "" 而非 default，
    静默走进「有 id 但 id 无效」的中间态。
    """
    assert field not in normalize_internal_message({"role": "tool", field: blank})


def test_identifiers_are_stripped() -> None:
    normalized = normalize_internal_message(
        {"role": "tool", "id": "  m1  ", "tool_call_id": "  call_1  "}
    )
    assert normalized["id"] == "m1"
    assert normalized["tool_call_id"] == "call_1"


@pytest.mark.parametrize(
    "origin",
    [
        "user",
        "assistant",
        "tool",
        "system",
        "compaction_summary",
        "system_notice",
        "contextual_user",
        "forked",
    ],
)
def test_all_declared_origins_survive_normalization(origin: str) -> None:
    """八个合法 origin 必须原样保留。

    这条与 display_hint 的映射表互为对照：映射表有键但归一化丢掉它，
    等于该 origin 永远走不到映射表，前端拿到的永远是兜底值。
    """
    assert normalize_internal_message({"role": "user", "origin": origin})["origin"] == origin


@pytest.mark.parametrize("origin", ["hacker", "", None, "System", 7, "contextual-user"])
def test_unrecognized_origin_is_dropped(origin: Any) -> None:
    """未登记的 origin 整键丢弃，不进入内部消息。

    这是防泄露的第一道闸：未知 origin 若被保留，`compute_display_hint`
    会按「宁可多显示」兜底成 visible，于是一条本该隐藏的注入消息
    可能因为 origin 拼错（如 "contextual-user" 写成连字符）而显示到界面上。
    在归一化处丢掉，能让这类拼写错误退化为「按缺省 user 处理」而非「按可见处理」。
    """
    assert "origin" not in normalize_internal_message({"role": "user", "origin": origin})


@pytest.mark.parametrize("turn_n", ["3", 3.0, None, [3]])
def test_non_int_turn_n_is_dropped(turn_n: Any) -> None:
    assert "turn_n" not in normalize_internal_message({"role": "user", "turn_n": turn_n})


def test_bool_turn_n_is_accepted_as_int_documenting_current_behavior() -> None:
    """bool 是 int 的子类，因此 turn_n=True 会被当成 1 保留。

    这不是刻意设计，而是 `isinstance(x, int)` 的既有语义。此处显式钉住现状：
    若将来改为拒绝 bool，本用例会失败并提醒同步更新契约文档，
    而不是让一个静默的行为变更漂过去。
    """
    assert normalize_internal_message({"role": "user", "turn_n": True})["turn_n"] is True


@pytest.mark.parametrize("raw", ["not-a-list", {"a": 1}, None, 5])
def test_non_list_tool_calls_yields_no_key(raw: Any) -> None:
    assert "tool_calls" not in normalize_internal_message({"role": "assistant", "tool_calls": raw})


def test_non_dict_tool_call_items_are_skipped() -> None:
    """列表里混入非 dict 项时跳过该项，保留其余合法项，不整体放弃。"""
    normalized = normalize_internal_message(
        {
            "role": "assistant",
            "tool_calls": ["junk", None, {"id": "c1", "function": {"name": "read"}}],
        }
    )
    tool_calls = normalized["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "c1"


def test_malformed_function_field_degrades_to_empty_names() -> None:
    """function 不是 dict 时按空 dict 处理，产出结构完整但字段为空的调用。"""
    normalized = normalize_internal_message(
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": "oops"}]}
    )
    call = normalized["tool_calls"][0]
    assert call["function"]["name"] == ""
    assert call["function"]["arguments"] == "{}"


def test_tool_call_defaults_type_to_function() -> None:
    normalized = normalize_internal_message(
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "ls"}}]}
    )
    assert normalized["tool_calls"][0]["type"] == "function"


def test_dict_arguments_are_serialized_preserving_non_ascii() -> None:
    """dict 形态的 arguments 序列化为 JSON 文本，且不转义非 ASCII。

    `ensure_ascii=False` 不只是美观：转义后中文路径参数的字节长度会膨胀，
    对按字符计费/限长的端点是实际差异。
    """
    normalized = normalize_internal_message(
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "w", "arguments": {"p": "笔记"}}}],
        }
    )
    assert normalized["tool_calls"][0]["function"]["arguments"] == '{"p": "笔记"}'


@pytest.mark.parametrize("empty", [None, "", {}, []])
def test_empty_arguments_become_empty_json_object(empty: Any) -> None:
    """空 arguments 统一成 "{}"，而非空串。

    空串会让服务商侧的 JSON 解析报错，"{}" 是「无参数」的合法表达。
    """
    normalized = normalize_internal_message(
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"arguments": empty}}]}
    )
    assert normalized["tool_calls"][0]["function"]["arguments"] == "{}"


def test_string_arguments_pass_through_unparsed() -> None:
    """已是字符串的 arguments 原样保留，包括非法 JSON。

    归一化层不做 JSON 合法性校验——模型吐出的残缺 JSON 需要在更上层
    按 finish_reason 判断是截断还是真错误，此处提前吞掉会丢失线索。
    """
    normalized = normalize_internal_message(
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"arguments": '{"a":'}}]}
    )
    assert normalized["tool_calls"][0]["function"]["arguments"] == '{"a":'


def test_reasoning_content_key_presence_is_preserved_even_when_none() -> None:
    """`reasoning_content` 按「键是否存在」传递，值为 None 也保留该键。

    实现用的是 `if "reasoning_content" in message` 而非真值判断，
    两者对 None 的处理不同。钉住这点：区分「没有推理」与「推理为空」
    在排查 think 内容丢失时是关键线索。
    """
    normalized = normalize_internal_message({"role": "assistant", "reasoning_content": None})
    assert "reasoning_content" in normalized
    assert normalized["reasoning_content"] is None
    assert "reasoning_content" not in normalize_internal_message({"role": "assistant"})


# ------------------------------------------------------- 2. 各协议的结构契约


def test_openai_tool_message_carries_tool_call_id_and_drops_other_keys() -> None:
    converted = to_openai_chat_messages(
        [{"role": "tool", "tool_call_id": "call_1", "content": "ok", "origin": "tool"}]
    )
    assert converted == [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]


def test_openai_tool_message_without_id_uses_empty_string() -> None:
    """缺 tool_call_id 时补空串而不是抛异常。

    上游已尽力保证配对，这里保持宽容：一条配不上的 tool 结果由服务商报错，
    比在本地抛异常中断整轮更容易定位。
    """
    converted = to_openai_chat_messages([{"role": "tool", "content": "ok"}])
    assert converted[0]["tool_call_id"] == ""


def test_openai_projects_tool_calls_only_for_assistant() -> None:
    """只有 assistant 的 tool_calls 被投影；user 携带的 tool_calls 不透传。

    user 消息带 tool_calls 属于非法输入（多为上游拼装错误）。静默丢弃而非报错，
    但必须真的不出现在请求里——否则服务商会拒绝整个请求。
    """
    payload = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "ls"}}]},
        {"role": "user", "tool_calls": [{"id": "c2", "function": {"name": "rm"}}]},
    ]
    converted = to_openai_chat_messages(payload)
    assert "tool_calls" in converted[0]
    assert "tool_calls" not in converted[1]


def test_openai_passes_reasoning_content_through() -> None:
    converted = to_openai_chat_messages(
        [{"role": "assistant", "content": "answer", "reasoning_content": "why"}]
    )
    assert converted[0]["reasoning_content"] == "why"


def test_anthropic_extracts_system_out_of_message_list() -> None:
    """system 必须被抽成独立的 system 参数，且不残留在 messages 中。

    Anthropic 的 Messages API 不接受 role=system 的消息；残留会整请求失败。
    """
    system, messages = to_anthropic_messages(
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    )
    assert system == "be brief"
    assert [m["role"] for m in messages] == ["user"]


def test_anthropic_joins_multiple_systems_with_blank_line() -> None:
    """多条 system 按出现顺序用空行拼接，不是覆盖也不是只取第一条。"""
    system, _ = to_anthropic_messages(
        [
            {"role": "system", "content": "rule A"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "rule B"},
        ]
    )
    assert system == "rule A\n\nrule B"


@pytest.mark.parametrize("blank", ["", "   ", "\n\n"])
def test_anthropic_blank_system_yields_none_not_empty_string(blank: str) -> None:
    """空白 system 返回 None，让调用方能省略该参数。

    传空串给端点与不传是两种语义，某些兼容层对空串 system 会报错。
    """
    system, _ = to_anthropic_messages([{"role": "system", "content": blank}])
    assert system is None


def test_anthropic_tool_result_is_wrapped_in_user_message() -> None:
    """tool 结果在 Anthropic 协议里是 user 消息内的 tool_result 块。"""
    _, messages = to_anthropic_messages(
        [{"role": "tool", "tool_call_id": "call_1", "content": "42"}]
    )
    assert messages == [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "42"}],
        }
    ]


def test_anthropic_thinking_block_precedes_text_block() -> None:
    """thinking 块必须排在 text 块之前。

    顺序不是风格问题：Anthropic 要求 assistant 轮内 thinking 先于其他块，
    顺序错了会被拒。既有测试覆盖了「是否带签名」，此处覆盖「排在哪」。
    """
    _, messages = to_anthropic_messages(
        [
            {
                "role": "assistant",
                "content": "final",
                "reasoning_content": "step",
                "reasoning_signature": "sig",
            }
        ]
    )
    assert [block["type"] for block in messages[0]["content"]] == ["thinking", "text"]


def test_anthropic_empty_assistant_content_produces_no_text_block() -> None:
    """空 content 不产出空 text 块——空块会被端点拒绝。"""
    _, messages = to_anthropic_messages([{"role": "assistant", "content": ""}])
    assert messages[0]["content"] == []


def test_responses_drops_tool_call_message_with_empty_content() -> None:
    """Responses 协议下，assistant 的纯工具调用消息（无文本）被整条丢弃。

    该协议的工具调用走独立的 function_call 项而非消息体，
    投影出一条 content 为空的 assistant 消息只会被拒。
    """
    converted = to_responses_input_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {}}]},
        ]
    )
    assert [m["role"] for m in converted] == ["user"]


def test_responses_keeps_text_of_tool_call_message_when_present() -> None:
    """同一条消息既有文本又有工具调用时，保留文本部分。"""
    converted = to_responses_input_messages(
        [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [{"id": "c", "function": {}}],
            }
        ]
    )
    assert converted == [{"role": "assistant", "content": "let me check"}]


def test_responses_tool_message_carries_tool_call_id() -> None:
    converted = to_responses_input_messages(
        [{"role": "tool", "tool_call_id": "call_9", "content": "done"}]
    )
    assert converted == [{"role": "tool", "content": "done", "tool_call_id": "call_9"}]


# ------------------------------------- 3. 跨协议不变量：内部字段不得外泄

# 一条塞满了内部字段的消息。任何投影都不该把这些键带给服务商。
_MESSAGE_WITH_INTERNAL_FIELDS: dict[str, Any] = {
    "role": "assistant",
    "id": "msg_internal_1",
    "content": "visible answer",
    "origin": "contextual_user",
    "turn_n": 7,
    "display_hint": "collapsed",
    "reasoning_content": "hidden reasoning",
    "reasoning_signature": "sig",
    "reasoning_redacted_data": "redacted",
}

_INTERNAL_ONLY_KEYS = ("origin", "turn_n", "display_hint", "id")


def _openai_projection() -> list[dict[str, Any]]:
    return to_openai_chat_messages([_MESSAGE_WITH_INTERNAL_FIELDS])


def _anthropic_projection() -> list[dict[str, Any]]:
    return to_anthropic_messages([_MESSAGE_WITH_INTERNAL_FIELDS])[1]


def _responses_projection() -> list[dict[str, Any]]:
    return to_responses_input_messages([_MESSAGE_WITH_INTERNAL_FIELDS])


@pytest.mark.parametrize(
    "project",
    [_openai_projection, _anthropic_projection, _responses_projection],
    ids=["openai", "anthropic", "responses"],
)
@pytest.mark.parametrize("leaked_key", _INTERNAL_ONLY_KEYS)
def test_internal_only_fields_never_reach_provider_payload(
    project: Callable[[], list[dict[str, Any]]],
    leaked_key: str,
) -> None:
    """三协议 × 四个内部键：投影结果的顶层不得出现任一内部键。

    这是本文件的核心断言。它锁住的不是某个已知 bug，而是一类偷懒写法：
    把投影实现成 `{**message}` 再删几个键。那种写法今天可能恰好正确，
    但每新增一个内部字段就默默多泄一个——本用例会立刻失败。
    """
    for converted in project():
        assert leaked_key not in converted, f"{leaked_key} 泄露到 provider payload: {converted}"


@pytest.mark.parametrize(
    "project",
    [_openai_projection, _anthropic_projection, _responses_projection],
    ids=["openai", "anthropic", "responses"],
)
def test_projection_still_carries_the_actual_content(
    project: Callable[[], list[dict[str, Any]]],
) -> None:
    """配套的正向断言：上一条只证明「没多带」，本条证明「没漏带」。

    只测不泄露的话，一个返回空列表的坏实现也能通过。两条一起才构成有效约束。
    """
    serialized = repr(project())
    assert "visible answer" in serialized
