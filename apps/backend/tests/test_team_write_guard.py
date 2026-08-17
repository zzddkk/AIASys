"""Per-Agent Write Allow Root 写范围守卫测试。

覆盖（6 必选 + 1 Shell 已知缺口）：
1. 范围内写放行：WriteFile / StrReplaceFile / CreateFile 在允许范围内写文件被放行
2. 越界写被硬拒绝：写入超出 write_allow_root 范围的文件被硬拒绝（不进审批流）
3. 符号链接指向范围外被拒：通过符号链接尝试写入范围外被拒
4. src/data 范围不放行 src/database：路径分量前缀匹配，src/data 不误匹配 src/database
5. 无 write_allow_root 时完全不受限制：write_allow_root=None 时 WriteFile 放行任意路径
6. 多个 worker 各自范围互不干扰：worker A 不能写 worker B 的范围
7. Shell 工具在守卫激活时被拒绝：Shell 工具的命令级路径无法守卫（已知缺口，fail-closed）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.tool_result import ToolResult
from app.services.agent.runtime_backends.aiasys.team.store import (
    check_write_guard,
)
from app.services.agent.runtime_backends.aiasys.team.tools import (
    TEAM_SPAWN_WRITE_GUARD_READY,
    TeamInitTool,
    TeamPlanTool,
    TeamSpawnTool,
    clear_store_cache,
    set_team_state_dir_override,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_async_gen(*items: Any):
    """把同步 items 包装成 async generator。"""

    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# 1. check_write_guard 单元测试（直接测试核心逻辑）
# ---------------------------------------------------------------------------


class TestCheckWriteGuardUnit:
    """直接测试 check_write_guard() 函数的行为。"""

    def test_no_allow_root_always_passes(self):
        """write_allow_root=None 时放行；空列表 [] 表示 team 场景无允许路径，应拒绝。"""
        assert check_write_guard(None, "WriteFile", {"path": "/anywhere/file.txt"}) is None
        # 空列表表示 team 场景但无允许路径，应拒绝
        result = check_write_guard([], "WriteFile", {"path": "/anywhere/file.txt"})
        assert result is not None
        assert "硬拒绝" in result

    def test_write_within_scope_allowed(self, tmp_path: Path):
        """在允许范围内的写操作被放行（使用真实路径）。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "output.json"
        result = check_write_guard([str(allowed)], "WriteFile", {"path": str(target)})
        assert result is None

    def test_write_outside_scope_denied(self, tmp_path: Path):
        """超出允许范围的写操作被硬拒绝。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = tmp_path / "forbidden" / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = check_write_guard([str(allowed)], "WriteFile", {"path": str(target)})
        assert result is not None
        assert "硬拒绝" in result
        assert "WriteFile" in result

    def test_symlink_outside_scope_denied(self, tmp_path: Path):
        """符号链接指向范围外时，解析后判断为越界。"""
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret")
        link = allowed / "link"
        link.symlink_to(secret)

        # 直接写 outside/secret.txt → 越界
        assert check_write_guard([str(allowed)], "WriteFile", {"path": str(secret)}) is not None

        # 通过符号链接写 allowed/link → realpath 解析为 outside/secret.txt → 越界
        result_link = check_write_guard([str(allowed)], "WriteFile", {"path": str(link)})
        assert result_link is not None, (
            f"符号链接指向范围外，但守卫放行了。"
            f"link realpath={os.path.realpath(str(link))}, allow=[{allowed}]"
        )

    def test_symlink_within_scope_allowed(self, tmp_path: Path):
        """符号链接指向范围内时，解析后判断为合法。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "target.txt"
        target.write_text("ok")
        link = allowed / "link"
        link.symlink_to(target)

        result = check_write_guard([str(allowed)], "WriteFile", {"path": str(link)})
        assert result is None

    def test_src_data_not_match_src_database(self, tmp_path: Path):
        """路径分量前缀匹配：src/data 不误匹配 src/database。"""
        repo = tmp_path / "repo"
        repo.mkdir()
        data_dir = repo / "src" / "data"
        db_dir = repo / "src" / "database"
        data_dir.mkdir(parents=True)
        db_dir.mkdir(parents=True)

        allow = [str(data_dir)]
        # 同根目录的不同子目录 → 不匹配
        db_file = db_dir / "schema.sql"
        result = check_write_guard(allow, "WriteFile", {"path": str(db_file)})
        assert result is not None, "src/data 不应匹配 src/database"

        # 子目录下 → 匹配
        data_file = data_dir / "schema.sql"
        result2 = check_write_guard(allow, "WriteFile", {"path": str(data_file)})
        assert result2 is None

    def test_str_replace_within_scope_allowed(self, tmp_path: Path):
        """StrReplaceFile 在范围内放行。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "readme.md"
        target.write_text("hello")
        result = check_write_guard([str(allowed)], "StrReplaceFile", {"path": str(target)})
        assert result is None

    def test_str_replace_outside_scope_denied(self, tmp_path: Path):
        """StrReplaceFile 越界被拒。"""
        allowed = tmp_path / "allowed"
        other = tmp_path / "other"
        allowed.mkdir()
        other.mkdir()
        target = other / "readme.md"
        target.write_text("hello")
        result = check_write_guard([str(allowed)], "StrReplaceFile", {"path": str(target)})
        assert result is not None

    def test_create_file_within_scope_allowed(self, tmp_path: Path):
        """CreateFile 在范围内放行（创建新文件场景）。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "new_file.py"  # 文件不存在，只检查路径
        result = check_write_guard([str(allowed)], "CreateFile", {"path": str(target)})
        assert result is None

    def test_create_file_outside_scope_denied(self, tmp_path: Path):
        """CreateFile 越界被拒。"""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        result = check_write_guard([str(allowed)], "CreateFile", {"path": "/etc/passwd"})
        assert result is not None

    def test_multiple_allow_roots(self, tmp_path: Path):
        """多个 allow_root 中任意一个匹配即放行。"""
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        allow = [str(root1), str(root2)]
        assert check_write_guard(allow, "WriteFile", {"path": str(root1 / "a.py")}) is None
        assert check_write_guard(allow, "WriteFile", {"path": str(root2 / "b.py")}) is None
        other = tmp_path / "other"
        other.mkdir()
        assert check_write_guard(allow, "WriteFile", {"path": str(other / "c.py")}) is not None

    def test_shell_tool_denied_when_guard_active(self):
        """Shell 工具在守卫激活时被拒绝（已知缺口，fail-closed）。"""
        allow = ["/some/allowed/dir/"]
        result = check_write_guard(allow, "Shell", {"command": "echo hello > /etc/passwd"})
        assert result is not None
        assert "Shell" in result

    def test_shell_tool_passes_when_no_guard(self):
        """无守卫时 Shell 工具不受影响。"""
        result = check_write_guard(None, "Shell", {"command": "echo hello"})
        assert result is None

    def test_unregistered_tool_denied_fail_closed(self):
        """未在 _WRITE_PATH_ARG 注册的写工具被保守拒绝。"""
        allow = ["/some/allowed/dir/"]
        result = check_write_guard(allow, "UnknownWriteTool", {"path": "/some/allowed/dir/x"})
        assert result is not None
        assert "未注册" in result

    def test_missing_path_arg_denied(self):
        """工具缺少路径参数时被拒绝。"""
        allow = ["/some/allowed/dir/"]
        result = check_write_guard(allow, "WriteFile", {})
        assert result is not None
        assert "缺少目标路径" in result


