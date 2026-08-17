"""SessionStreamMixin — ReAct prompt 流式循环。

从 AiasysRuntimeSession 中提取，保持核心类精简。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.tool_result import ToolResult
from app.services.agent.authorization import (
    AuthorizationMode,
    CapabilityAuthorizationRequest,
    CapabilityAuthorizationService,
)
from app.services.agent.errors import RunCancelled
from app.services.agent.message_content import (
    downgrade_message_content_for_history,
    extract_message_text,
    hydrate_message_images,
)

# Per-Agent Write Allow Root 守卫（team_spawn build 类任务）
from app.services.agent.runtime_backends.aiasys.team.store import check_write_guard
from app.services.history.session_history_projection import unwrap_user_prompt

from ..base import AgentRuntimeEvent
from .llm_clients.error_classifier import classify_api_error
from .llm_clients.message_protocol import DisplayHint
from .llm_clients.retry_utils import jittered_backoff
from .loop_detection import (
    THINKING_LOOP_NUDGE,
    ContinuationVerdict,
    ThinkingLoopVerdict,
    advance_continuation,
    check_continuation_safety,
    describe_continuation_stop,
)
from .session_utils import (
    extract_usage_counts,
    merge_stream_fragment,
    normalize_capabilities,
    read_config_value,
)

logger = logging.getLogger(__name__)


@dataclass
class _TurnBegin:
    """ReAct 循环新一轮开始的标记，供后端事件投影生成 turn_begin SSE 事件。"""

    type: str = "turn_begin"
    kind: str = "turn_begin"


def _serialize_tool_content_for_event(content: str | list[dict[str, Any]]) -> str:
    """把 tool result 的结构化 content 序列化为事件展示字符串。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif item_type == "image_url":
            image_url = item.get("image_url", {})
            url = image_url.get("url") if isinstance(image_url, dict) else str(image_url)
            url = url.removeprefix("file://")
            parts.append(f"![image]({url})")
    return "".join(parts)


