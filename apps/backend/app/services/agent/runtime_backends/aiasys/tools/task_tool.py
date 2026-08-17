"""AIASys 原生子 Agent 调度工具 (TaskTool)。

基于自有 AiasysRuntimeBackend 实现子 Agent 创建与执行。

支持 background 模式：background=True 时立即返回 task_id，子 Agent 在后台运行。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import tomllib
import uuid
from collections.abc import AsyncGenerator
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import tomli_w

from app.core.agent_tool import AiasysTool
from app.core.tool_result import ToolResult
from app.services.agent.agent_path import AgentPath, normalize_agent_max_depth
from app.services.agent.runtime_backends.aiasys.backend import AiasysRuntimeBackend
from app.services.agent.runtime_backends.aiasys.session import AiasysRuntimeSession
from app.services.agent.runtime_backends.base import (
    AgentRuntimeEvent,
    RuntimeSessionCreateSpec,
)
from app.services.agent.subagent_lifecycle import SubAgentLifecycleManager
from app.services.agent.subagent_registry import get_subagent_registry
from app.services.agent.subagent_storage import SubAgentStorage
from app.services.agent.system_presets import (
    get_role_type_default_tools,
    get_subagent_universal_excludes,
)
from app.services.history import (
    current_session_id,
    current_session_root,
    current_user_id,
    current_workspace,
)
from app.utils.path_utils import as_system_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background task registry
# ---------------------------------------------------------------------------

# Module-level: agent_id -> asyncio.Task 映射，用于后台任务的追踪与状态查询。
# 设计理由：
# - 子 Agent session 已通过 SubAgentRegistry 在内存中管理（含状态/launch_spec）。
# - asyncio.Task 对象在这里额外追踪，用于：
#   - 查询任务是否仍在运行（task.done()）
#   - 等待任务完成（task.result()）
# - 生命周期结束时由 lifecycle manager 自行清理，此处不干预。
_background_tasks: dict[str, asyncio.Task] = {}
_background_tasks_lock = threading.Lock()


async def get_background_task(task_id: str) -> asyncio.Task | None:
    """获取后台任务（不阻塞）。"""
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


async def is_background_task_running(task_id: str) -> bool:
    """检查后台任务是否仍在运行。"""
    task = await get_background_task(task_id)
    return task is not None and not task.done()


async def wait_background_task(task_id: str, timeout: float | None = None) -> Any | None:
    """等待后台任务完成，返回结果或 None。"""
    task = await get_background_task(task_id)
    if task is None:
        return None
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        return None


async def remove_background_task(task_id: str) -> None:
    """从注册表中移除（任务自行结束后调用）。"""
    with _background_tasks_lock:
        _background_tasks.pop(task_id, None)


def _filter_mcp_configs(
    host_mcp_configs: list | None,
    mcp_policy: str,
    mcp_servers: list[str],
) -> list | None:
    """根据子 Agent 的 MCP 继承策略过滤 Host 的 MCP 配置。

    策略:
    - none: 不继承任何 MCP 配置
    - inherit: 完整继承 Host 的所有 MCP 配置
    - allowlist: 只继承 mcp_servers 中列出的 server
    - denylist: 继承全部，但排除 mcp_servers 中列出的 server
    """
    policy = (mcp_policy or "none").strip().lower()
    if policy == "none":
        return None
    if not host_mcp_configs:
        return None

    if policy == "inherit":
        return host_mcp_configs

    allowed_names = {name.strip() for name in mcp_servers if name.strip()}
    if not allowed_names:
        return None if policy == "allowlist" else host_mcp_configs

    filtered: list[dict[str, Any]] = []
    for block in host_mcp_configs:
        if not isinstance(block, dict):
            continue
        raw_servers = block.get("mcpServers") or block.get("mcp_servers") or {}
        if not isinstance(raw_servers, dict):
            continue
        new_servers: dict[str, Any] = {}
        for server_name, server_config in raw_servers.items():
            name = str(server_name).strip()
            if policy == "allowlist":
                if name in allowed_names:
                    new_servers[name] = server_config
            elif policy == "denylist":
                if name not in allowed_names:
                    new_servers[name] = server_config
        if new_servers:
            # 保持原始 key 风格
            key = "mcpServers" if "mcpServers" in block else "mcp_servers"
            filtered.append({key: new_servers})

    return filtered if filtered else None


def _resolve_skills_dir(
    workspace: Path,
    skill_policy: str,
    skills: list[str],
) -> Path | None:
    """根据子 Agent 的 Skill 继承策略决定 skills_dir。

    策略:
    - none: 不继承任何 Skill
    - inherit: 继承 Host workspace 的全部 Skill
    - allowlist/denylist: 返回原始 workspace skills 目录路径，
      由 runtime 在加载 system prompt 时做过滤注入
    """
    policy = (skill_policy or "inherit").strip().lower()
    if policy == "none":
        return None
    workspace_skills = workspace / ".aiasys" / "skills"
    if not workspace_skills.exists():
        return None
    return workspace_skills


_TASK_PARAMETERS = {
    "type": "object",
    "properties": {
        "subagent_name": {
            "type": "string",
            "description": "要调用的子 Agent 名称。可用预设角色: coder, data_analyst, researcher, reviewer。也可使用自定义子 Agent 名称。省略时默认使用 coder。当用户说'让数据分析专家处理'或'委派给某个专家'时，必须把对应 role_id 填到这里。",
        },
        "description": {
            "type": "string",
            "description": "任务简述，用于 UI 展示和日志",
        },
        "prompt": {
            "type": "string",
            "description": "给子 Agent 的完整任务指令。当用户要求委派任务给专家时，把用户原任务改写为清晰指令填到这里。",
        },
        "background": {
            "type": "boolean",
            "description": "是否在后台运行（不阻塞当前回合，立即返回 task_id）。省略时默认 False（同步等待完成）。",
        },
    },
    "required": ["prompt"],
}


def _streaming_event(event: AgentRuntimeEvent) -> ToolResult:
    """将一个 AgentRuntimeEvent 包装为流式 ToolResult。"""
    return ToolResult(
        content="",
        is_error=False,
        artifacts=[{"_streaming_event": asdict(event)}],
    )


def _annotate_subagent_runtime_event(
    event: AgentRuntimeEvent,
    *,
    agent_id: str,
    subagent_name: str,
) -> AgentRuntimeEvent:
    payload = asdict(event)
    payload["agent_id"] = str(payload.get("agent_id") or agent_id)
    payload["subagent_type"] = str(payload.get("subagent_type") or subagent_name)
    payload["subagent_name"] = str(payload.get("subagent_name") or subagent_name)
    return AgentRuntimeEvent(**payload)


def _find_subagent_manifest(
    host_agent_config: dict[str, Any],
    subagent_name: str,
) -> dict[str, Any] | None:
    """从 Host agent config 中查找指定子 Agent 的 manifest。"""
    subagents = host_agent_config.get("subagents") or {}
    if not isinstance(subagents, dict):
        return None
    binding = subagents.get(subagent_name)
    if not isinstance(binding, dict):
        return None
    manifest = binding.get("agent_manifest")
    if isinstance(manifest, dict):
        return manifest
    raw_path = binding.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        with Path(as_system_path(str(path))).open("rb") as file:
            payload = tomllib.load(file) or {}
    except Exception:
        logger.warning(
            "读取子 Agent manifest 失败: subagent=%s path=%s",
            subagent_name,
            raw_path,
            exc_info=True,
        )
        return None
    agent = payload.get("agent")
    if isinstance(agent, dict):
        return agent
    return None


def _resolve_collaboration_policy(ctx: dict[str, Any]) -> dict[str, Any]:
    raw_policy = ctx.get("collaboration_policy")
    if hasattr(raw_policy, "model_dump"):
        raw_policy = raw_policy.model_dump()
    if not isinstance(raw_policy, dict):
        raw_policy = {}

    max_depth = normalize_agent_max_depth(raw_policy.get("max_depth"), default=1)
    if max_depth < 1:
        max_depth = 1

    return {
        "max_depth": max_depth,
        "allow_nested_spawn": bool(raw_policy.get("allow_nested_spawn", False)),
        "max_threads": raw_policy.get("max_threads"),
    }


def _materialize_subagent_toml(
    manifest: dict[str, Any],
    subagent_name: str,
    tmpdir: str | None = None,
) -> Path:
    """将子 Agent manifest 物化为临时 TOML 文件。

    如果提供 tmpdir 则使用该目录，否则自行创建临时目录。
    调用方负责在 finally 块中清理临时目录。
    """
    import tempfile

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"native_subagent_{subagent_name}_{timestamp}_{uuid.uuid4().hex[:8]}.toml"
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp(prefix="aiasys_subagent_")
    path = Path(tmpdir) / filename

    # manifest 已经是 agent 段内容，需要包装为完整 TOML
    clean_manifest = {k: v for k, v in deepcopy(manifest).items() if v is not None}
    payload = {"version": 1, "agent": clean_manifest}
    with open(as_system_path(str(path)), "wb") as f:
        tomli_w.dump(payload, f)
    return path


class TaskTool(AiasysTool):
    """AIASys 原生子 Agent 调度工具。

    当 Host Agent 调用 Task 时，此工具：
    1. 查找子 Agent 配置
    2. 创建独立的工作区和存储
    3. 启动新的 AiasysRuntimeSession 运行子 Agent
    4. 流式返回子 Agent 的执行事件
    """

    name = "Task"
    description = (
        "将任务委派给专门的子 Agent 执行。"
        "参数: subagent_name(子Agent名称, 可选, 默认coder), description(任务简述), prompt(完整指令)"
    )
    parameters = _TASK_PARAMETERS

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """同步调用（非流式）。

        为了兼容非流式场景，提供同步版本。
        实际会运行完整子 Agent，但只返回最终结果。
        """
        # 收集所有流式结果，只返回最后一个 result
        final_result: ToolResult | None = None
        async for item in self.invoke_stream(ctx, **kwargs):
            if item.artifacts and any(
                isinstance(a, dict) and a.get("_streaming_event") is not None
                for a in item.artifacts
            ):
                continue
            final_result = item
        return final_result or ToolResult(content="子 Agent 执行完成（无输出）")

    async def invoke_stream(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[ToolResult, None]:
        """流式调用子 Agent。

        yield 中间事件（通过 _streaming_event artifact 标记）和最终结果。
        当 background=True 时，仅 yield 一次启动结果（含 task_id），子 Agent 在后台运行。
        """
        ctx = ctx or {}
        subagent_name = str(kwargs.get("subagent_name") or "").strip() or "coder"
        description = str(kwargs.get("description") or "").strip()
        prompt = str(kwargs.get("prompt") or "").strip()
        background = bool(kwargs.get("background", False))
        write_allow_root: list[str] | None = kwargs.get("write_allow_root")

        if not prompt:
            yield ToolResult(content="缺少 prompt 参数", is_error=True)
            return

        # ---- setup phase（与 background 无关，必须同步完成）----
        setup_error = None
        setup_values = None
        try:
            setup_values = await self._setup_subagent(
                ctx=ctx,
                kwargs=kwargs,
                subagent_name=subagent_name,
                description=description,
                prompt=prompt,
                write_allow_root=write_allow_root,
            )
        except ValueError as exc:
            # _setup_subagent 中的预检查失败（manifest 未找到/深度超限/并发超限等）
            # 保持与原代码一致的行为：yield 错误结果而非抛出异常
            setup_error = str(exc)
        except Exception as exc:
            setup_error = f"设置子 Agent 失败: {exc}"
            logger.exception("_setup_subagent 未预期错误")

        if setup_error:
            yield ToolResult(content=setup_error, is_error=True)
            return

        (
            user_id,
            host_session_id,
            workspace,
            session_root,
            host_agent_config,
            host_llm_config,
            subagent_manifest,
            agent_id,
            child_path,
            parent_agent_id,
            child_allow_spawn,
            max_threads,
            registry,
            parent_tool_call_id,
            effective_model,
            nickname,
            storage,
            tool_policy,
            fork_turns,
            subagent_toml_path,
            child_mcp_configs,
            child_skills_dir,
            spec,
            subagent_session,
            full_launch_spec,
        ) = setup_values

        # 持久化初始用户指令
        await storage.append_context_message(
            {
                "role": "user",
                "content": prompt,
                "parent_tool_call_id": parent_tool_call_id,
            }
        )

        if background:
            # ---- background 模式：用 asyncio.create_task 在后台跑 ----
            # contextvar 快照：create_task 在 Python 3.12+ 会 copy 当前 context，
            # 所以 setup 阶段设置的 current_workspace/current_session_id 等会被子任务继承。
            lifecycle_manager = SubAgentLifecycleManager(registry=registry)

            async def _run_in_background() -> ToolResult:
                final_result = ToolResult(content="子 Agent 后台执行完成（无输出）")
                try:
                    async for result in lifecycle_manager.run_subagent_session(
                        subagent_session=subagent_session,
                        agent_id=agent_id,
                        subagent_name=subagent_name,
                        prompt=prompt,
                        storage=storage,
                        keep_alive=True,
                        timeout_seconds=full_launch_spec.get("timeout_seconds", 300),
                        workspace=workspace,
                        session_root=session_root,
                        user_id=user_id,
                        host_session_id=host_session_id,
                    ):
                        if not result.artifacts or not any(
                            isinstance(a, dict) and a.get("_streaming_event") is not None
                            for a in result.artifacts
                        ):
                            final_result = result
                except asyncio.CancelledError:
                    final_result = ToolResult(
                        content="子 Agent 执行被取消",
                        is_error=True,
                    )
                except Exception as exc:
                    logger.exception("后台子 Agent 执行异常: agent_id=%s", agent_id)
                    final_result = ToolResult(
                        content=f"后台子 Agent 执行异常: {exc}",
                        is_error=True,
                    )
                finally:
                    # 清理后台任务注册
                    await remove_background_task(agent_id)
                    try:
                        await storage.flush()
                    except Exception:
                        logger.warning("后台子 Agent 缓冲刷盘失败", exc_info=True)
                return final_result

            bg_task = asyncio.create_task(_run_in_background())
            with _background_tasks_lock:
                _background_tasks[agent_id] = bg_task

            yield ToolResult(
                content=(
                    f"子 Agent 已在后台启动。\n"
                    f"- task_id: {agent_id}\n"
                    f"- subagent: {subagent_name}\n"
                    f"- 状态: running（使用 TaskTool(background=True) 查询结果）"
                ),
                artifacts=[{"task_id": agent_id, "status": "running"}],
            )
            return

        # ---- foreground 模式（默认）：流式返回事件 ----
        lifecycle_manager = SubAgentLifecycleManager(registry=registry)
        try:
            async for result in lifecycle_manager.run_subagent_session(
                subagent_session=subagent_session,
                agent_id=agent_id,
                subagent_name=subagent_name,
                prompt=prompt,
                storage=storage,
                keep_alive=True,
                timeout_seconds=full_launch_spec.get("timeout_seconds", 300),
                workspace=workspace,
                session_root=session_root,
                user_id=user_id,
                host_session_id=host_session_id,
            ):
                yield result
        finally:
            try:
                await storage.flush()
            except Exception:
                logger.warning("子 Agent 缓冲刷盘失败", exc_info=True)

    async def _setup_subagent(
        self,
        ctx: dict[str, Any],
        kwargs: dict[str, Any],
        subagent_name: str,
        description: str,
        prompt: str,
        write_allow_root: list[str] | None = None,
    ) -> tuple[
        str,  # user_id
        str,  # host_session_id
        Path,  # workspace
        Path,  # session_root
        dict,  # host_agent_config
        Any,  # host_llm_config
        dict,  # subagent_manifest
        str,  # agent_id
        AgentPath,  # child_path
        str | None,  # parent_agent_id
        bool,  # child_allow_spawn
        int | None,  # max_threads
        Any,  # registry
        str,  # parent_tool_call_id
        str | None,  # effective_model
        str | None,  # nickname
        SubAgentStorage,  # storage
        str,  # tool_policy
        int | None,  # fork_turns
        Path,  # subagent_toml_path
        list | None,  # child_mcp_configs
        Path | None,  # child_skills_dir
        RuntimeSessionCreateSpec,  # spec
        AiasysRuntimeSession,  # subagent_session
        dict,  # full_launch_spec
    ]:
        """子 Agent 的同步设置阶段（步骤 1-9，与 background 无关）。

        返回所有 setup 阶段产出的值，供 invoke_stream 的两种模式使用。
        """
        user_id = str(ctx.get("user_id") or current_user_id.get() or "")
        session_id = str(ctx.get("session_id") or current_session_id.get() or "")
        host_session_id = str(ctx.get("host_session_id") or session_id)
        workspace = Path(str(ctx.get("workspace") or current_workspace.get() or ""))
        session_root = Path(str(ctx.get("session_root") or current_session_root.get() or workspace))
        host_agent_config = ctx.get("agent_config") or {}
        host_llm_config = ctx.get("llm_config")

        if not user_id or not session_id:
            raise ValueError("无法确定当前会话上下文")

        # 1. 查找子 Agent manifest
        subagent_manifest = await asyncio.to_thread(
            _find_subagent_manifest, host_agent_config, subagent_name
        )
        if subagent_manifest is None:
            from app.services.agent.subagent_catalog import (
                get_normalized_enabled_expert_role_ids,
                is_subagent_dispatch_enabled,
                load_subagent_for_runtime,
            )

            normalized_enabled_expert_role_ids = get_normalized_enabled_expert_role_ids(
                user_id=user_id,
                session_id=session_id,
            )
            workspace_id = user_id
            try:
                from app.services.workspace_registry import get_workspace_registry_service

                registry_svc = get_workspace_registry_service()
                resolved = registry_svc.find_workspace_id_by_session_id(user_id, session_id)
                if resolved:
                    workspace_id = resolved
            except Exception:
                pass
            if not is_subagent_dispatch_enabled(
                user_id=user_id,
                role_id=subagent_name,
                workspace_id=workspace_id,
                explicit_enabled_role_ids=normalized_enabled_expert_role_ids,
            ):
                raise ValueError(
                    f"协作专家 '{subagent_name}' 未启用到我的默认或当前工作区，不能派发。"
                )
            subagent_manifest = load_subagent_for_runtime(
                user_id=user_id,
                name=subagent_name,
                session_id=session_id,
                workspace_id=workspace_id,
            )
        if subagent_manifest is None:
            available = list((host_agent_config.get("subagents") or {}).keys())
            raise ValueError(f"未找到子 Agent '{subagent_name}'。可用: {available}")

        # 2. 生成唯一 agent_id
        agent_id = f"{subagent_name}_{uuid.uuid4().hex[:12]}"
        current_path = AgentPath.parse(str(ctx.get("agent_path") or "/root"))
        collaboration_policy = _resolve_collaboration_policy(ctx)
        effective_max_depth = int(collaboration_policy["max_depth"])
        allow_nested_spawn = bool(collaboration_policy["allow_nested_spawn"])
        registry = get_subagent_registry()
        raw_max_threads = collaboration_policy.get("max_threads")
        max_threads = (
            raw_max_threads if isinstance(raw_max_threads, int) and raw_max_threads > 0 else None
        )
        try:
            child_path = current_path.ensure_child_allowed(
                max_depth=effective_max_depth,
                child_agent_id=agent_id,
            )
        except ValueError as exc:
            raise ValueError(str(exc))
        parent_agent_id = (
            str(current_path.current_agent_id or ctx.get("parent_agent_id") or "") or None
        )
        child_allow_spawn = allow_nested_spawn and child_path.depth < effective_max_depth

        # 2a. 预检查并发限制
        if max_threads is not None:
            active_count = await registry.acount_active_for_host(host_session_id)
            if active_count >= max_threads:
                raise ValueError(
                    f"当前会话协作节点并发数已达到上限 {max_threads}，请等待已有节点完成后再派发。"
                )

        parent_tool_call_id = str(
            ctx.get("_tool_call_id") or kwargs.get("_tool_call_id") or uuid.uuid4().hex[:12]
        )
        effective_model = subagent_manifest.get("model")
        if not effective_model:
            llm_config = ctx.get("llm_config")
            if llm_config and hasattr(llm_config, "task_models"):
                task_models = llm_config.task_models
                subagent_model = task_models.get("subagent")
                if subagent_model:
                    available = set(getattr(llm_config, "models", {}).keys())
                    if subagent_model in available:
                        effective_model = subagent_model
        nickname = None
        nickname_pool = subagent_manifest.get("agent_nickname_pool")
        if isinstance(nickname_pool, list) and nickname_pool:
            nickname = str(nickname_pool[0] or "").strip() or None

        # 3. 创建子 Agent storage 工作区
        storage = SubAgentStorage(user_id, host_session_id, agent_id)
        storage.create_workspace(
            parent_tool_call_id=parent_tool_call_id,
            subagent_type=subagent_name,
            description=description or f"子 Agent: {subagent_name}",
            effective_model=effective_model,
            model_override=effective_model,
            host_session_id=host_session_id,
            parent_agent_id=parent_agent_id,
            agent_path=str(child_path),
            depth=child_path.depth,
            nickname=nickname,
        )

        # 4. 决定工具、MCP、Skill 继承策略
        tool_policy = subagent_manifest.get("tool_policy") or "inherit"
        fork_turns = subagent_manifest.get("fork_turns")

        if not subagent_manifest.get("tools"):
            if tool_policy == "allowlist":
                default_tools = get_role_type_default_tools(subagent_name)
                if default_tools:
                    subagent_manifest["tools"] = list(default_tools)
                    subagent_manifest["allowed_tools"] = list(default_tools)
            elif tool_policy in ("inherit", "denylist") and not ctx.get("parent_registry"):
                default_tools = get_role_type_default_tools(subagent_name)
                if default_tools:
                    subagent_manifest["tools"] = list(default_tools)

        universal_excludes = get_subagent_universal_excludes()
        if universal_excludes:
            existing_excludes = set(subagent_manifest.get("exclude_tools") or [])
            merged_excludes = existing_excludes | set(universal_excludes)
            subagent_manifest["exclude_tools"] = list(merged_excludes)

        subagent_toml_path = await asyncio.to_thread(
            _materialize_subagent_toml,
            subagent_manifest,
            subagent_name,
            str(storage.subagent_dir),
        )

        mcp_policy = subagent_manifest.get("mcp_policy") or "none"
        mcp_servers = subagent_manifest.get("mcp_servers") or []
        host_mcp_configs = ctx.get("mcp_configs")
        child_mcp_configs = _filter_mcp_configs(host_mcp_configs, mcp_policy, mcp_servers)

        skill_policy = subagent_manifest.get("skill_policy") or "inherit"
        skills = subagent_manifest.get("skills") or []
        child_skills_dir = _resolve_skills_dir(workspace, skill_policy, skills)

        host_messages = ctx.get("messages") or []
        host_budget = ctx.get("budget")

        # 6. 创建 RuntimeSessionCreateSpec
        from app.core.workspace_path import WorkspacePath

        shared_work_dir = WorkspacePath(str(session_root))
        spec = RuntimeSessionCreateSpec(
            work_dir=shared_work_dir,
            session_id=agent_id,
            user_id=user_id,
            config=host_llm_config,
            agent_file=subagent_toml_path,
            skills_dir=WorkspacePath(str(child_skills_dir)) if child_skills_dir else None,
            authorization_mode=str(ctx.get("authorization_mode") or "smart"),
            yolo=bool(ctx.get("yolo", False)),
            mcp_configs=child_mcp_configs,
            is_subagent=True,
            parent_registry=ctx.get("parent_registry"),
            tool_policy=tool_policy,
            fork_turns=fork_turns,
            fork_messages=host_messages,
            host_session_id=host_session_id,
            parent_agent_id=parent_agent_id,
            agent_path=str(child_path),
            agent_max_depth=effective_max_depth,
            allow_subagent_spawn=child_allow_spawn,
            collaboration_policy=collaboration_policy,
            budget=host_budget,
            memory_enabled=False,
            write_allow_root=write_allow_root,
        )

        # 6. 创建子 Agent session
        backend = AiasysRuntimeBackend()
        subagent_session: AiasysRuntimeSession | None = None
        try:
            subagent_session = await backend.create_session(spec)
        except Exception as exc:
            raise ValueError(f"创建子 Agent session 失败: {exc}")

        # 8. 注册到运行时注册表
        registered = await registry.try_register(
            agent_id,
            subagent_session,
            host_session_id=host_session_id,
            max_threads=max_threads,
        )
        if not registered:
            raise ValueError(
                f"当前会话协作节点并发数已达到上限 {max_threads}，请等待已有节点完成后再派发。"
            )

        full_launch_spec = {
            "agent_id": agent_id,
            "subagent_name": subagent_name,
            "host_session_id": host_session_id,
            "user_id": user_id,
            "storage_path": str(storage.subagent_dir),
            "subagent_toml_path": str(subagent_toml_path),
            "effective_model": effective_model,
            "parent_tool_call_id": parent_tool_call_id,
            "parent_agent_id": parent_agent_id,
            "child_path": str(child_path),
            "nickname": nickname,
            "description": description,
            "max_threads": max_threads,
            "timeout_seconds": collaboration_policy.get("timeout_policy", {}).get(
                "default_seconds", 300
            ),
            "llm_config": getattr(
                subagent_session._spec.config, "model_dump", lambda mode="json": {}
            )(mode="json"),
            "agent_file": str(subagent_toml_path),
            "session_root": str(session_root),
            "child_skills_dir": str(child_skills_dir) if child_skills_dir else None,
            "collaboration_policy": collaboration_policy,
            "authorization_mode": str(ctx.get("authorization_mode") or "smart"),
            "yolo": bool(ctx.get("yolo", False)),
            "mcp_configs": child_mcp_configs,
            "tool_policy": tool_policy,
            "agent_path": str(child_path),
            "write_allow_root": write_allow_root,
        }
        storage.update_launch_spec(full_launch_spec)
        await registry.aset_launch_spec(agent_id, full_launch_spec)

        return (
            user_id,
            host_session_id,
            workspace,
            session_root,
            host_agent_config,
            host_llm_config,
            subagent_manifest,
            agent_id,
            child_path,
            parent_agent_id,
            child_allow_spawn,
            max_threads,
            registry,
            parent_tool_call_id,
            effective_model,
            nickname,
            storage,
            tool_policy,
            fork_turns,
            subagent_toml_path,
            child_mcp_configs,
            child_skills_dir,
            spec,
            subagent_session,
            full_launch_spec,
        )


class AgentTool(TaskTool):
    """AIASys 原生子 Agent 调度工具（Agent 名称变体）。

    为了保持 LLM 兼容性，同时注册 Task 和 Agent 两个名称，
    底层实现与 TaskTool 完全一致。
    """

    name = "Agent"
    description = (
        "创建并运行一个专门的子 Agent 来执行任务。"
        "参数: subagent_name(子Agent名称, 可选, 默认coder), description(任务简述), prompt(完整指令)"
    )
    parameters = _TASK_PARAMETERS