# ---------------------------------------------------------------------------
# 2. team_spawn 集成：write_allow_root 计算与传递
# ---------------------------------------------------------------------------


class TestTeamSpawnWriteAllowRoot:
    """team_spawn 派生 build 类任务时，正确计算并传递 write_allow_root。"""

    @pytest.fixture
    def tmp_state_dir(self, tmp_path: Path) -> str:
        return str(tmp_path / "team_state")

    @pytest.mark.asyncio
    async def test_feature_flag_is_true(self):
        """TEAM_SPAWN_WRITE_GUARD_READY 已打开。"""
        assert TEAM_SPAWN_WRITE_GUARD_READY is True

    @pytest.mark.asyncio
    async def test_build_task_computes_write_allow_root(self, tmp_path: Path, tmp_state_dir: str):
        """team_spawn 为 build 任务计算正确的 write_allow_root。"""
        clear_store_cache()
        set_team_state_dir_override(tmp_state_dir)

        try:
            ctx = {
                "user_id": "test_user",
                "session_id": "test_session",
                "agent_path": "/root",
            }

            init_result = await TeamInitTool().invoke(ctx, repo_root=str(tmp_path), base="main")
            assert not init_result.is_error, f"init failed: {init_result.content}"

            plan_result = await TeamPlanTool().invoke(
                ctx,
                missions=[
                    {
                        "title": "Build task",
                        "kind": "build",
                        "scope": ["src/data/", "src/models/"],
                    }
                ],
            )
            assert not plan_result.is_error, f"plan failed: {plan_result.content}"

            import re

            match = re.search(r"M\d+", plan_result.content or "")
            assert match, f"Could not find mission id in: {plan_result.content}"
            mission_id = match.group(0)

            # 调用 team_spawn，验证 write_allow_root 被计算并传入 TaskTool
            from app.services.agent.runtime_backends.aiasys.tools.task_tool import TaskTool

            spawn_tool = TeamSpawnTool()
            captured_kwargs: dict = {}

            async def fake_invoke_stream(self_inner, ctx_inner, **kwargs):
                captured_kwargs.update(kwargs)
                yield ToolResult(
                    content="task_id: fake_task_123",
                    artifacts=[{"task_id": "fake_task_123"}],
                )

            # _setup_subagent 返回 26 个值的 tuple（与真实签名匹配）
            fake_setup_return = (
                "test_user",
                "test_session",
                Path(str(tmp_path)),
                Path(str(tmp_path)),
                {},
                MagicMock(),  # host_agent_config, host_llm_config
                {"name": "coder", "tool_policy": "inherit", "mcp_policy": "none"},
                "agent_1",
                MagicMock(),
                None,
                False,
                1,
                MagicMock(),
                "cid",
                "test-model",
                None,
                MagicMock(),
                "inherit",
                None,
                Path(tmp_path / ".tmp"),
                None,
                None,
                MagicMock(),
                MagicMock(),
                MagicMock(),
            )

            with patch.object(TaskTool, "invoke_stream", fake_invoke_stream):
                with patch.object(
                    TaskTool, "_setup_subagent", new_callable=AsyncMock
                ) as mock_setup:
                    mock_setup.return_value = fake_setup_return
                    spawn_result = await spawn_tool.invoke(
                        ctx,
                        mission_id=mission_id,
                        prompt="Build something",
                        subagent_type="coder",
                    )

            assert not spawn_result.is_error, f"spawn failed: {spawn_result.content}"
            assert "write_allow_root" in captured_kwargs, (
                f"write_allow_root not passed to TaskTool. kwargs={captured_kwargs}"
            )
            wal = captured_kwargs["write_allow_root"]
            assert wal is not None
            assert len(wal) == 2
            for p in wal:
                assert os.path.isabs(p), f"write_allow_root 条目应为绝对路径: {p}"
            expected_1 = os.path.realpath(os.path.join(str(tmp_path), "src/data"))
            expected_2 = os.path.realpath(os.path.join(str(tmp_path), "src/models"))
            assert os.path.realpath(wal[0]) == expected_1
            assert os.path.realpath(wal[1]) == expected_2
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()

    @pytest.mark.asyncio
    async def test_survey_task_no_write_allow_root(self, tmp_path: Path, tmp_state_dir: str):
        """survey 类任务不需要 write_allow_root（传 None 不限制写）。"""
        clear_store_cache()
        set_team_state_dir_override(tmp_state_dir)

        try:
            ctx = {
                "user_id": "test_user",
                "session_id": "test_session",
                "agent_path": "/root",
            }

            init_result = await TeamInitTool().invoke(ctx, repo_root=str(tmp_path), base="main")
            assert not init_result.is_error

            plan_result = await TeamPlanTool().invoke(
                ctx,
                missions=[{"title": "Survey task", "kind": "survey", "scope": []}],
            )
            assert not plan_result.is_error

            import re

            match = re.search(r"M\d+", plan_result.content or "")
            mission_id = match.group(0)

            captured_kwargs: dict = {}

            async def fake_invoke_stream(self_inner, ctx_inner, **kwargs):
                captured_kwargs.update(kwargs)
                yield ToolResult(
                    content="task_id: fake_survey_123",
                    artifacts=[{"task_id": "fake_survey_123"}],
                )

            from app.services.agent.runtime_backends.aiasys.tools.task_tool import TaskTool

            fake_setup_return = (
                "test_user",
                "test_session",
                Path(str(tmp_path)),
                Path(str(tmp_path)),
                {},
                MagicMock(),
                {"name": "researcher", "tool_policy": "inherit", "mcp_policy": "none"},
                "agent_2",
                MagicMock(),
                None,
                False,
                1,
                MagicMock(),
                "cid2",
                "test-model",
                None,
                MagicMock(),
                "inherit",
                None,
                Path(tmp_path / ".tmp"),
                None,
                None,
                MagicMock(),
                MagicMock(),
                MagicMock(),
            )

            spawn_tool = TeamSpawnTool()
            with patch.object(TaskTool, "invoke_stream", fake_invoke_stream):
                with patch.object(
                    TaskTool, "_setup_subagent", new_callable=AsyncMock
                ) as mock_setup:
                    mock_setup.return_value = fake_setup_return
                    spawn_result = await spawn_tool.invoke(
                        ctx,
                        mission_id=mission_id,
                        prompt="Survey something",
                        subagent_type="researcher",
                    )

            assert not spawn_result.is_error
            assert captured_kwargs.get("write_allow_root") is None
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()

    @pytest.mark.asyncio
    async def test_build_with_empty_scope_rejected_by_plan(
        self, tmp_path: Path, tmp_state_dir: str
    ):
        """build 任务 scope 为空时，store.plan 拒绝（write_allow_root 不进入派生路径）。"""
        clear_store_cache()
        set_team_state_dir_override(tmp_state_dir)

        try:
            ctx = {
                "user_id": "test_user",
                "session_id": "test_session",
                "agent_path": "/root",
            }

            init_result = await TeamInitTool().invoke(ctx, repo_root=str(tmp_path), base="main")
            assert not init_result.is_error

            plan_result = await TeamPlanTool().invoke(
                ctx,
                missions=[{"title": "Bad build", "kind": "build", "scope": []}],
            )
            assert plan_result.is_error
            assert "scope" in plan_result.content.lower()
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()