class SessionStreamMixin:
    """提供 prompt() ReAct 流式循环，作为 mixin 混入 AiasysRuntimeSession。"""

    # 显示流分层：直接实例化 SessionStreamMixin 的测试桩需要此缺省值。
    # 用 DisplayHint 而非 str，理由同 session.py 的实例属性声明：str 会让下游
    # 所有 AgentRuntimeEvent(display_hint=...) 失去 Literal 校验。
    _current_display_hint: DisplayHint = "visible"

    def _prepare_messages_for_current_model(self) -> list[dict[str, Any]]:
        # Tier 1: 每次 LLM 调用前执行零成本 tool 结果清理
        self._run_pre_turn_clearing()
        messages = self._messages_for_model()
        capabilities = normalize_capabilities(
            read_config_value(getattr(self, "_model_config", None), "capabilities")
        )
        if "image_in" in capabilities:
            return hydrate_message_images(
                messages,
                workspace_dir=Path(str(self._spec.work_dir)),
            )

        prepared: list[dict[str, Any]] = []
        for message in messages:
            downgraded_content = downgrade_message_content_for_history(message.get("content"))
            if downgraded_content == message.get("content"):
                prepared.append(message)
                continue

            updated_message = dict(message)
            updated_message["content"] = downgraded_content
            prepared.append(updated_message)
        return prepared

    # ------------------------------------------------------------------
    # 工具调用辅助方法（授权 / 执行 / 收尾）
    # ------------------------------------------------------------------

    async def _authorize_single_tool(
        self,
        item: dict[str, Any],
        tool_ctx: dict[str, Any],
    ) -> tuple[bool, list[AgentRuntimeEvent], dict[str, Any] | None]:
        """对单个工具做循环检测、参数校验和授权决策。

        返回 (approved, events_to_yield, exec_info)。
        approved=False 时 events 已包含 tool_result 等事件，调用方只需 yield。
        """
        events: list[AgentRuntimeEvent] = []
        self._last_tool_name = item["function"]["name"]
        events.append(
            AgentRuntimeEvent(
                kind="tool_call",
                tool_call_id=item["id"],
                tool_name=item["function"]["name"],
                arguments=item["arguments"],
            )
        )

        # 循环检测
        loop_warning = self._check_loop_detection(
            item["function"]["name"],
            item["arguments"],
        )
        if loop_warning:
            events.append(AgentRuntimeEvent(kind="system_warning", text=loop_warning))
            tool_result = ToolResult(content=loop_warning, is_error=True)
            events.append(
                AgentRuntimeEvent(
                    kind="tool_result",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    content=_serialize_tool_content_for_event(tool_result.content),
                    is_error=tool_result.is_error,
                )
            )
            self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": item["id"],
                    "content": tool_result.content,
                }
            )
            return False, events, None

        # 参数解析错误
        parse_error = item.get("_parse_error")
        if parse_error:
            tool_result = ToolResult(content=parse_error, is_error=True)
            events.append(
                AgentRuntimeEvent(
                    kind="tool_result",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    content=_serialize_tool_content_for_event(tool_result.content),
                    is_error=tool_result.is_error,
                )
            )
            self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": item["id"],
                    "content": tool_result.content,
                }
            )
            return False, events, None

        # 能力授权决策
        resolved_tool_name = self._tool_registry._aliases.get(
            item["function"]["name"], item["function"]["name"]
        )
        tool = self._tool_registry._tools.get(resolved_tool_name)

        tool_risk = getattr(tool, "risk_level", "medium") if tool is not None else "medium"
        tool_scope = getattr(tool, "effect_scope", "workspace") if tool is not None else "workspace"
        tool_side_effect = getattr(tool, "side_effect", True) if tool is not None else True

        auth_mode_str = self._spec.authorization_mode
        if self._spec.yolo and auth_mode_str == "smart":
            auth_mode_str = "full_auto"

        skill_security: dict[str, Any] = {}
        if resolved_tool_name in ("EnableSkill", "DisableSkill"):
            skill_name = item["arguments"].get("name", "")
            if skill_name:
                skill_security = self._get_skill_security(skill_name)

        plan_state = self._get_plan_state()
        plan_mode_active = plan_state is not None and plan_state.mode == "active"
        plan_file_path = self._get_plan_file_path()

        auth_request = CapabilityAuthorizationRequest(
            tool_name=resolved_tool_name or item["function"]["name"],
            arguments=item["arguments"],
            risk_level=tool_risk,
            effect_scope=tool_scope,
            side_effect=tool_side_effect,
            authorization_mode=AuthorizationMode(auth_mode_str),
            is_subagent=self._spec.is_subagent,
            skill_security=skill_security,
            plan_mode_active=plan_mode_active,
            plan_file_path=plan_file_path,
        )
        auth_result = CapabilityAuthorizationService.decide(auth_request)

        if auth_result.decision in ("ask",):
            events.append(
                AgentRuntimeEvent(
                    kind="approval_required",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    arguments=item["arguments"],
                )
            )
            events.append(
                AgentRuntimeEvent(
                    kind="capability_confirmation",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    arguments=item["arguments"],
                    content=auth_result.confirmation_prompt
                    or f"是否允许执行工具 {item['function']['name']}？",
                )
            )
            approved, feedback = await self._confirmation_manager.wait_for_confirmation(
                tool_call_id=item["id"],
                tool_name=item["function"]["name"],
                arguments=item["arguments"],
                prompt=auth_result.confirmation_prompt
                or f"是否允许执行工具 {item['function']['name']}？",
                pattern_key=auth_result.pattern_key,
                subagent_name=getattr(self._spec, "subagent_name", None),
                agent_id=getattr(self._spec, "agent_id", None),
            )
            if not approved:
                denial_msg = feedback or "操作被拒绝"
                tool_result = ToolResult(content=denial_msg, is_error=True)
                events.append(
                    AgentRuntimeEvent(
                        kind="tool_result",
                        tool_call_id=item["id"],
                        tool_name=item["function"]["name"],
                        content=_serialize_tool_content_for_event(tool_result.content),
                        is_error=tool_result.is_error,
                    )
                )
                self._append_message(
                    {
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": tool_result.content,
                    }
                )
                return False, events, None

        if auth_result.decision in ("deny", "block"):
            denial_msg = (
                auth_result.denial_message
                or f"工具 {item['function']['name']} 已被系统拦截：{auth_result.reason}"
            )
            tool_result = ToolResult(content=denial_msg, is_error=True)
            events.append(
                AgentRuntimeEvent(
                    kind="tool_result",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    content=_serialize_tool_content_for_event(tool_result.content),
                    is_error=tool_result.is_error,
                )
            )
            self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": item["id"],
                    "content": tool_result.content,
                }
            )
            return False, events, None

        item_ctx = {**tool_ctx, "_tool_call_id": item["id"]}
        exec_info: dict[str, Any] = {
            "item": item,
            "resolved_tool_name": resolved_tool_name,
            "tool": tool,
            "item_ctx": item_ctx,
            "side_effect": tool_side_effect,
        }
        return True, events, exec_info

    async def _execute_tool_stream(
        self,
        item: dict[str, Any],
        item_ctx: dict[str, Any],
    ) -> tuple[list[AgentRuntimeEvent], ToolResult]:
        """调用单个工具的流式接口，返回收集到的事件和最终结果。"""
        events: list[AgentRuntimeEvent] = []
        final_tool_result: ToolResult | None = None
        try:
            stream_gen = self._tool_registry.invoke_stream(
                item["function"]["name"],
                item["arguments"],
                ctx=item_ctx,
            )
            while True:
                try:
                    stream_event = await asyncio.wait_for(stream_gen.__anext__(), timeout=300)
                except StopAsyncIteration:
                    break

                if stream_event.kind == "event" and stream_event.runtime_event is not None:
                    from .session_utils import wrap_subagent_event

                    wrapped = wrap_subagent_event(
                        stream_event.runtime_event,
                        item["id"],
                    )
                    events.append(wrapped)
                elif stream_event.kind == "result":
                    final_tool_result = stream_event.tool_result
                    break

            tool_result = final_tool_result or ToolResult(content="无结果", is_error=True)
        except asyncio.TimeoutError:
            logger.warning(
                "工具执行超时(300秒): session=%s tool=%s",
                self.session_id,
                item["function"]["name"],
            )
            tool_result = ToolResult(content="工具执行超时（300秒）", is_error=True)
        except Exception as exc:
            logger.exception(
                "工具执行失败: session=%s tool=%s",
                self.session_id,
                item["function"]["name"],
            )
            tool_result = ToolResult(content=str(exc), is_error=True)
        return events, tool_result

    async def _finish_tool_execution(
        self,
        item: dict[str, Any],
        tool_result: ToolResult,
    ) -> AsyncGenerator[AgentRuntimeEvent, None]:
        """工具执行后的统一收尾：权限恢复、提示追加、事件输出、消息入队。"""
        if item["function"]["name"] == "exit_plan_mode" and not tool_result.is_error:
            self._restore_pre_plan_permission_mode()

        user_message = self._get_last_user_text()
        text = user_message.lower()
        is_expert_delegation_request = "专家" in text and any(
            k in text for k in ("委派", "派给", "让", "交给", "处理", "来做")
        )
        if is_expert_delegation_request:
            tool_name = item["function"]["name"]
            if tool_name == "Task":
                self._expert_delegation_hint_sent = True
            elif tool_name == "ListSystemExperts":
                tool_result.content = (
                    str(tool_result.content) + "\n<system-reminder>\n"
                    "用户要求将任务委派给专家处理。你已经获得专家目录，必须立即停止搜索/列出。\n"
                    "下一步直接调用 Task(subagent_name='<role_id>', description='任务简述', prompt='详细指令') 执行委派。\n"
                    "如果目标专家未安装，先调用 InstallExpert(name='<role_id>')，然后立即 Task。\n"
                    "</system-reminder>"
                )
            elif tool_name in ("InstallExpert", "ConfigureExpert"):
                tool_result.content = (
                    str(tool_result.content) + "\n<system-reminder>\n"
                    "用户要求将任务委派给专家处理。专家已安装/配置，下一步立即调用 Task(subagent_name='<role_id>', description='任务简述', prompt='详细指令') 执行委派。\n"
                    "</system-reminder>"
                )
            else:
                tool_result.content = (
                    str(tool_result.content) + "\n<system-reminder>\n"
                    "用户要求将任务委派给专家处理。请停止自己执行当前操作，"
                    "直接调用 Task(subagent_name='<role_id>', description='任务简述', prompt='详细指令') 执行委派。"
                    "如果目标专家未安装，先调用 InstallExpert(name='<role_id>')，然后立即 Task。\n"
                    "</system-reminder>"
                )

        yield AgentRuntimeEvent(
            display_hint=self._current_display_hint,
            kind="tool_result",
            tool_call_id=item["id"],
            tool_name=item["function"]["name"],
            content=_serialize_tool_content_for_event(tool_result.content),
            is_error=tool_result.is_error,
        )
        self._append_message(
            {
                "role": "tool",
                "tool_call_id": item["id"],
                "content": tool_result.content,
            }
        )
        if not tool_result.is_error:
            self._reset_loop_counter(item["function"]["name"])

    async def _execute_readonly_batch(
        self,
        infos: list[dict[str, Any]],
        tool_ctx: dict[str, Any],
    ) -> AsyncGenerator[AgentRuntimeEvent, None]:
        """并发执行一批只读工具，结果按传入顺序输出。

        无论工具执行还是收尾阶段是否抛异常，都必须保证批次内每个 tool_call
        都落盘一条对应的 tool 消息，避免留下"有 tool_calls 无 tool_result"的
        非法消息序列（provider 会 400）。
        """
        if not infos:
            return

        async def run_one(
            info: dict[str, Any],
        ) -> tuple[dict[str, Any], list[AgentRuntimeEvent], ToolResult]:
            events, tool_result = await self._execute_tool_stream(info["item"], info["item_ctx"])
            return info, events, tool_result

        # return_exceptions=True：即使某个 run_one 抛出未预期异常，也不会中断整批，
        # 其余工具的结果照常回收，异常项在下方合成 error tool_result 占位。
        results = await asyncio.gather(
            *(asyncio.create_task(run_one(info)) for info in infos),
            return_exceptions=True,
        )
        for info, result in zip(infos, results):
            if isinstance(result, BaseException):
                logger.exception(
                    "只读工具并发执行异常: session=%s tool=%s",
                    self.session_id,
                    info["item"]["function"]["name"],
                    exc_info=result,
                )
                error_result = ToolResult(content=f"工具执行异常: {result}", is_error=True)
                async for event in self._finish_tool_execution_safe(info["item"], error_result):
                    yield event
                continue

            _info, events, tool_result = result
            for event in events:
                yield event
            async for event in self._finish_tool_execution_safe(info["item"], tool_result):
                yield event

    async def _finish_tool_execution_safe(
        self,
        item: dict[str, Any],
        tool_result: ToolResult,
    ) -> AsyncGenerator[AgentRuntimeEvent, None]:
        """_finish_tool_execution 的兜底包装：即使收尾逻辑抛异常，也保证该 tool_call
        至少落盘一条 tool 消息，维持消息序列合法性。
        """
        try:
            async for event in self._finish_tool_execution(item, tool_result):
                yield event
        except Exception as exc:  # noqa: BLE001 - 收尾异常不能破坏消息序列闭合
            logger.exception(
                "工具收尾阶段异常: session=%s tool=%s",
                self.session_id,
                item["function"]["name"],
            )
            # 确保该 tool_call 有对应 tool 回复：若尚未落盘则补一条。
            already_replied = any(
                m.get("role") == "tool" and m.get("tool_call_id") == item["id"]
                for m in self.messages
            )
            if not already_replied:
                self._append_message(
                    {
                        "role": "tool",
                        "tool_call_id": item["id"],
                        "content": f"工具收尾异常: {exc}",
                    }
                )
                yield AgentRuntimeEvent(
                    kind="tool_result",
                    tool_call_id=item["id"],
                    tool_name=item["function"]["name"],
                    content=f"工具收尾异常: {exc}",
                    is_error=True,
                )

    async def _execute_write_tool(
        self,
        info: dict[str, Any],
    ) -> AsyncGenerator[AgentRuntimeEvent, None]:
        """串行执行单个有副作用工具（含写范围守卫检查）。"""
        item = info["item"]
        item_ctx = info["item_ctx"]

        # Per-Agent Write Allow Root 守卫：在工具执行前校验目标路径
        write_allow_root = item_ctx.get("write_allow_root")
        resource_lease_keys = item_ctx.get("resource_lease_keys")
        if write_allow_root or resource_lease_keys:
            tool_name = item["function"]["name"]
            arguments = item.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
            denial = check_write_guard(write_allow_root, tool_name, arguments, resource_lease_keys)
            if denial:
                tool_result = ToolResult(content=denial, is_error=True)
                yield AgentRuntimeEvent(
                    display_hint=self._current_display_hint,
                    kind="tool_result",
                    tool_call_id=item.get("id"),
                    tool_name=tool_name,
                    content=denial,
                    is_error=True,
                )
                self._append_message(
                    {
                        "role": "tool",
                        "tool_call_id": item.get("id"),
                        "content": denial,
                    }
                )
                return

        events, tool_result = await self._execute_tool_stream(item, item_ctx)
        for event in events:
            yield event
        async for event in self._finish_tool_execution(item, tool_result):
            yield event

    async def prompt(
        self,
        user_input: str | list[dict[str, Any]],
        *,
        merge_wire_messages: bool = False,
    ) -> AsyncGenerator[AgentRuntimeEvent, None]:
        del merge_wire_messages
        if self._closed:
            raise RuntimeError("Runtime session is already closed")
        if await self._is_session_budget_blocked():
            yield AgentRuntimeEvent(
                kind="budget_limited",
                text=self._session_budget_limited_text(),
                display_hint="visible",
            )
            yield AgentRuntimeEvent(
                kind="budget_updated",
                text=json.dumps(
                    {
                        "token_budget": self.budget.token_budget if self.budget else None,
                        "tokens_used": self.budget.tokens_used if self.budget else 0,
                        "time_budget_seconds": (
                            self.budget.time_budget_seconds if self.budget else None
                        ),
                        "time_used_seconds": self.budget.time_used_seconds if self.budget else 0,
                        "status": self.budget.status if self.budget else "active",
                    },
                    ensure_ascii=False,
                ),
                display_hint="visible",
            )
            return

        self.messages = self._downgrade_historical_image_messages(self.messages)
        normalized_input = self._normalize_user_input(user_input)
        existing_user_message_ids = {
            str(message.get("id"))
            for message in self.messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and message.get("id") is not None
        }
        for msg in normalized_input:
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if (
                isinstance(msg_id, str)
                and msg_id in existing_user_message_ids
                and msg.get("role") == "user"
            ):
                continue
            self._append_message(msg)
        self._continuation_state.reset()
        self._thinking_loop_nudge_sent = False

        # 每个新的 user message 重置 Auto-Nudge 状态
        self._auto_nudge_sent_for_current_turn = False
        self._post_list_nudge_sent_for_current_turn = False
        self._tools_used_since_user_message = False
        self._last_tool_name = None
        self._expert_delegation_hint_sent = False

        # ---- 上下文自动压缩 ----
        async for compact_event in self._maybe_compact_context(force=False):
            yield compact_event

        total_input_tokens = 0
        total_output_tokens = 0

        max_turns = self._spec.config.loop_control.max_steps_per_turn
        for _turn in range(max_turns):
            if self._cancel_event.is_set():
                break

            self._session_turn_count += 1
            self._current_turn_n = self._session_turn_count

            # 标记新一轮 ReAct turn 开始，让后端事件投影可以按顺序编号
            yield _TurnBegin()

            assistant_parts: list[str] = []
            assistant_reasoning = ""
            assistant_reasoning_signature: str | None = None
            assistant_reasoning_redacted: str | None = None
            aggregated_tool_calls: dict[int, dict[str, Any]] = {}
            latest_finish_reason: str | None = None
            latest_raw_finish_reason: str | None = None
            latest_usage: dict[str, Any] | None = None
            request_options = self._resolve_request_options()

            # thinking 死循环检测按轮独立：每轮的思考流是新的，跨轮累积会把上一轮的
            # 正常思考当成本轮「更早内容」里的重复，制造误报。
            self._thinking_loop_detector.reset()
            thinking_loop_hit: ThinkingLoopVerdict | None = None

            stream_error: Exception | None = None
            for retry_attempt in range(4):  # 1 次原始 + 3 次重试
                if self._cancel_event.is_set():
                    break

                # 重试时清空上一轮已收集的片段
                if retry_attempt > 0:
                    assistant_parts = []
                    assistant_reasoning = ""
                    assistant_reasoning_signature = None
                    assistant_reasoning_redacted = None
                    aggregated_tool_calls = {}
                    latest_finish_reason = None
                    latest_raw_finish_reason = None
                    latest_usage = None

                try:
                    messages_for_model = self._prepare_messages_for_current_model()
                    async for chunk in self._client.chat_stream(
                        messages_for_model,
                        self._prepare_tools_for_model(),
                        self._resolve_temperature(),
                        self._resolve_max_tokens(),
                        request_options=request_options,
                    ):
                        if self._cancel_event.is_set():
                            break

                        if chunk.usage is not None:
                            latest_usage = chunk.usage

                        if chunk.finish_reason:
                            latest_finish_reason = chunk.finish_reason
                            latest_raw_finish_reason = chunk.raw_finish_reason

                        delta = chunk.delta
                        if delta.reasoning_signature:
                            # provider 私有推理签名：原样保留，随 assistant 消息回灌。
                            assistant_reasoning_signature = delta.reasoning_signature
                        if delta.reasoning_redacted_data:
                            # 加密推理块 data：与签名分开累积，回灌时独立成块。
                            assistant_reasoning_redacted = delta.reasoning_redacted_data
                        if delta.content:
                            assistant_parts.append(delta.content)
                            yield AgentRuntimeEvent(
                                display_hint=self._current_display_hint,
                                kind="content",
                                content_type="text",
                                text=delta.content,
                            )

                        # 渲染只认模型实际吐出的事件，不认开关（step-code events.ts /
                        # harness ReasoningRow 同款原则）：开关只控制请求侧「不主动
                        # 要求思考」；模型服务端默认思考并返回了 reasoning 时，必须
                        # 展示并记入历史——隐藏实际输出会让 UI 对模型行为撒谎。
                        if delta.reasoning_content:
                            assistant_reasoning = merge_stream_fragment(
                                assistant_reasoning,
                                delta.reasoning_content,
                            )
                            yield AgentRuntimeEvent(
                                display_hint=self._current_display_hint,
                                kind="content",
                                content_type="think",
                                think=delta.reasoning_content,
                            )

                            # 思考流死循环：中途发现就中止，省掉整段无效思考的 token。
                            # 三个前置条件都必要：
                            # - 已吐出正文（assistant_parts 非空）说明模型没卡在思考里，
                            #   此时中止会打断正常回答；正文阶段的重复交给续写守卫管。
                            # - 本次 run 已注入过诱导跳出则不再中止，避免「打断—重开—再打断」
                            #   自己变成新的循环。
                            if (
                                self._thinking_loop_guard_enabled
                                and not self._thinking_loop_nudge_sent
                                and not assistant_parts
                            ):
                                verdict = self._thinking_loop_detector.ingest(
                                    delta.reasoning_content
                                )
                                if verdict.looping:
                                    thinking_loop_hit = verdict
                                    break

                        for tool_delta in delta.tool_calls or []:
                            if not isinstance(tool_delta, dict):
                                continue
                            index = int(tool_delta.get("index", 0))
                            current = aggregated_tool_calls.setdefault(
                                index,
                                {
                                    "id": None,
                                    "name": "",
                                    "arguments_text": "",
                                },
                            )
                            tool_id = tool_delta.get("id")
                            if isinstance(tool_id, str) and tool_id.strip():
                                current["id"] = tool_id.strip()

                            function = tool_delta.get("function") or {}
                            function_name = function.get("name")
                            if isinstance(function_name, str) and function_name:
                                current["name"] = merge_stream_fragment(
                                    current["name"],
                                    function_name,
                                )

                            arguments_fragment = function.get("arguments")
                            if isinstance(arguments_fragment, str) and arguments_fragment:
                                current["arguments_text"] += arguments_fragment

                    stream_error = None
                    break

                except Exception as exc:
                    model_id = self._resolve_model_id()
                    classified = classify_api_error(
                        exc,
                        provider="",
                        model=model_id,
                        approx_tokens=self._estimated_token_count,
                        num_messages=len(self.messages),
                    )

                    if not classified.retryable or retry_attempt >= 3:
                        stream_error = exc
                        break

                    if classified.should_compress:
                        try:
                            async for compact_event in self._maybe_compact_context(force=False):
                                yield compact_event
                        except Exception as compact_exc:
                            logger.warning("重试前上下文压缩失败: %s", compact_exc)

                    delay = jittered_backoff(retry_attempt + 1)
                    logger.warning(
                        "API 错误[%s]，%.1f 秒后第 %d 次重试: %s",
                        classified.reason.value,
                        delay,
                        retry_attempt + 1,
                        classified.message,
                    )
                    await asyncio.sleep(delay)
                    continue

            if self._cancel_event.is_set():
                break

            # thinking 死循环：本轮思考流已被中止，注入诱导跳出后重开一轮。
            # 放在流中断恢复之前——它不是错误，不该走 fallback 那条路。
            if thinking_loop_hit is not None:
                self._thinking_loop_nudge_sent = True
                logger.warning(
                    "检测到 thinking 死循环（重复单元 %r，重复 %s 次，已累积 %d 字符思考），"
                    "中止本轮并注入诱导跳出",
                    (thinking_loop_hit.sample or "")[:40],
                    thinking_loop_hit.repeats,
                    len(assistant_reasoning),
                )
                # 本轮 assistant 响应被中止，视为无效、不入历史（与参考实现一致）：
                # 半截的思考流没有对应的完整响应，写进历史反而给模型
                # 「我已经答过了」的假象。前端已收到的 think 事件仍会展示，那是展示层的事。
                yield AgentRuntimeEvent(
                    display_hint=self._current_display_hint,
                    kind="system_warning",
                    text=THINKING_LOOP_NUDGE,
                )
                # 保留 role=user（诱导跳出要让模型当成新指令来响应，且 Anthropic 协议
                # 不允许 system 出现在 messages 中间），但必须标 origin：
                # 不标的话恢复时会按 role 反推成 origin="user"，这条系统注入就与真人
                # 输入无法区分了——压缩保真、显示过滤、审计回溯三处都会误判。
                # 参考实现（step-code runTurn.ts:321）此处标的是 kind:'user'，是其缺陷，不照搬。
                self._append_message(
                    {
                        "role": "user",
                        "origin": "system_notice",
                        "content": THINKING_LOOP_NUDGE,
                    }
                )
                continue

            # 流中断恢复：重试用尽后，若已收集到部分内容则作为 fallback
            if stream_error is not None:
                fallback_content = "".join(assistant_parts)
                if fallback_content or assistant_reasoning or aggregated_tool_calls:
                    logger.warning(
                        "流中断，使用已传输内容作为最终响应: %d 字符",
                        len(fallback_content),
                    )
                    fallback_message: dict[str, Any] = {"role": "assistant"}
                    if fallback_content:
                        fallback_message["content"] = fallback_content
                    if assistant_reasoning:
                        fallback_message["reasoning_content"] = assistant_reasoning
                        if assistant_reasoning_signature:
                            fallback_message["reasoning_signature"] = assistant_reasoning_signature
                    # 加密推理块与可读 thinking 相互独立：只有 redacted 而无可读内容
                    # 是合法形态，故不嵌在上面的 reasoning 分支内。
                    if assistant_reasoning_redacted:
                        fallback_message["reasoning_redacted_data"] = assistant_reasoning_redacted
                    self._append_message(fallback_message)
                    yield AgentRuntimeEvent(
                        display_hint=self._current_display_hint,
                        kind="system_warning",
                        text=(
                            "<system>\n"
                            f"流式输出中断，使用已传输的 {len(fallback_content)} 字符作为最终响应。\n"
                            "</system>"
                        ),
                    )
                    break
                raise stream_error

            input_tokens, output_tokens = extract_usage_counts(latest_usage)
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            self._log_cache_stats(latest_usage)
            self._append_usage_record(latest_usage, input_tokens, output_tokens)

            # 用 LLM 返回的精确 prompt_tokens 修正估算值，优先于预算检查
            if latest_usage is not None:
                prompt_tokens = latest_usage.get("prompt_tokens")
                if prompt_tokens is None:
                    prompt_tokens = latest_usage.get("input_tokens")
                if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                    self._estimated_token_count = prompt_tokens
                    self._reset_pending_token_estimate()
                    await self._save_context_tokens_to_metadata()

            # Session 级预算检查（在 _estimated_token_count 修正之后）
            if self.budget is not None and self.budget.status == "active":
                await self._check_session_budget(input_tokens, output_tokens)

            assistant_content = "".join(assistant_parts) or None
            assistant_reasoning_content = assistant_reasoning or ""

            tool_calls = self._build_openai_tool_calls(aggregated_tool_calls)

            # Fallback: if no structured tool_calls but content has raw tags
            if not tool_calls and assistant_content and self._prepare_tools_for_model():
                raw_text = assistant_content
                if any(
                    tag in raw_text
                    for tag in (
                        "<tool_call>",
                        "<｜tool▁calls▁begin｜>",
                        "<|tool_calls_section_begin|>",
                        "<function=",
                        "[TOOL_CALLS]",
                        "<longcat_tool_call>",
                    )
                ):
                    try:
                        from .tool_call_parsers import get_parser

                        for parser_name in (
                            "hermes",
                            "deepseek_v3",
                            "kimi_k2",
                            "glm45",
                            "qwen3_coder",
                            "mistral",
                            "longcat",
                        ):
                            try:
                                parser = get_parser(parser_name)
                                parsed_content, parsed_calls = parser.parse(raw_text)
                                if parsed_calls:
                                    if parsed_content:
                                        assistant_content = parsed_content
                                    tool_calls = []
                                    for tc in parsed_calls:
                                        args_str = tc["function"]["arguments"]
                                        parsed_args, parse_error = self._safe_parse_arguments(
                                            tc["function"]["name"], args_str
                                        )
                                        tool_calls.append(
                                            {
                                                "id": tc["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": tc["function"]["name"],
                                                    "arguments": args_str,
                                                },
                                                "arguments": parsed_args,
                                                "_parse_error": parse_error,
                                            }
                                        )
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

            if tool_calls:
                # 标记当前 user message 已有工具调用
                self._tools_used_since_user_message = True

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": item["id"],
                            "type": item["type"],
                            "function": item["function"],
                        }
                        for item in tool_calls
                    ],
                }
                if assistant_content is not None:
                    assistant_message["content"] = assistant_content
                if assistant_reasoning_content is not None:
                    assistant_message["reasoning_content"] = assistant_reasoning_content
                    if assistant_reasoning_signature:
                        assistant_message["reasoning_signature"] = assistant_reasoning_signature
                if assistant_reasoning_redacted:
                    assistant_message["reasoning_redacted_data"] = assistant_reasoning_redacted
                self._append_message(assistant_message)

                tool_ctx = self._tool_context()
                idx = 0
                read_only_batch: list[dict[str, Any]] = []
                while idx < len(tool_calls):
                    item = tool_calls[idx]
                    approved, auth_events, exec_info = await self._authorize_single_tool(
                        item, tool_ctx
                    )
                    for event in auth_events:
                        yield event
                    if not approved:
                        idx += 1
                        continue
                    if exec_info["side_effect"]:
                        if read_only_batch:
                            async for event in self._execute_readonly_batch(
                                read_only_batch, tool_ctx
                            ):
                                yield event
                            read_only_batch = []
                        async for event in self._execute_write_tool(exec_info):
                            yield event
                        idx += 1
                        continue
                    read_only_batch.append(exec_info)
                    idx += 1
                if read_only_batch:
                    async for event in self._execute_readonly_batch(read_only_batch, tool_ctx):
                        yield event

                continue

            # 记录本轮 assistant 文本内容是否已落盘，避免 length 续写分支重复追加。
            assistant_content_appended = False
            if assistant_content is not None:
                assistant_message = {
                    "role": "assistant",
                    "content": assistant_content,
                }
                if assistant_reasoning_content is not None:
                    assistant_message["reasoning_content"] = assistant_reasoning_content
                    if assistant_reasoning_signature:
                        assistant_message["reasoning_signature"] = assistant_reasoning_signature
                if assistant_reasoning_redacted:
                    assistant_message["reasoning_redacted_data"] = assistant_reasoning_redacted
                self._append_message(assistant_message)
                assistant_content_appended = True

            # Auto-Nudge: 用户消息可执行但 Agent 首回合只回复文字时，注入运行时纠正
            if (
                not tool_calls
                and assistant_content is not None
                and not self._auto_nudge_sent_for_current_turn
                and not self._tools_used_since_user_message
            ):
                user_message = self._get_last_user_text()
                nudge = self._maybe_auto_nudge(
                    user_message,
                    has_tool_calls=bool(tool_calls),
                )
                if nudge:
                    self._auto_nudge_sent_for_current_turn = True
                    yield AgentRuntimeEvent(
                        display_hint=self._current_display_hint,
                        kind="system_warning",
                        text=nudge,
                    )
                    self._append_message({"role": "system", "content": nudge})
                    continue

            # Post-List Nudge: Agent 已列出/搜索目录但只回复文字，未执行后续操作
            if (
                not tool_calls
                and assistant_content is not None
                and not self._post_list_nudge_sent_for_current_turn
                and self._tools_used_since_user_message
                and self._last_tool_name
            ):
                user_message = self._get_last_user_text()
                nudge = self._maybe_post_list_nudge(
                    user_message,
                    self._last_tool_name,
                )
                if nudge:
                    self._post_list_nudge_sent_for_current_turn = True
                    yield AgentRuntimeEvent(
                        display_hint=self._current_display_hint,
                        kind="system_warning",
                        text=nudge,
                    )
                    self._append_message({"role": "system", "content": nudge})
                    continue

            # 触发值只判归一化枚举。dev（79206e9 起）把 finish_reason 收敛为 FinishReason
            # 六值，OpenAI 的 "length" 与 Anthropic 的 "max_tokens" 都映射到 "truncated"；
            # 适配器漏归一化会被 __post_init__ 降级成 "other" 并打 warning，所以裸判
            # provider 原始字符串既不合契约也是死代码。要诊断原始值看 latest_raw_finish_reason。
            if latest_finish_reason == "truncated":
                # 截断自动续写的六道守卫：三条确定性判据（零进展 / 与上轮完全相同 /
                # 从头重来）+ 两条文本病态（尾部周期复读 / 龟速）+ 次数兜底。
                # 旧实现只有次数兜底，模型卡死时会白烧满 3 轮才停，且停下来只说
                # 「次数已达上限」，看不出到底是被截断还是卡住了。
                state = self._continuation_state
                max_continues = self._spec.config.loop_control.max_auto_continues
                chunk = assistant_content if isinstance(assistant_content, str) else ""

                if self._continuation_guard_enabled:
                    verdict = check_continuation_safety(chunk, state, max_continues)
                elif state.count + 1 >= max_continues:
                    # 守卫关闭时退回旧行为：只看次数。
                    verdict = ContinuationVerdict(
                        safe=False, reason="max_continues", detail=state.count + 1
                    )
                else:
                    verdict = ContinuationVerdict(safe=True)

                if not verdict.safe:
                    logger.warning(
                        "自动续写被守卫拦下：reason=%s detail=%s（已续写 %d 次，本轮正文 %d 字符）",
                        verdict.reason,
                        verdict.detail,
                        state.count,
                        len(chunk),
                    )
                    yield AgentRuntimeEvent(
                        display_hint=self._current_display_hint,
                        kind="system_warning",
                        text=describe_continuation_stop(verdict),
                    )
                    break

                advance_continuation(chunk, state)

                # 保存已收集的部分 assistant 消息。
                # 若上文已落盘纯文本内容（assistant_content_appended），此处不再重复
                # 追加 content，只在存在 tool_calls 时补一条带 tool_calls 的 assistant 消息。
                if not assistant_content_appended:
                    partial_message: dict[str, Any] = {"role": "assistant"}
                    if assistant_content is not None:
                        partial_message["content"] = assistant_content
                    if assistant_reasoning_content is not None:
                        partial_message["reasoning_content"] = assistant_reasoning_content
                        if assistant_reasoning_signature:
                            partial_message["reasoning_signature"] = assistant_reasoning_signature
                    if assistant_reasoning_redacted:
                        partial_message["reasoning_redacted_data"] = assistant_reasoning_redacted
                    if tool_calls:
                        partial_message["tool_calls"] = [
                            {
                                "id": item["id"],
                                "type": item["type"],
                                "function": item["function"],
                            }
                            for item in tool_calls
                        ]
                    self._append_message(partial_message)
                elif tool_calls:
                    # 内容已落盘但仍带 tool_calls（罕见）：补一条仅含 tool_calls 的消息。
                    self._append_message(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": item["id"],
                                    "type": item["type"],
                                    "function": item["function"],
                                }
                                for item in tool_calls
                            ],
                        }
                    )

                # 注入续写提示
                self._append_message(
                    {
                        "role": "user",
                        "content": "[System: Your previous response was truncated due to length limit. Continue exactly where you left off without repeating anything already said.]",
                    }
                )

                logger.info(
                    "检测到 finish_reason=truncated（provider 原始值=%s），触发第 %d 次自动续写",
                    latest_raw_finish_reason,
                    state.count,
                )
                continue

            if latest_finish_reason != "tool_calls":
                if latest_finish_reason in ("paused", "other"):
                    logger.warning(
                        "非常规停止原因导致本轮终止: finish_reason=%s provider 原始值=%s",
                        latest_finish_reason,
                        latest_raw_finish_reason,
                    )
                break

        # ReAct 循环结束，清除当前 turn 标记
        self._current_turn_n = None

        if self._cancel_event.is_set():
            raise RunCancelled("会话运行已取消")

        if total_input_tokens or total_output_tokens:
            yield AgentRuntimeEvent(
                display_hint=self._current_display_hint,
                kind="token_usage",
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                # 当前上下文占用，前端可直接用于刷新 TokenUsageBar
                context_tokens=self.effective_token_count,
            )

        # Budget Mode: 推送最终 session budget 状态
        if self.budget is not None:
            yield AgentRuntimeEvent(
                display_hint=self._current_display_hint,
                kind="budget_updated",
                text=json.dumps(
                    {
                        "token_budget": self.budget.token_budget,
                        "tokens_used": self.budget.tokens_used,
                        "time_budget_seconds": self.budget.time_budget_seconds,
                        "time_used_seconds": self.budget.time_used_seconds,
                        "status": self.budget.status,
                    },
                    ensure_ascii=False,
                ),
            )

    @staticmethod
    def _is_memory_only_request(text: str) -> bool:
        """检测用户是否明确要求基于记忆/历史回复，不调用工具。"""
        lowered = text.lower()
        keywords = [
            "不要调用工具",
            "不要调工具",
            "不要调用任何工具",
            "不要调任何工具",
            "只用记忆",
            "仅凭记忆",
            "从记忆",
            "凭记忆",
            "根据对话历史",
            "基于对话历史",
            "answer from memory",
            "from memory",
            "do not call any tools",
            "don't call any tools",
            "do not use tools",
            "don't use tools",
            "without calling tools",
            "no tools",
        ]
        return any(kw in lowered for kw in keywords)

    def _maybe_auto_nudge(
        self,
        user_message: str,
        has_tool_calls: bool,
    ) -> str | None:
        """CheetahClaws 风格的运行时纠正。

        当用户消息包含可执行任务（文件路径、任务动词），
        但 Agent 只回复了文字没有调用工具时，
        透明注入一条 <system-reminder> 提示重试。
        """
        if not getattr(self, "_auto_nudge_enabled", True):
            return None
        if has_tool_calls:
            return None
        if self._is_memory_only_request(user_message):
            return None
        if not self._looks_like_actionable(user_message):
            return None

        return (
            "<system-reminder>\n"
            "你只回复了文字，但用户的消息包含具体文件路径或可执行任务。\n"
            "不要询问用户已经提供了什么信息。请直接：\n"
            "1. 用 Bash ls 或 Glob 确认路径\n"
            "2. 用 ReadFile 读取相关文件\n"
            "3. 执行用户要求的操作\n"
            "请重新尝试。\n"
            "</system-reminder>"
        )

    def _maybe_post_list_nudge(
        self,
        user_message: str,
        last_tool_name: str | None,
    ) -> str | None:
        """列表/搜索工具已返回，但 Agent 只回复文字未执行下一步时，提示标准工作流。"""
        if not getattr(self, "_auto_nudge_enabled", True):
            return None
        if not last_tool_name:
            return None

        text = user_message.lower()

        # Skill 列表/搜索后的禁用意图：给出标准下一步，目标未明确时仍允许询问
        if last_tool_name in ("ListSkills", "SearchStoreSkills"):
            if any(k in text for k in ("禁用", "关闭", "卸载", "移除", "删掉", "不要")):
                return (
                    "<system-reminder>\n"
                    "标准工作流：用户表达禁用 Skill 的意图并已获得列表。\n"
                    "下一步通常是调用 DisableSkill(name='<skill_name>')。\n"
                    "如果用户明确说了名字，直接禁用该名字；如果名字不明确，可以先确认再操作。\n"
                    "</system-reminder>"
                )

        # 专家列表后的禁用/委派意图
        if last_tool_name == "ListSystemExperts":
            if any(k in text for k in ("禁用", "关闭", "关掉", "不要出现")):
                return (
                    "<system-reminder>\n"
                    "标准工作流：用户表达关闭专家的意图并已获得目录。\n"
                    "下一步通常是 ConfigureExpert(name='<role_id>', enabled=false)。\n"
                    "如果用户明确说了专家名字，直接关闭；如果不明确，可以先确认再操作。\n"
                    "</system-reminder>"
                )
            if any(k in text for k in ("委派", "派给", "让", "处理", "交给")):
                return (
                    "<system-reminder>\n"
                    "标准工作流：用户要求把任务交给专家处理。\n"
                    "请使用 TaskTool(subagent_name='<role_id>', description='任务简述', prompt='详细指令') 委派给对应专家。\n"
                    "</system-reminder>"
                )

        # 环境变量列表后的删除意图
        if last_tool_name == "ListEnvVars":
            if any(k in text for k in ("删除", "移除", "删掉", "不用")):
                return (
                    "<system-reminder>\n"
                    "标准工作流：用户表达删除环境变量的意图并已获得列表。\n"
                    "下一步通常是 DeleteEnvVar(name='<var_name>')。\n"
                    "</system-reminder>"
                )

        # 安装专家后的禁用意图：明确要求禁用
        if last_tool_name == "InstallExpert":
            if any(k in text for k in ("禁用", "关闭", "关掉", "不要出现")):
                return (
                    "<system-reminder>\n"
                    "标准工作流：用户要求安装专家后再禁用。\n"
                    "InstallExpert 只是安装/启用，下一步必须调用 ConfigureExpert(name='<role_id>', enabled=false) 完成禁用。\n"
                    "</system-reminder>"
                )

        # 兜底：用户消息明确要求专家委派，但 Agent 没有使用专家相关工具
        if (
            last_tool_name not in ("ListSystemExperts", "InstallExpert", "ConfigureExpert", "Task")
            and "专家" in text
            and any(k in text for k in ("委派", "派给", "让", "交给", "处理", "来做"))
        ):
            return (
                "<system-reminder>\n"
                "用户要求将任务委派给专家处理。请立即停止自己执行，也不要再重复调用 ListSystemExperts。\n"
                "直接调用 Task(subagent_name='<role_id>', description='任务简述', prompt='详细指令') 将任务交给对应专家。\n"
                "如果专家未安装，先调用 InstallExpert(name='<role_id>')，然后立即 Task。\n"
                "</system-reminder>"
            )

        return None

    @staticmethod
    def _looks_like_actionable(text: str) -> bool:
        """检测是否为可执行任务。"""
        import re

        # 用户明确要求基于记忆回复，不按可执行任务处理
        if SessionStreamMixin._is_memory_only_request(text):
            return False

        # 剥离 URL
        stripped = re.sub(r"https?://\S+", "", text)
        # 含绝对路径
        if re.search(r"(?:^|\s)(/[a-zA-Z0-9_./-]{2,})", stripped):
            return True
        # 含文件扩展名
        if re.search(r"\.(?:py|ts|js|json|yaml|md|ipynb|csv|toml|cfg|sh)\b", stripped):
            return True
        # 明确的任务动词
        task_verbs = [
            "修改",
            "创建",
            "运行",
            "测试",
            "删除",
            "安装",
            "配置",
            "编译",
            "构建",
            "更新",
            "添加",
            "移除",
            "替换",
            "读取",
            "写入",
            "查看",
        ]
        if any(w in stripped for w in task_verbs):
            return True
        return False

    def _get_last_user_text(self) -> str:
        """从 messages 中提取最近一条 user 消息的真实用户输入文本。

        先提取消息文本，再还原执行契约，避免把契约中的任务动词
        （修改、创建、运行、测试等）误判为用户真实意图。
        """
        for message in reversed(self.messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content", "")
                text = extract_message_text(content)
                unwrapped = unwrap_user_prompt(text)
                return unwrapped if isinstance(unwrapped, str) else text
        return ""

    def _get_skill_security(self, skill_name: str) -> dict[str, Any]:
        """查询 Skill 安全元数据，用于 EnableSkill/DisableSkill 授权决策。"""
        try:
            from pathlib import Path

            from app.skills.manager import get_skill_manager

            workspace_path = Path(str(self._spec.work_dir))
            mgr = get_skill_manager()
            all_skills = mgr.list_all_skills(workspace_path)
            for skill in all_skills:
                if skill.name == skill_name:
                    sec = skill.security
                    return {
                        "source_trust": sec.source_trust,
                        "risk_level": sec.risk_level,
                        "has_scripts": sec.has_scripts,
                        "requires_env": sec.requires_env,
                        "writes_workspace": sec.writes_workspace,
                        "writes_global": sec.writes_global,
                        "uses_shell": sec.uses_shell,
                        "uses_network": sec.uses_network,
                        "installs_dependencies": sec.installs_dependencies,
                        "adds_tools": sec.adds_tools,
                    }
        except Exception:
            logger.debug("查询 Skill 安全元数据失败: %s", skill_name, exc_info=True)
        return {}

    def _log_cache_stats(self, usage: dict[str, Any] | None) -> None:
        """记录 prefix cache hit 统计（Anthropic / OpenRouter 格式）。"""
        if not usage:
            return
        try:
            prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens", 0)
            cached = usage.get("cache_read_input_tokens", 0) or 0
            written = usage.get("cache_creation_input_tokens", 0) or 0
            if prompt_tokens and (cached or written):
                hit_pct = cached / prompt_tokens * 100
                logger.info(
                    "Cache: %d/%d tokens (%.0f%% hit, %d written)",
                    cached,
                    prompt_tokens,
                    hit_pct,
                    written,
                )
        except Exception as exc:
            logger.warning("缓存统计失败: %s", exc, exc_info=True)
