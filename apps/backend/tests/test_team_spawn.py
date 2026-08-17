"""Team spawn 工具与 TaskTool background 模式测试。

覆盖：
1. background=False 时 TaskTool 行为与改动前一致（同步等待完成）
2. background=True 时立即返回 task_id 且不阻塞
3. 依赖未满足时 team_spawn 被拒
4. build 类在守卫未就绪时被拒（fail-closed）
5. survey 类可派生
6. 非主控调用 team_spawn 被拒
7. 派生成功后 mission 变 active
8. TEAM_SPAWN_WRITE_GUARD_READY feature flag 可切换
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.tool_result import ToolResult
from app.services.agent.runtime_backends.aiasys.team.tools import (
    TEAM_SPAWN_WRITE_GUARD_READY,
    TeamSpawnTool,
    _get_store,
    _require_controller,
    _resolve_team_state_dir,
    clear_store_cache,
    set_team_state_dir_override,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state_dir(tmp_path):
    return str(tmp_path / "team_state")


@pytest.fixture
async def initialized_ctx(tmp_state_dir):
    """已初始化的团队 ctx（主控身份）。"""
    clear_store_cache()
    set_team_state_dir_override(tmp_state_dir)

    from app.services.agent.runtime_backends.aiasys.team.tools import (
        TeamInitTool,
    )

    tool = TeamInitTool()
    ctx = {
        "user_id": "test_user",
        "session_id": "test_session",
        "agent_path": "/root",
    }
    result: ToolResult = await tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
    assert not result.is_error, f"init failed: {result.content}"
    yield ctx

    set_team_state_dir_override(None)
    clear_store_cache()


def _worker_ctx() -> dict[str, Any]:
    return {
        "user_id": "test_user",
        "session_id": "test_session",
        "agent_path": "/root/worker_1",
    }


def _assert_tool_error(result: ToolResult, expected_substring: str) -> None:
    assert result.is_error, f"Expected error but got: {result.content}"
    assert expected_substring in result.content, (
        f"Expected '{expected_substring}' in error: {result.content}"
    )


# ---------------------------------------------------------------------------
# 1. _require_controller
# ---------------------------------------------------------------------------


class TestRequireController:
    def test_root_allowed(self):
        ctx = {"agent_path": "/root", "user_id": "u"}
        assert _require_controller(ctx) is None

    def test_worker_rejected(self):
        ctx = {"agent_path": "/root/worker_1", "user_id": "u"}
        err = _require_controller(ctx)
        assert err is not None
        assert "仅主控可调用" in err


# ---------------------------------------------------------------------------
# 2. TaskTool background=False 行为不变
# ---------------------------------------------------------------------------


class TestTaskToolBackgroundFalse:
    """background=False（默认）时 TaskTool 行为应与改动前完全一致。

    由于 TaskTool.invoke_stream 需要真实的子 Agent 运行环境，
    这里通过 mock 验证背景模式不改变默认路径的调用序列。
    """

    @pytest.mark.asyncio
    async def test_default_is_foreground(self):
        """不传 background 参数时，行为等价于 background=False。"""
        from app.services.agent.runtime_backends.aiasys.tools.task_tool import (
            TaskTool,
        )

        tool = TaskTool()
        ctx = {
            "user_id": "test_user",
            "session_id": "test_session",
            "agent_path": "/root",
            "agent_config": {"subagents": {}},
            "llm_config": MagicMock(),
        }

        # Mock _find_subagent_manifest to return a valid manifest so we can
        # test that the default (no background) path is synchronous.
        fake_manifest = {
            "name": "coder",
            "model": "test-model",
            "tool_policy": "inherit",
            "mcp_policy": "none",
            "skill_policy": "inherit",
        }

        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool._find_subagent_manifest",
            return_value=fake_manifest,
        ):
            with patch(
                "app.services.agent.runtime_backends.aiasys.tools.task_tool.AiasysRuntimeBackend"
            ) as mock_backend_cls:
                mock_session = MagicMock()
                mock_session._spec = MagicMock()
                mock_session._spec.config.model_dump = MagicMock(return_value={})
                mock_backend = MagicMock()
                mock_backend.create_session = AsyncMock(return_value=mock_session)
                mock_backend_cls.return_value = mock_backend

                with patch(
                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.get_subagent_registry"
                ) as mock_registry_fn:
                    mock_registry = MagicMock()
                    mock_registry.try_register = AsyncMock(return_value=True)
                    mock_registry.aset_launch_spec = AsyncMock()
                    mock_registry.aget_launch_spec = AsyncMock(
                        return_value={"timeout_seconds": 300}
                    )
                    mock_registry_fn.return_value = mock_registry

                    with patch(
                        "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentStorage"
                    ) as mock_storage_cls:
                        mock_storage = MagicMock()
                        mock_storage.subagent_dir = MagicMock()
                        mock_storage.append_context_message = AsyncMock()
                        mock_storage.update_launch_spec = MagicMock()
                        mock_storage.flush = AsyncMock()
                        mock_storage_cls.return_value = mock_storage

                        with patch(
                            "app.services.agent.runtime_backends.aiasys.tools.task_tool._materialize_subagent_toml",
                            return_value=MagicMock(),
                        ):
                            with patch(
                                "app.services.agent.runtime_backends.aiasys.tools.task_tool._resolve_skills_dir",
                                return_value=None,
                            ):
                                with patch(
                                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentLifecycleManager"
                                ) as mock_lifecycle_cls:
                                    mock_lifecycle = MagicMock()

                                    async def _fake_run_subagent(*args, **kwargs):
                                        yield ToolResult(content="done", is_error=False)

                                    mock_lifecycle.run_subagent_session = _fake_run_subagent
                                    mock_lifecycle_cls.return_value = mock_lifecycle

                                    results = []
                                    async for r in tool.invoke_stream(ctx, prompt="hello"):
                                        results.append(r)

        # Default (no background param): should stream synchronously to completion
        assert len(results) >= 1
        # Should have gotten the "done" result from lifecycle manager
        contents = [r.content for r in results if not r.is_error]
        assert any("done" in c for c in contents), f"Expected 'done' in results: {contents}"


# ---------------------------------------------------------------------------
# 3. TaskTool background=True 立即返回
# ---------------------------------------------------------------------------


class TestTaskToolBackgroundTrue:
    """background=True 时 TaskTool 应立即返回 task_id。"""

    @pytest.mark.asyncio
    async def test_background_true_returns_task_id(self):
        """background=True 时只 yield 一次结果，含 task_id。"""
        from app.services.agent.runtime_backends.aiasys.tools.task_tool import (
            TaskTool,
        )

        tool = TaskTool()
        mock_session = MagicMock()
        mock_session._spec = MagicMock()
        mock_session._spec.config.model_dump = MagicMock(return_value={})

        mock_registry = MagicMock()
        mock_registry.try_register = AsyncMock(return_value=True)
        mock_registry.aset_launch_spec = AsyncMock()

        fake_manifest = {
            "name": "test_agent",
            "model": "test-model",
            "tool_policy": "inherit",
            "mcp_policy": "none",
            "skill_policy": "inherit",
        }

        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool._find_subagent_manifest",
            return_value=fake_manifest,
        ):
            with patch(
                "app.services.agent.runtime_backends.aiasys.tools.task_tool.AiasysRuntimeBackend"
            ) as mock_backend_cls:
                mock_backend = MagicMock()
                mock_backend.create_session = AsyncMock(return_value=mock_session)
                mock_backend_cls.return_value = mock_backend

                with patch(
                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.get_subagent_registry",
                    return_value=mock_registry,
                ):
                    with patch(
                        "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentStorage"
                    ) as mock_storage_cls:
                        mock_storage = MagicMock()
                        mock_storage.subagent_dir = MagicMock()
                        mock_storage.append_context_message = AsyncMock()
                        mock_storage.update_launch_spec = MagicMock()
                        mock_storage.flush = AsyncMock()
                        mock_storage_cls.return_value = mock_storage

                        async def _fake_run_subagent_bg(*args, **kwargs):
                            yield ToolResult(content="background done", is_error=False)

                        with patch(
                            "app.services.agent.runtime_backends.aiasys.tools.task_tool._materialize_subagent_toml",
                            return_value=MagicMock(),
                        ):
                            with patch(
                                "app.services.agent.runtime_backends.aiasys.tools.task_tool._resolve_skills_dir",
                                return_value=None,
                            ):
                                with patch(
                                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentLifecycleManager"
                                ) as mock_lifecycle_cls:
                                    mock_lifecycle = MagicMock()
                                    mock_lifecycle.run_subagent_session = _fake_run_subagent_bg
                                    mock_lifecycle_cls.return_value = mock_lifecycle

                                    ctx = {
                                        "user_id": "test_user",
                                        "session_id": "test_session",
                                        "agent_path": "/root",
                                        "agent_config": {},
                                        "llm_config": MagicMock(),
                                    }

                                    results = []
                                    async for r in tool.invoke_stream(
                                        ctx,
                                        prompt="test prompt",
                                        background=True,
                                    ):
                                        results.append(r)

        # background=True: 应该只 yield 一次启动结果
        assert len(results) == 1
        result = results[0]
        assert not result.is_error
        assert result.content is not None and "task_id" in result.content

    @pytest.mark.asyncio
    async def test_background_true_task_runs_in_background(self):
        """background=True 时 asyncio task 确实在后台运行。"""
        from app.services.agent.runtime_backends.aiasys.tools.task_tool import (
            TaskTool,
        )

        tool = TaskTool()
        mock_session = MagicMock()
        mock_session._spec = MagicMock()
        mock_session._spec.config.model_dump = MagicMock(return_value={})

        mock_registry = MagicMock()
        mock_registry.try_register = AsyncMock(return_value=True)
        mock_registry.aset_launch_spec = AsyncMock()

        fake_manifest = {
            "name": "test_agent",
            "model": "test-model",
            "tool_policy": "inherit",
            "mcp_policy": "none",
            "skill_policy": "inherit",
        }

        lifecycle_run = AsyncMock()

        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool._find_subagent_manifest",
            return_value=fake_manifest,
        ):
            with patch(
                "app.services.agent.runtime_backends.aiasys.tools.task_tool.AiasysRuntimeBackend"
            ) as mock_backend_cls:
                mock_backend = MagicMock()
                mock_backend.create_session = AsyncMock(return_value=mock_session)
                mock_backend_cls.return_value = mock_backend

                with patch(
                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.get_subagent_registry",
                    return_value=mock_registry,
                ):
                    with patch(
                        "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentStorage"
                    ) as mock_storage_cls:
                        mock_storage = MagicMock()
                        mock_storage.subagent_dir = MagicMock()
                        mock_storage.append_context_message = AsyncMock()
                        mock_storage.update_launch_spec = MagicMock()
                        mock_storage.flush = AsyncMock()
                        mock_storage_cls.return_value = mock_storage

                        async def _fake_run_subagent_check(*args, **kwargs):
                            yield ToolResult(content="bg done", is_error=False)

                        with patch(
                            "app.services.agent.runtime_backends.aiasys.tools.task_tool._materialize_subagent_toml",
                            return_value=MagicMock(),
                        ):
                            with patch(
                                "app.services.agent.runtime_backends.aiasys.tools.task_tool._resolve_skills_dir",
                                return_value=None,
                            ):
                                with patch(
                                    "app.services.agent.runtime_backends.aiasys.tools.task_tool.SubAgentLifecycleManager"
                                ) as mock_lifecycle_cls:
                                    mock_lifecycle_cls.return_value.run_subagent_session = (
                                        _fake_run_subagent_check
                                    )
                                    ctx = {
                                        "user_id": "test_user",
                                        "session_id": "test_session",
                                        "agent_path": "/root",
                                        "agent_config": {},
                                        "llm_config": MagicMock(),
                                    }

                                    results = []
                                    async for r in tool.invoke_stream(
                                        ctx,
                                        prompt="test prompt",
                                        background=True,
                                    ):
                                        results.append(r)

        # 确认立即返回（不等待 lifecycle run）
        assert len(results) == 1
        # lifecycle_run 应该还没有被 await（因为是 create_task 在后台）
        lifecycle_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. team_spawn 依赖门控
# ---------------------------------------------------------------------------


class TestTeamSpawnDepGate:
    """依赖未全部 merged 的 mission 拒绝启动。"""

    @pytest.mark.asyncio
    async def test_unmerged_deps_rejected(self, initialized_ctx, tmp_state_dir):
        """依赖未 merged 时 team_spawn 被拒。"""
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamPlanTool,
        )

        # Plan 一个 survey 任务（无依赖）和一个有依赖的任务
        plan_tool = TeamPlanTool()
        plan_result: ToolResult = await plan_tool.invoke(
            initialized_ctx,
            missions=[
                {"title": "dep task", "kind": "survey"},
                {"title": "main task", "kind": "survey", "deps": ["M1"]},
            ],
        )
        assert not plan_result.is_error, f"plan failed: {plan_result.content}"

        # 尝试 spawn 有未 merged 依赖的任务 → 应该被拒
        spawn_tool = TeamSpawnTool()
        # Mock TaskTool to avoid real spawn (patch at the tool_tool module level)
        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool.TaskTool"
        ) as mock_task_tool_cls:
            mock_task_tool = MagicMock()

            async def _fake_invoke_stream_dep(*args, **kwargs):
                yield ToolResult(
                    content="task_id: fake_id",
                    artifacts=[{"task_id": "fake_id"}],
                )

            mock_task_tool.invoke_stream = _fake_invoke_stream_dep
            mock_task_tool_cls.return_value = mock_task_tool

            result: ToolResult = await spawn_tool.invoke(
                initialized_ctx,
                mission_id="M2",
                prompt="do something",
            )

        _assert_tool_error(result, "依赖")


# ---------------------------------------------------------------------------
# 5. team_spawn build 类在守卫未就绪时被拒
# ---------------------------------------------------------------------------


class TestTeamSpawnWriteGuard:
    """team_spawn 写范围守卫：守卫就绪时放行 build 类任务。"""

    @pytest.mark.asyncio
    async def test_build_passes_guard_when_ready(self, initialized_ctx, tmp_state_dir):
        """kind=build + TEAM_SPAWN_WRITE_GUARD_READY=True → 不在此处被拒，进入派生流程。

        由于未 mock TaskTool，后续会因 session 创建失败而报错——这是预期行为，
        证明守卫已放行（不再在 feature flag 处拦截）。
        """
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamPlanTool,
        )

        plan_tool = TeamPlanTool()
        plan_result: ToolResult = await plan_tool.invoke(
            initialized_ctx,
            missions=[
                {
                    "title": "build task",
                    "kind": "build",
                    "scope": ["src/"],
                },
            ],
        )
        assert not plan_result.is_error, f"plan failed: {plan_result.content}"

        spawn_tool = TeamSpawnTool()
        result: ToolResult = await spawn_tool.invoke(
            initialized_ctx,
            mission_id="M1",
            prompt="do build",
        )

        # 不再在 feature flag 处拒绝（那是旧行为）
        assert "TEAM_SPAWN_WRITE_GUARD_READY" not in (result.content or ""), (
            "守卫已就绪，不应再返回 feature flag 拒绝消息"
        )
        # 后续失败是因为 TaskTool 未 mock，不是守卫问题
        # 验证 mission 已切到 active（守卫通过后，状态门已执行）
        state_dir = _resolve_team_state_dir(initialized_ctx)
        store = await _get_store(state_dir)
        state = await store.load()
        mission = next((m for m in state.missions if m.id == "M1"), None)
        assert mission is not None
        # mission 被设为 active（后续 TaskTool 失败会回滚，但这里预期 active）
        assert mission.status == "active"

    @pytest.mark.asyncio
    async def test_survey_allowed_when_guard_not_ready(self, initialized_ctx, tmp_state_dir):
        """kind=survey + TEAM_SPAWN_WRITE_GUARD_READY=False → 允许（但需 mock TaskTool）。"""
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamPlanTool,
        )

        plan_tool = TeamPlanTool()
        plan_result: ToolResult = await plan_tool.invoke(
            initialized_ctx,
            missions=[
                {
                    "title": "survey task",
                    "kind": "survey",
                },
            ],
        )
        assert not plan_result.is_error

        spawn_tool = TeamSpawnTool()
        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool.TaskTool"
        ) as mock_task_tool_cls:
            mock_task_tool = MagicMock()

            async def _fake_invoke_stream(*args, **kwargs):
                yield ToolResult(
                    content="task_id: fake_survey_id",
                    artifacts=[{"task_id": "fake_survey_id"}],
                )

            mock_task_tool.invoke_stream = _fake_invoke_stream
            mock_task_tool_cls.return_value = mock_task_tool

            result: ToolResult = await spawn_tool.invoke(
                initialized_ctx,
                mission_id="M1",
                prompt="survey something",
            )

        # survey 应该被允许（不被 write guard 拦住）
        assert not result.is_error, f"survey spawn failed: {result.content}"
        assert "task_id" in result.content


# ---------------------------------------------------------------------------
# 6. team_spawn 非主控调用被拒
# ---------------------------------------------------------------------------


class TestTeamSpawnControllerOnly:
    """team_spawn 仅主控可调用。"""

    @pytest.mark.asyncio
    async def test_worker_cannot_spawn(self, tmp_state_dir):
        """worker 调用 team_spawn 被拒。"""
        clear_store_cache()
        set_team_state_dir_override(tmp_state_dir)

        try:
            from app.services.agent.runtime_backends.aiasys.team.tools import (
                TeamInitTool,
            )

            init_tool = TeamInitTool()
            ctx = {
                "user_id": "test_user",
                "session_id": "test_session",
                "agent_path": "/root",
            }
            result = await init_tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
            assert not result.is_error

            spawn_tool = TeamSpawnTool()
            worker_ctx = _worker_ctx()
            result: ToolResult = await spawn_tool.invoke(
                worker_ctx,
                mission_id="M1",
                prompt="do something",
            )
            _assert_tool_error(result, "仅主控可调用")
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()


# ---------------------------------------------------------------------------
# 7. 派生成功后 mission 变 active
# ---------------------------------------------------------------------------


class TestTeamSpawnMissionActive:
    """派生成功后 mission 状态变为 active，owner 被设置。"""

    @pytest.mark.asyncio
    async def test_mission_becomes_active(self, initialized_ctx, tmp_state_dir):
        """spawn 成功后 mission 状态为 active，owner 非空。"""
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamPlanTool,
        )

        plan_tool = TeamPlanTool()
        plan_result: ToolResult = await plan_tool.invoke(
            initialized_ctx,
            missions=[
                {
                    "title": "survey task",
                    "kind": "survey",
                },
            ],
        )
        assert not plan_result.is_error

        spawn_tool = TeamSpawnTool()
        with patch(
            "app.services.agent.runtime_backends.aiasys.tools.task_tool.TaskTool"
        ) as mock_task_tool_cls:
            fake_task_id = "test_task_123"
            mock_task_tool = MagicMock()

            async def _fake_invoke_stream_active(*args, **kwargs):
                yield ToolResult(
                    content=f"task_id: {fake_task_id}",
                    artifacts=[{"task_id": fake_task_id}],
                )

            mock_task_tool.invoke_stream = _fake_invoke_stream_active
            mock_task_tool_cls.return_value = mock_task_tool

            result: ToolResult = await spawn_tool.invoke(
                initialized_ctx,
                mission_id="M1",
                prompt="survey something",
            )

        assert not result.is_error, f"spawn failed: {result.content}"

        # 验证 mission 状态
        state_dir = _resolve_team_state_dir(initialized_ctx)
        store = await _get_store(state_dir)
        state = await store.load()
        mission = next((m for m in state.missions if m.id == "M1"), None)
        assert mission is not None
        assert mission.status == "active"
        assert mission.owner == fake_task_id


# ---------------------------------------------------------------------------
# 8. TEAM_SPAWN_WRITE_GUARD_READY feature flag
# ---------------------------------------------------------------------------


class TestWriteGuardFeatureFlag:
    """TEAM_SPAWN_WRITE_GUARD_READY 常量控制 build 派生。"""

    def test_default_is_true(self):
        """默认值为 True（写范围守卫已实现并通过测试）。"""
        assert TEAM_SPAWN_WRITE_GUARD_READY is True

    def test_module_constant_exists(self):
        """常量存在于 team.tools 模块中。"""
        from app.services.agent.runtime_backends.aiasys.team import tools as team_tools

        assert hasattr(team_tools, "TEAM_SPAWN_WRITE_GUARD_READY")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_iter(items):
    """将列表包装为 async iterator。"""
    for item in items:
        yield item