# ---------------------------------------------------------------------------
# 3. _execute_write_tool 层拦截验证（最小化 mock）
# ---------------------------------------------------------------------------


def _make_session_mocks(write_allow_root: list[str] | None = None) -> dict:
    """为 SessionStreamMixin 提供最小化 mock 上下文（非 fixture，是工厂函数）。"""
    finish_mock = MagicMock()
    finish_mock.return_value = _make_async_gen()
    return {
        "_spec": MagicMock(
            write_allow_root=write_allow_root,
            agent_path="/root/w1",
            authorization_mode="smart",
            yolo=False,
            config=MagicMock(),
            collaboration_policy=None,
            is_subagent=True,
            host_session_id="host",
            parent_agent_id=None,
            agent_max_depth=2,
            allow_subagent_spawn=False,
            budget=None,
            memory_enabled=False,
        ),
        "_tool_registry": MagicMock(),
        "_append_message": MagicMock(),
        "_reset_loop_counter": MagicMock(),
        "_get_last_user_text": MagicMock(return_value=""),
        "_finish_tool_execution": finish_mock,
        "_continuation_state": MagicMock(),
        "_thinking_loop_detector": MagicMock(),
        "_estimated_token_count": 0,
        "_pending_token_estimate": 0,
        "_loop_guard_enabled": True,
        "_thinking_loop_guard_enabled": True,
        "_auto_nudge_enabled": True,
        "_post_list_nudge_enabled": True,
        "_expert_delegation_hint_sent": False,
        "_last_tool_name": None,
        "_cancel_event": MagicMock(is_set=MagicMock(return_value=False)),
        "_closed": False,
        "session_id": "test_w1",
        "mcp_configs": [],
        "_agent_config": {},
        "_tool_strategy": MagicMock(),
        "_model_config": None,
        "_session_turn_count": 0,
        "_current_turn_n": None,
        "budget": None,
        "messages": [],
        "_log_cache_stats": MagicMock(),
        "_append_usage_record": MagicMock(),
        "_save_context_tokens_to_metadata": AsyncMock(),
        "_check_session_budget": AsyncMock(),
        "_is_session_budget_blocked": AsyncMock(return_value=False),
        "_maybe_compact_context": MagicMock(return_value=_make_async_gen()),
        "effective_token_count": 0,
        "_client": MagicMock(),
        "_resolve_request_options": MagicMock(return_value=MagicMock()),
        "_resolve_temperature": MagicMock(return_value=0.7),
        "_resolve_max_tokens": MagicMock(return_value=4096),
    }


