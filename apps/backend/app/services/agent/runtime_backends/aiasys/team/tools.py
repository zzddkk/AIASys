"""Multi-agent team collaboration tools (step 2: tool layer).

Seven tools: team_init / team_plan / team_status / team_send / team_inbox / team_merge / team_teardown
Step 3 adds: team_spawn (background subagent spawn with fail-closed write guard).

移植自 step-code src/agent/team/store.ts（MIT, Copyright (c) 2026 stepfun-ai）
参考 kimi-code tower 实现（tpoisonooo/feat-cowork 分支）
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.agent_tool import AiasysTool
from app.core.tool_result import ToolResult
from app.services.agent.agent_path import AgentPath
from app.services.agent.runtime_backends.aiasys.team.store import (
    TeamError,
    TeamMission,
    TeamStore,
    _normalize_scope,
    _resolve_real_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_DASH = re.compile(r"-+")


def _sanitize_filename_component(s: str, max_len: int = 40) -> str:
    """Sanitize a string for use as a filename component (cross-platform safe)."""
    s = _WINDOWS_INVALID_CHARS.sub("-", s)
    s = _MULTI_DASH.sub("-", s).strip("-")
    return s[:max_len] or "unknown"


def _require_controller(ctx: dict[str, Any] | None) -> str | None:
    """Check that the caller is the controller (depth == 0). Returns error message or None."""
    agent_path_str = str(ctx.get("agent_path") or "/root") if ctx else "/root"
    path = AgentPath.parse(agent_path_str)
    if path.depth != 0:
        return (
            f"此工具仅主控可调用（当前 agent_path={agent_path_str}，depth={path.depth}）。"
            "请通过主控协调者发起操作。"
        )
    return None


def _make_tool_result(content: str, is_error: bool = False) -> ToolResult:
    return ToolResult(content=content, is_error=is_error)


# ---------------------------------------------------------------------------
# TeamStore factory
# ---------------------------------------------------------------------------

# Module-level cache: state_dir -> TeamStore instance
_store_cache: dict[str, TeamStore] = {}
_store_cache_lock = threading.Lock()

# Test override: set to a specific path to bypass _resolve_team_state_dir resolution
_team_state_dir_override: str | None = None


def clear_store_cache() -> None:
    """Clear the TeamStore cache (for testing)."""
    _store_cache.clear()


def set_team_state_dir_override(path: str | None) -> None:
    """Override the team state directory (for testing)."""
    global _team_state_dir_override
    _team_state_dir_override = path


async def _get_store(state_dir: str) -> TeamStore:
    """Get or create a TeamStore for the given state directory.

    双重检查加锁（double-checked locking）：快路径无锁读缓存，未命中才加锁并再查
    一次。这里必须用上 _store_cache_lock —— TeamStore 的互斥靠实例级
    `self._lock`，所以「同一 state_dir 只有一个实例」是租约互斥和 lost update
    防护的前提。一旦并发下建出两个实例，就有两把互不相干的锁，两个 worker 会同时
    认为自己独占了同一个 notebook，而且失效是静默的（不抛异常、不报错）。

    2026-08-09 修复：此前这个函数完全没用锁，靠「if 判断与赋值之间没有 await
    point、asyncio 单线程不抢占」侥幸原子（_store_cache_lock 声明了却零处引用）。
    那样的安全是脆的：任何人在这两行之间插入一个 await（例如给 TeamStore 加异步
    的目录初始化），就会立刻退化成双实例，而现有测试全都察觉不到。

    锁类型是 threading.Lock 而非 asyncio.Lock，这一点是刻意的：_store_cache 是
    模块级全局、跨事件循环存活，而 asyncio.Lock 会在首次真实 acquire 时绑定当时
    的事件循环，之后换循环 acquire 就抛「attached to a different loop」。本临界区
    是纯内存操作、不含 await，用 threading.Lock 语义正确、跨循环安全，还顺带防住
    了从 to_thread 里调进来的真实多线程竞争。详见
    tests/test_event_loop_affinity.py 的模块头注释。
    不变式由 tests/test_team_concurrency.py::TestStoreCacheSingleton 钉住。
    """
    cache_key = state_dir
    if cache_key in _store_cache:
        return _store_cache[cache_key]
    with _store_cache_lock:
        # 二次检查：等锁期间可能已有别的协程/线程建好了实例
        if cache_key in _store_cache:
            return _store_cache[cache_key]
        store = TeamStore(cache_key)
        _store_cache[cache_key] = store
        return store


def _resolve_team_state_dir(ctx: dict[str, Any] | None) -> str:
    """Resolve the team state directory from context."""
    if _team_state_dir_override is not None:
        return _team_state_dir_override
    user_id = str(ctx.get("user_id") or "default") if ctx else "default"
    # Store under user's global workspace
    try:
        from app.core.config import get_user_global_workspace_dir

        workspace = get_user_global_workspace_dir(user_id)
        return str(workspace / ".team" / "state")
    except Exception:
        # Fallback to temp dir
        fallback = os.path.join(tempfile.gettempdir(), f"aiasys-team-{user_id}")
        return fallback


# ---------------------------------------------------------------------------
# team_init
# ---------------------------------------------------------------------------


class TeamInitTool(AiasysTool):
    """初始化团队协作工作区。

    幂等：已初始化则保留全部状态（重进已关闭团队时清关闭标记）。
    自动创建状态目录与必要的子目录。

    仅主控可调用。
    """

    name = "team_init"
    description = (
        "初始化多 Agent 团队协作工作区。幂等操作——已初始化则直接返回现有状态。"
        "参数: repo_root(仓库根目录, 可选, 缺省取当前工作目录), base(基准分支, 可选, 缺省 main)。"
        "仅主控可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "repo_root": {
                "type": "string",
                "description": "仓库根目录绝对路径。省略时取当前工作目录。",
            },
            "base": {
                "type": "string",
                "description": "基准分支名。省略时默认 main。",
            },
        },
    }
    risk_level = "medium"
    effect_scope = "workspace"
    side_effect = True
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        if err := _require_controller(ctx):
            return _make_tool_result(err, is_error=True)

        repo_root = str(kwargs.get("repo_root") or "").strip()
        if not repo_root:
            try:
                repo_root = os.getcwd()
            except OSError:
                repo_root = "."

        base = str(kwargs.get("base") or "").strip() or "main"

        try:
            repo_root_resolved = _resolve_real_path(repo_root)
        except (OSError, ValueError):
            repo_root_resolved = repo_root

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        try:
            state = await store.init(repo_root=repo_root_resolved, base=base)
        except TeamError as exc:
            return _make_tool_result(f"team_init 失败: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("team_init 未预期错误")
            return _make_tool_result(f"team_init 失败: {exc}", is_error=True)

        missions = [
            {
                "id": m.id,
                "title": m.title,
                "kind": m.kind,
                "status": m.status,
                "scope": m.scope,
                "deps": m.deps,
                "owner": m.owner,
            }
            for m in state.missions
        ]
        return _make_tool_result(
            f"团队工作区已初始化。\n"
            f"- 仓库: {state.repo_root}\n"
            f"- 基准分支: {state.base}\n"
            f"- 已有任务: {len(missions)} 个\n"
            f"- 状态目录: {state_dir}\n"
            f"- 任务列表: {missions}"
        )


# ---------------------------------------------------------------------------
# team_plan
# ---------------------------------------------------------------------------


class TeamPlanTool(AiasysTool):
    """规划任务清单。

    系统强制检查：
    1. deps 必须引用已存在的任务 id
    2. build 类 scope 必须两两不重叠（survey 不占位，允许任意重叠）
    3. 路径用 os.path.realpath() 规范化，按路径分量前缀比对

    仅主控可调用。
    """

    name = "team_plan"
    description = (
        "规划一批团队任务。系统强制检查依赖和 scope 互斥。"
        "参数: missions(任务列表), 每个任务含 title, kind(build|survey), scope(路径前缀列表, 可选), deps(依赖id列表, 可选)。"
        "返回分配后的任务 id 列表（M1, M2, ...）。仅主控可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "missions": {
                "type": "array",
                "description": "任务列表。每项: title(必填), kind(build|survey, 必填), scope(路径前缀数组, build必填), deps(任务id数组, 可选)。",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "任务标题"},
                        "kind": {
                            "type": "string",
                            "enum": ["build", "survey"],
                            "description": "任务类型: build产生新产出(survey只读调查)",
                        },
                        "scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "文件路径前缀列表（build 类必填，survey 可省略）",
                        },
                        "deps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "依赖的任务 id 列表",
                        },
                    },
                    "required": ["title", "kind"],
                },
            },
        },
        "required": ["missions"],
    }
    risk_level = "medium"
    effect_scope = "workspace"
    side_effect = True
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        if err := _require_controller(ctx):
            return _make_tool_result(err, is_error=True)

        raw_missions = kwargs.get("missions") or []
        if not isinstance(raw_missions, list) or len(raw_missions) == 0:
            return _make_tool_result("missions 参数必须是非空数组。", is_error=True)

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        missions_to_plan: list[TeamMission] = []
        for idx, item in enumerate(raw_missions):
            if not isinstance(item, dict):
                return _make_tool_result(f"missions[{idx}] 必须是对象。", is_error=True)

            title = str(item.get("title", "")).strip()
            if not title:
                return _make_tool_result(f"missions[{idx}].title 不能为空。", is_error=True)

            kind = str(item.get("kind", "")).strip().lower()
            if kind not in ("build", "survey"):
                return _make_tool_result(
                    f"missions[{idx}].kind 必须是 build 或 survey，收到: {kind}",
                    is_error=True,
                )

            scope_raw = item.get("scope") or []
            if not isinstance(scope_raw, list):
                scope_raw = [str(scope_raw)]
            scope = [str(s).strip() for s in scope_raw if str(s).strip()]

            if kind == "build" and len(scope) == 0:
                return _make_tool_result(
                    f"missions[{idx}].scope 不能为空（build 类任务必须声明产出范围）。",
                    is_error=True,
                )

            deps_raw = item.get("deps") or []
            if not isinstance(deps_raw, list):
                deps_raw = [str(deps_raw)]
            deps = [str(d).strip() for d in deps_raw if str(d).strip()]

            # lease 是可选的资源租约声明
            lease_raw = item.get("lease")
            lease: dict[str, Any] | None = None
            if isinstance(lease_raw, dict):
                lease = {k: v for k, v in lease_raw.items() if v is not None}

            missions_to_plan.append(
                TeamMission(
                    id=f"__pending_{idx}__",
                    title=title,
                    kind=kind,
                    scope=scope,
                    deps=deps,
                    status="planned",
                    lease=lease,
                )
            )

        try:
            result = await store.plan(missions_to_plan)
        except TeamError as exc:
            return _make_tool_result(f"team_plan 失败: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("team_plan 未预期错误")
            return _make_tool_result(f"team_plan 失败: {exc}", is_error=True)

        output = [
            f"已规划 {len(result)} 个任务：",
            "",
        ]
        for m in result:
            output.append(
                f"- {m.id}: {m.title} [{m.kind}] "
                f"scope={m.scope or '(无)'} deps={m.deps or '(无)'} status={m.status}"
            )
        return _make_tool_result("\n".join(output))


# ---------------------------------------------------------------------------
# team_status
# ---------------------------------------------------------------------------


class TeamStatusTool(AiasysTool):
    """查询或变更任务状态。

    查询模式（不传 new_status）：返回任务详情，任何 agent 可调用。
    变更模式（传 new_status）：仅主控可调用，走状态机门控。

    合法迁移：
    planned → active / blocked / paused
    active → completed / blocked / paused
    blocked → active / merged
    paused → active
    completed → active / merged
    """

    name = "team_status"
    description = (
        "查询或变更任务状态。不传 new_status 时查询（返回全部或指定任务详情），"
        "传入 new_status 时变更状态（仅主控）。"
        "参数: mission_id(任务id, 查询单个时必填), new_status(目标状态, 可选)。"
        "状态: planned / active / completed / blocked / paused / merged。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "mission_id": {
                "type": "string",
                "description": "任务 id（如 M1）。查询全部时省略。",
            },
            "new_status": {
                "type": "string",
                "description": "目标状态（planned/active/completed/blocked/paused/merged）。省略时仅查询。",
            },
        },
    }
    risk_level = "low"
    effect_scope = "workspace"
    side_effect = True  # new_status 变更会写盘
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        new_status = str(kwargs.get("new_status") or "").strip()
        mission_id = str(kwargs.get("mission_id") or "").strip()

        # If changing status, require controller
        if new_status:
            if err := _require_controller(ctx):
                return _make_tool_result(err, is_error=True)

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        try:
            state = await store.load()
        except TeamError as exc:
            return _make_tool_result(str(exc), is_error=True)

        if mission_id:
            # Single mission query or status change
            mission = next((m for m in state.missions if m.id == mission_id), None)
            if mission is None:
                return _make_tool_result(f"任务 {mission_id} 不存在。", is_error=True)

            if new_status:
                try:
                    updated = await store.set_status(mission_id, new_status)
                except TeamError as exc:
                    return _make_tool_result(str(exc), is_error=True)
                except Exception as exc:
                    logger.exception("team_status set 未预期错误")
                    return _make_tool_result(f"team_status 失败: {exc}", is_error=True)
                return _make_tool_result(
                    f"任务 {updated.id} 状态已更新: {mission.status} → {updated.status}"
                )

            # Query single
            return _make_tool_result(
                f"任务 {mission.id}: {mission.title}\n"
                f"  类型: {mission.kind}\n"
                f"  状态: {mission.status}\n"
                f"  范围: {mission.scope or '(无)'}\n"
                f"  依赖: {mission.deps or '(无)'}\n"
                f"  执行者: {mission.owner or '(未分配)'}"
            )

        # Query all missions
        if not state.missions:
            return _make_tool_result("团队尚无任务。请先使用 team_plan 规划任务。")

        lines = [f"团队任务概览（共 {len(state.missions)} 个）：", ""]
        for m in state.missions:
            lines.append(
                f"- {m.id}: {m.title} [{m.kind}] → {m.status} "
                f"scope={m.scope or '-'} owner={m.owner or '-'}"
            )
        return _make_tool_result("\n".join(lines))


# ---------------------------------------------------------------------------
# team_send
# ---------------------------------------------------------------------------


class TeamSendTool(AiasysTool):
    """发送消息到团队信箱。

    消息以 markdown 文件落盘，可审计。
    to 支持 'all' 广播。
    文件名含时间戳前缀，保证 newest-first 排序；跨平台安全（过滤 Windows 非法字符）。

    主控与 worker 均可调用。
    """

    name = "team_send"
    description = (
        "发送消息到团队信箱。消息以 markdown 文件落盘，可审计。"
        "参数: from(发送者名称), to(接收者名称或 'all' 广播), subject(主题), body(正文)。"
        "主控与 worker 均可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "from": {
                "type": "string",
                "description": "发送者名称（建议用 agent 名称或 agent_id）。",
            },
            "to": {
                "type": "string",
                "description": "接收者名称，或 'all' 广播给所有人。",
            },
            "subject": {
                "type": "string",
                "description": "消息主题。",
            },
            "body": {
                "type": "string",
                "description": "消息正文（markdown 格式）。",
            },
        },
        "required": ["from", "to", "subject", "body"],
    }
    risk_level = "low"
    effect_scope = "workspace"
    side_effect = True
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}

        # 'from' is a Python reserved word; accept from_ and map it
        sender = str(kwargs.get("from") or kwargs.get("from_") or "").strip()
        recipient = str(kwargs.get("to") or "").strip()
        subject = str(kwargs.get("subject") or "").strip()
        body = str(kwargs.get("body") or "").strip()

        if not sender:
            return _make_tool_result("from 参数不能为空。", is_error=True)
        if not recipient:
            return _make_tool_result("to 参数不能为空。", is_error=True)
        if not subject:
            return _make_tool_result("subject 参数不能为空。", is_error=True)

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        # Ensure team is initialized
        try:
            await store.load()
        except TeamError:
            return _make_tool_result("team 尚未初始化——先运行 team_init。", is_error=True)

        inbox_dir = Path(state_dir) / "comms" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        date_prefix = now.strftime("%Y%m%d")
        ts_prefix = now.strftime("%H%M%S%f")  # microsecond precision for sorting

        safe_from = _sanitize_filename_component(sender)
        safe_to = _sanitize_filename_component(recipient)
        safe_subject = _sanitize_filename_component(subject)

        filename = f"{date_prefix}-{ts_prefix}-{safe_from}-to-{safe_to}-{safe_subject}.md"
        filepath = inbox_dir / filename

        content = "\n".join(
            [
                "---",
                f"message_id: {now.strftime('%Y%m%d%H%M%S%f')}-{os.urandom(4).hex()}",
                f"from: {sender}",
                f"to: {recipient}",
                f"subject: {subject}",
                f"sent_at: {now.isoformat()}",
                "---",
                "",
                body,
                "",
            ]
        )

        try:
            # Atomic write for safety
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(inbox_dir), suffix=".tmp", prefix=".msg-")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, str(filepath))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as exc:
            logger.exception("team_send 写入失败")
            return _make_tool_result(f"team_send 失败: {exc}", is_error=True)

        return _make_tool_result(
            f"消息已发送。\n- 文件: {filename}\n- 从: {sender} → 到: {recipient}\n- 主题: {subject}"
        )


# ---------------------------------------------------------------------------
# team_inbox
# ---------------------------------------------------------------------------


class TeamInboxTool(AiasysTool):
    """读取团队信箱。

    newest-first 排序。
    - 主控（depth==0）可以看到全部消息。
    - worker（depth>0）只能看到发给自己的与广播（to=all）消息。

    主控与 worker 均可调用（有可见性限制）。
    """

    name = "team_inbox"
    description = (
        "读取团队信箱。newest-first 排序。"
        "主控可看全部消息；worker 只能看发给自己的与广播消息。"
        "参数: name(当前 agent 名称, 主控用 'team' 看全部), limit(最多返回条数, 默认 20)。"
        "主控与 worker 均可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "当前 agent 名称。主控用 'team' 查看全部消息，worker 用自己的名称查看发给自己的消息。",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数（默认 20）。",
            },
        },
    }
    risk_level = "readonly"
    effect_scope = "workspace"
    side_effect = False
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}

        name = str(kwargs.get("name") or "").strip()
        limit = int(kwargs.get("limit") or 20)
        limit = max(1, min(limit, 200))  # clamp

        if not name:
            return _make_tool_result("name 参数不能为空（主控用 'team'）。", is_error=True)

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        try:
            messages = await store.inbox(name, limit=limit)
        except TeamError as exc:
            return _make_tool_result(str(exc), is_error=True)
        except Exception as exc:
            logger.exception("team_inbox 未预期错误")
            return _make_tool_result(f"team_inbox 失败: {exc}", is_error=True)

        if not messages:
            return _make_tool_result("信箱为空。")

        lines = [f"信箱消息（最近 {len(messages)} 条，newest-first）：", ""]
        for msg in messages:
            lines.append(f"### {msg['subject']}")
            lines.append(f"- 从: {msg['from']} | 到: {msg['to']}")
            lines.append(f"- 时间: {msg['sent_at']}")
            lines.append(f"- 文件: {msg['file']}")
            lines.append("")
            lines.append(msg["body"])
            lines.append("")
            lines.append("---")
            lines.append("")

        return _make_tool_result("\n".join(lines))


# ---------------------------------------------------------------------------
# team_merge
# ---------------------------------------------------------------------------


class TeamMergeTool(AiasysTool):
    """收编（合并）任务。

    走 store 六道门（全部在 store 层执行，工具层只做参数校验与转发）：
    门〇：空产出检查（artifacts 为空列表则拒绝）
    门①：已审阅（reviewed_commit 不能为空）
    门②：审阅结论干净（调用方责任，本层只传参）
    门③：产出物指纹未变（fingerprint 比对）
    门④：依赖已收编（deps 全部 merged）
    门⑤：无范围外产出（逐产出物比对 lease）

    仅主控可调用。
    """

    name = "team_merge"
    description = (
        "收编（合并）已完成任务。走六道门硬控。"
        "参数: mission_id(任务id), reviewed_commit(审阅时的产出物指纹), artifacts(产出物清单, 可选)。"
        '产出物清单格式: [{"path": "...", "size": 123, "hash": "..."}]。'
        "仅主控可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "mission_id": {
                "type": "string",
                "description": "要收编的任务 id（如 M1）。",
            },
            "reviewed_commit": {
                "type": "string",
                "description": "审阅时的产出物指纹（字符串，用于门①和门③比对）。",
            },
            "artifacts": {
                "type": "array",
                "description": "产出物清单（用于门〇/③/⑤）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "产出物路径"},
                        "size": {"type": "integer", "description": "文件大小（字节）"},
                        "hash": {"type": "string", "description": "内容 hash"},
                    },
                },
            },
        },
        "required": ["mission_id", "reviewed_commit"],
    }
    risk_level = "high"
    effect_scope = "workspace"
    side_effect = True
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        if err := _require_controller(ctx):
            return _make_tool_result(err, is_error=True)

        mission_id = str(kwargs.get("mission_id") or "").strip()
        raw_reviewed_commit = kwargs.get("reviewed_commit")
        reviewed_commit = (
            str(raw_reviewed_commit).strip() if raw_reviewed_commit is not None else None
        )
        raw_artifacts = kwargs.get("artifacts") or []

        if not mission_id:
            return _make_tool_result("mission_id 不能为空。", is_error=True)

        # Normalize artifacts (keep empty list for gate 0 check in store)
        artifacts: list[dict[str, Any]] = []
        if isinstance(raw_artifacts, list):
            for art in raw_artifacts:
                if isinstance(art, dict):
                    artifacts.append(
                        {
                            "path": str(art.get("path", "")),
                            "size": art.get("size", 0),
                            "hash": str(art.get("hash", "")),
                        }
                    )

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        try:
            result = await store.merge(
                mission_id=mission_id,
                reviewed_commit=reviewed_commit,
                artifacts=artifacts,
            )
        except TeamError as exc:
            return _make_tool_result(f"team_merge 失败: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("team_merge 未预期错误")
            return _make_tool_result(f"team_merge 失败: {exc}", is_error=True)

        conflicts = result.get("conflictsWith", [])
        parts = [
            f"任务 {mission_id} 已收编。",
            f"波及检测：以下未合并任务的 scope 与本次产出物重叠，需要重新审阅：{conflicts}"
            if conflicts
            else "无波及冲突。",
        ]
        return _make_tool_result("\n".join(parts))


# ---------------------------------------------------------------------------
# team_teardown
# ---------------------------------------------------------------------------


class TeamTeardownTool(AiasysTool):
    """收尾：关闭团队并清理工作状态。

    1. 标记团队已关闭（防止 resume 复活）
    2. 清理工作间（dirty 的默认保留，force=True 时强制清理）
    3. 状态目录与日志永久保留（可审计）

    仅主控可调用。
    """

    name = "team_teardown"
    description = (
        "收尾关闭团队。标记关闭、清理工作间（dirty 默认保留）。"
        "参数: force(是否强制清理 dirty 工作间, 默认 false)。"
        "状态目录与日志永久保留（可审计）。仅主控可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "是否强制清理有未提交改动的任务工作间（默认 false，保留 dirty 工作间）。",
            },
        },
    }
    risk_level = "high"
    effect_scope = "workspace"
    side_effect = True
    dangerous = True

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        if err := _require_controller(ctx):
            return _make_tool_result(err, is_error=True)

        force = bool(kwargs.get("force", False))

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        try:
            result = await store.teardown(force=force)
        except TeamError as exc:
            return _make_tool_result(f"team_teardown 失败: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("team_teardown 未预期错误")
            return _make_tool_result(f"team_teardown 失败: {exc}", is_error=True)

        removed = result.get("removed", [])
        kept = result.get("kept", [])

        parts = [
            "团队已关闭。",
            f"- 已清理工作间: {len(removed)} 个",
            f"- 保留工作间: {len(kept)} 个",
        ]
        if removed:
            parts.append(f"  已清理: {', '.join(removed)}")
        if kept:
            parts.append(f"  保留原因: {'; '.join(kept)}")

        return _make_tool_result("\n".join(parts))


# ---------------------------------------------------------------------------
# Feature flag: write guard readiness
# ---------------------------------------------------------------------------

# 写范围守卫（Per-Agent Write Allow Root）已实现并通过测试。
# 设为 True 后，team_spawn 可派生 kind=="build" 的写类任务。
# 守卫在 session_stream._execute_write_tool 中拦截：worker 写入超出 scope
# 范围的文件会被硬拒绝（不进审批流、不问用户）。
# 已知缺口：Shell 工具的命令级路径无法被守卫覆盖（应用层限制）。
TEAM_SPAWN_WRITE_GUARD_READY: bool = True


# ---------------------------------------------------------------------------
# team_spawn
# ---------------------------------------------------------------------------


class TeamSpawnTool(AiasysTool):
    """派生子 Agent 执行团队任务（后台模式）。

    工作流程：
    1. 校验调用方是主控（depth==0）。
    2. 校验 mission 存在且依赖全部 merged（走 store 门控）。
    3. 写范围守卫检查：守卫未就绪时拒绝 build 类任务。
    4. 调用 TaskTool(background=True) 派生子 Agent。
    5. 将 mission 切到 active，owner 设为 task_id。

    仅主控可调用。
    risk_level=high：派生子 Agent 属于高影响操作，可能触发 LLM API 调用和文件写入。
    effect_scope=workspace：子 Agent 在团队工作区内执行。
    side_effect=True：会改变 mission 状态、创建子 Agent 会话。
    dangerous=False：不直接删除数据（但派生 build 任务可写文件，需守卫控制）。
    """

    name = "team_spawn"
    description = (
        "派生子 Agent 执行团队任务（后台模式，立即返回 task_id）。"
        "系统强制检查：依赖未全部 merged 时拒绝启动。"
        "写范围守卫未就绪时仅允许 survey 类任务。"
        "参数: mission_id(任务id), prompt(给子Agent的完整指令), subagent_type(子Agent类型, 可选, 默认coder)。"
        "返回: task_id（用于后续查询状态）。仅主控可调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "mission_id": {
                "type": "string",
                "description": "要执行的任务 id（如 M1）。",
            },
            "prompt": {
                "type": "string",
                "description": "给子 Agent 的完整任务指令。",
            },
            "subagent_type": {
                "type": "string",
                "description": "子 Agent 类型（如 coder, researcher, reviewer）。省略时默认 coder。",
            },
        },
        "required": ["mission_id", "prompt"],
    }
    risk_level = "high"
    effect_scope = "workspace"
    side_effect = True
    dangerous = False

    async def invoke(
        self,
        ctx: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ctx = ctx or {}
        if err := _require_controller(ctx):
            return _make_tool_result(err, is_error=True)

        mission_id = str(kwargs.get("mission_id") or "").strip()
        prompt = str(kwargs.get("prompt") or "").strip()
        subagent_type = str(kwargs.get("subagent_type") or "").strip() or "coder"

        if not mission_id:
            return _make_tool_result("mission_id 不能为空。", is_error=True)
        if not prompt:
            return _make_tool_result("prompt 不能为空。", is_error=True)

        state_dir = _resolve_team_state_dir(ctx)
        store = await _get_store(state_dir)

        # 1. 加载状态，查找 mission
        try:
            state = await store.load()
        except TeamError as exc:
            return _make_tool_result(f"team_spawn 失败: {exc}", is_error=True)

        mission = next((m for m in state.missions if m.id == mission_id), None)
        if mission is None:
            return _make_tool_result(f"任务 {mission_id} 不存在。", is_error=True)

        # 2. 写范围守卫检查（fail-closed）
        # 守卫未就绪时，拒绝派生 build 类任务（写类任务）。
        # 只允许派生 survey 类任务（只读调查）。
        if mission.kind == "build" and not TEAM_SPAWN_WRITE_GUARD_READY:
            return _make_tool_result(
                "team_spawn 被拒绝：写范围守卫（Per-Agent Write Allow Root）尚未就绪，"
                "无法安全派生 build 类任务（可能写任意路径）。"
                "请先实现写范围守卫，然后将 TEAM_SPAWN_WRITE_GUARD_READY 设为 True。"
                "当前仅允许派生 survey 类（只读）任务。",
                is_error=True,
            )

        # 2a. 计算 write_allow_root（仅 build 类任务需要）
        # 将 mission.scope 中的相对路径拼接为 repo_root 下的绝对路径。
        write_allow_root: list[str] | None = None
        if mission.kind == "build" and mission.scope:
            write_allow_root = []
            for scope_entry in mission.scope:
                scope_norm = _normalize_scope(scope_entry)
                combined = os.path.join(state.repo_root, scope_norm)
                try:
                    resolved = os.path.realpath(combined)
                except (OSError, ValueError):
                    resolved = os.path.abspath(combined)
                write_allow_root.append(resolved)

        # 2b. 解析资源租约（resolve mission lease 声明 → 运行时租约键列表）
        # 对独占资源（notebook）做冲突检测：被其他 mission 持有时直接拒绝。
        # 非独占资源仅登记，不检测冲突。
        resource_lease_keys: list[str] = []
        if mission.kind == "build" and mission.lease:
            try:
                resource_lease_keys = await store.resolve_mission_resource_leases(mission)
            except TeamError as exc:
                return _make_tool_result(f"team_spawn 被拒绝（资源租约冲突）: {exc}", is_error=True)

        # 3. 依赖门控：依赖未全部 merged 拒绝启动
        # （store.set_status("active") 内部已实现该门控，这里直接调用）
        try:
            updated_mission = await store.set_status(mission_id, "active")
        except TeamError as exc:
            return _make_tool_result(f"team_spawn 被拒绝: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("team_spawn set_status 未预期错误")
            return _make_tool_result(f"team_spawn 失败: {exc}", is_error=True)

        # 4. 调用 TaskTool(background=True) 派生子 Agent
        try:
            from app.services.agent.runtime_backends.aiasys.tools.task_tool import TaskTool

            task_tool = TaskTool()
            # 构建子 Agent 所需的 ctx（继承当前 ctx 的关键字段）
            spawn_ctx = {
                "user_id": ctx.get("user_id"),
                "session_id": ctx.get("session_id"),
                "host_session_id": ctx.get("session_id"),
                "workspace": ctx.get("workspace"),
                "session_root": ctx.get("session_root"),
                "agent_path": ctx.get("agent_path", "/root"),
                "agent_config": ctx.get("agent_config") or {},
                "llm_config": ctx.get("llm_config"),
                "parent_registry": ctx.get("parent_registry"),
                "authorization_mode": ctx.get("authorization_mode") or "smart",
                "yolo": ctx.get("yolo", False),
                "mcp_configs": ctx.get("mcp_configs"),
                "collaboration_policy": ctx.get("collaboration_policy"),
                "budget": ctx.get("budget"),
                "messages": ctx.get("messages") or [],
            }
            # 只保留非 None 值
            spawn_ctx = {k: v for k, v in spawn_ctx.items() if v is not None}

            task_results: list[ToolResult] = []
            async for result in task_tool.invoke_stream(
                spawn_ctx,
                subagent_name=subagent_type,
                description=f"Team task {mission_id}: {updated_mission.title}",
                prompt=prompt,
                background=True,
                write_allow_root=write_allow_root,
                resource_lease_keys=resource_lease_keys,
            ):
                task_results.append(result)

            if not task_results:
                return _make_tool_result(
                    "team_spawn 失败: TaskTool 未返回结果。",
                    is_error=True,
                )

            first_result = task_results[0]
            if first_result.is_error:
                # 派生失败，回滚 mission 状态
                try:
                    await store.set_status(mission_id, "planned")
                except Exception as rollback_exc:
                    # 回滚失败会让 mission 卡在非 planned 的中间态，后续 spawn/merge
                    # 全被状态机拒绝——必须留日志让人能发现，不能静默吞掉。
                    logger.error(
                        "team_spawn 失败后回滚 mission 状态失败（mission 可能卡在中间态）: "
                        "mission_id=%s error=%s",
                        mission_id,
                        rollback_exc,
                    )
                return _make_tool_result(
                    f"team_spawn 失败: {first_result.content}",
                    is_error=True,
                )

            # 从 artifacts 中提取 task_id
            task_id = None
            if first_result.artifacts:
                for artifact in first_result.artifacts:
                    if isinstance(artifact, dict) and "task_id" in artifact:
                        task_id = artifact["task_id"]
                        break

            if not task_id:
                # fallback: 从 content 解析
                import re

                # ToolResult.content 的类型是 str | list[dict[str, Any]]（多模态）。
                # 原写法 `first_result.content or ""` 兜不住 list：非空 list 是
                # truthy，`or` 不会替换它，re.search 直接收到 list 抛 TypeError。
                # 底层工具一旦返回多模态结果，team_spawn 就会崩在这里。
                raw_content = first_result.content
                text_content = raw_content if isinstance(raw_content, str) else ""
                match = re.search(r"task_id:\s*(\S+)", text_content)
                task_id = match.group(1) if match else None

            if not task_id:
                return _make_tool_result(
                    "team_spawn 失败: 未能获取 task_id。",
                    is_error=True,
                )

            # 5. 更新 mission owner
            async with store._lock:
                state = await store._load()
                m = next((m for m in state.missions if m.id == mission_id), None)
                if m is not None:
                    m.owner = task_id
                    await store._save(state)

            return _make_tool_result(
                f"任务 {mission_id} 已在后台启动。\n"
                f"- task_id: {task_id}\n"
                f"- subagent_type: {subagent_type}\n"
                f"- 状态: active\n"
                f"- 提示: 使用 team_status(mission_id='{mission_id}') 查询进度"
            )

        except ImportError as exc:
            return _make_tool_result(
                f"team_spawn 失败: 无法加载 TaskTool: {exc}",
                is_error=True,
            )
        except Exception as exc:
            logger.exception("team_spawn 未预期错误")
            # 回滚 mission 状态
            try:
                await store.set_status(mission_id, "planned")
            except Exception as rollback_exc:
                logger.error(
                    "team_spawn 异常后回滚 mission 状态失败（mission 可能卡在中间态）: "
                    "mission_id=%s error=%s",
                    mission_id,
                    rollback_exc,
                )
            return _make_tool_result(f"team_spawn 失败: {exc}", is_error=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ALL_TOOLS: list[type[AiasysTool]] = [
    TeamInitTool,
    TeamPlanTool,
    TeamStatusTool,
    TeamSendTool,
    TeamInboxTool,
    TeamMergeTool,
    TeamTeardownTool,
    TeamSpawnTool,
]

TOOL_CLASSES: dict[str, type[AiasysTool]] = {cls.name: cls for cls in ALL_TOOLS}


def register_all(registry: Any) -> None:
    """Register all team tools into the given ToolRegistry."""
    for cls in ALL_TOOLS:
        try:
            registry.register(cls())
            logger.debug("已注册团队工具: %s", cls.name)
        except ValueError:
            logger.debug("团队工具 %s 已注册，跳过", cls.name)
        except Exception:
            logger.warning("注册团队工具 %s 失败", cls.name, exc_info=True)