def _make_mixin(write_allow_root: list[str] | None = None) -> Any:
    """创建一个填充了 mock 的 SessionStreamMixin 实例。"""
    from app.services.agent.runtime_backends.aiasys.session_stream import (
        SessionStreamMixin,
    )

    mocks = _make_session_mocks(write_allow_root)
    mixin = SessionStreamMixin()
    for k, v in mocks.items():
        setattr(mixin, k, v)

    # 给两个会被 `async for` 消费的方法配上「立即结束」的默认返回值。
    #
    # 不配的话，裸 MagicMock 被 async for 迭代时 __anext__ 永远返回新的 Mock、从不抛
    # StopAsyncIteration，于是无限循环并不停创建 Mock 对象——表现为测试卡死而不是失败。
    # 2026-08-09 实测过一次：用反向探针把 check_write_guard 改成无条件放行，
    # test_write_outside_scope_blocked 直接挂住，faulthandler 转储里满屏
    # `unittest/mock.py line 335 in __new__`。
    #
    # 这三条断言「底层工具不被调用」的测试（write_outside_scope / str_replace_outside_scope
    # / shell_denied）恰恰因为预期不调用，就都没配返回值，于是守卫一失效就从「断言失败」
    # 退化成「永久挂起」。测试的前提被破坏时应当快速报错，而不是把 CI 拖到超时。
    #
    # 用 side_effect 而非 return_value：generator 实例只能消费一次，side_effect 每次调用
    # 都新建一个，多次调用也不会拿到已耗尽的对象。显式配置过的测试会覆盖这里的默认值。
    mixin._tool_registry.invoke_stream = MagicMock(side_effect=lambda *a, **kw: _make_async_gen())
    mixin._finish_tool_execution = MagicMock(side_effect=lambda *a, **kw: _make_async_gen())
    return mixin


class TestExecuteWriteToolGuard:
    """验证 _execute_write_tool 在写前正确拦截。"""

    @pytest.mark.asyncio
    async def test_write_without_guard_calls_tool(self, tmp_path: Path):
        """write_allow_root=None 时，_execute_write_tool 正常调用底层工具。"""

        mixin = _make_mixin(write_allow_root=None)

        tool_result = ToolResult(content="written", is_error=False)
        mixin._tool_registry.invoke_stream = MagicMock(return_value=_make_async_gen(tool_result))
        mixin._finish_tool_execution = MagicMock(return_value=_make_async_gen())

        # 格式与 prompt() 中构建的 exec_info["item"] 一致：
        # arguments 在 item 顶层（非 function 内），已由 _authorize_single_tool 解析
        write_info = {
            "item": {
                "id": "call_1",
                "type": "function",
                "function": {"name": "WriteFile"},
                "arguments": {"path": str(tmp_path / "out.txt"), "content": "hello"},
            },
            "item_ctx": {"write_allow_root": None},
            "side_effect": True,
        }

        events = []
        async for e in mixin._execute_write_tool(write_info):
            events.append(e)

        mixin._tool_registry.invoke_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_outside_scope_blocked(self, tmp_path: Path):
        """写入范围外时，_execute_write_tool 直接返回错误，不调用底层工具。"""

        allowed = tmp_path / "allowed"
        forbidden = tmp_path / "forbidden"
        allowed.mkdir()
        forbidden.mkdir()

        mixin = _make_mixin(write_allow_root=[str(allowed)])

        write_info = {
            "item": {
                "id": "call_1",
                "type": "function",
                "function": {"name": "WriteFile"},
                "arguments": {"path": str(forbidden / "out.txt"), "content": "hello"},
            },
            "item_ctx": {"write_allow_root": [str(allowed)]},
            "side_effect": True,
        }

        events = []
        async for e in mixin._execute_write_tool(write_info):
            events.append(e)

        result_events = [e for e in events if e.kind == "tool_result"]
        assert len(result_events) >= 1, f"Expected tool_result event, got: {events}"
        assert result_events[0].is_error is True
        assert "硬拒绝" in (result_events[0].content or "")
        mixin._tool_registry.invoke_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_within_scope_passes(self, tmp_path: Path):
        """写入范围内时，_execute_write_tool 正常调用底层工具。"""

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "out.txt"

        mixin = _make_mixin(write_allow_root=[str(allowed)])

        tool_result = ToolResult(content="written", is_error=False)
        mixin._tool_registry.invoke_stream = MagicMock(return_value=_make_async_gen(tool_result))
        mixin._finish_tool_execution = MagicMock(return_value=_make_async_gen())

        write_info = {
            "item": {
                "id": "call_1",
                "type": "function",
                "function": {"name": "WriteFile"},
                "arguments": {"path": str(target), "content": "hello"},
            },
            "item_ctx": {"write_allow_root": [str(allowed)]},
            "side_effect": True,
        }

        events = []
        async for e in mixin._execute_write_tool(write_info):
            events.append(e)

        mixin._tool_registry.invoke_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_str_replace_outside_scope_blocked(self, tmp_path: Path):
        """StrReplaceFile 越界被拦截。"""

        allowed = tmp_path / "allowed"
        forbidden = tmp_path / "forbidden"
        allowed.mkdir()
        forbidden.mkdir()

        mixin = _make_mixin(write_allow_root=[str(allowed)])

        write_info = {
            "item": {
                "id": "call_1",
                "type": "function",
                "function": {"name": "StrReplaceFile"},
                "arguments": {
                    "path": str(forbidden / "readme.md"),
                    "old_text": "old",
                    "new_text": "new",
                },
            },
            "item_ctx": {"write_allow_root": [str(allowed)]},
            "side_effect": True,
        }

        events = []
        async for e in mixin._execute_write_tool(write_info):
            events.append(e)

        result_events = [e for e in events if e.kind == "tool_result"]
        assert len(result_events) >= 1
        assert result_events[0].is_error is True
        mixin._tool_registry.invoke_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_shell_denied_with_active_guard(self):
        """Shell 工具在守卫激活时被拦截（已知缺口）。"""

        allowed = Path(tempfile.gettempdir()) / "aiasys_test_allowed_wg"
        allowed.mkdir(exist_ok=True)

        mixin = _make_mixin(write_allow_root=[str(allowed)])

        write_info = {
            "item": {
                "id": "call_1",
                "type": "function",
                "function": {"name": "Shell"},
                "arguments": {"command": "echo pwned > /etc/passwd"},
            },
            "item_ctx": {"write_allow_root": [str(allowed)]},
            "side_effect": True,
        }

        events = []
        async for e in mixin._execute_write_tool(write_info):
            events.append(e)

        result_events = [e for e in events if e.kind == "tool_result"]
        assert len(result_events) >= 1
        assert result_events[0].is_error is True
        mixin._tool_registry.invoke_stream.assert_not_called()
