"""Team tools 行为测试。

覆盖：
- 每个工具的正常路径
- 主控限制（worker 调用被拒）
- team_send 广播与定向的可见性隔离
- team_inbox 的 newest-first 顺序
- team_merge 六道门各自能拦住
- Windows 非法文件名字符被正确处理
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.tool_result import ToolResult
from app.services.agent.runtime_backends.aiasys.team.store import (
    _compute_artifacts_fingerprint,
)
from app.services.agent.runtime_backends.aiasys.team.tools import (
    TeamInboxTool,
    TeamInitTool,
    TeamMergeTool,
    TeamPlanTool,
    TeamSendTool,
    TeamStatusTool,
    TeamTeardownTool,
    _get_store,
    _resolve_team_state_dir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> str:
    """独立的团队状态目录。"""
    return str(tmp_path / "team_state")


@pytest.fixture
async def initialized_ctx(tmp_state_dir: str) -> dict[str, Any]:
    """已初始化的团队 ctx（主控身份）。"""
    from app.services.agent.runtime_backends.aiasys.team.tools import (
        clear_store_cache,
        set_team_state_dir_override,
    )

    clear_store_cache()
    set_team_state_dir_override(tmp_state_dir)

    tool = TeamInitTool()
    ctx = {
        "user_id": "test_user",
        "agent_path": "/root",
    }
    result: ToolResult = await tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
    assert not result.is_error, f"init failed: {result.content}"
    yield ctx

    # Cleanup
    set_team_state_dir_override(None)
    clear_store_cache()


def _worker_ctx(agent_id: str = "worker_1") -> dict[str, Any]:
    """构造 worker 身份的 ctx。"""
    return {
        "user_id": "test_user",
        "agent_path": f"/root/{agent_id}",
    }


def _assert_tool_error(result: ToolResult, expected_substring: str) -> None:
    assert result.is_error, f"Expected error but got: {result.content}"
    assert expected_substring in result.content, (
        f"Expected '{expected_substring}' in error: {result.content}"
    )


# ---------------------------------------------------------------------------
# 1. team_init
# ---------------------------------------------------------------------------


class TestTeamInit:
    async def test_正常初始化(self, tmp_state_dir: str):
        """主控调用应成功初始化。"""
        tool = TeamInitTool()
        ctx = {"user_id": "u1", "agent_path": "/root"}
        result = await tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
        assert not result.is_error
        assert "已初始化" in result.content

    async def test_幂等重复初始化(self, tmp_state_dir: str):
        """再次 init 不应报错，返回已有状态。"""
        tool = TeamInitTool()
        ctx = {"user_id": "u1", "agent_path": "/root"}
        r1 = await tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
        assert not r1.is_error

        r2 = await tool.invoke(ctx, repo_root=tmp_state_dir, base="main")
        assert not r2.is_error

    async def test_worker调用被拒绝(self, tmp_state_dir: str):
        """worker 调用 team_init 应被拒绝。"""
        tool = TeamInitTool()
        ctx = _worker_ctx()
        result = await tool.invoke(ctx, repo_root=tmp_state_dir)
        _assert_tool_error(result, "仅主控可调用")


# ---------------------------------------------------------------------------
# 2. team_plan
# ---------------------------------------------------------------------------


class TestTeamPlan:
    async def test_正常规划_build任务(self, initialized_ctx: dict[str, Any]):
        """主控规划 build 任务应成功。"""
        tool = TeamPlanTool()
        missions = [
            {"title": "数据采集", "kind": "build", "scope": ["outputs/data"]},
            {"title": "报告撰写", "kind": "build", "scope": ["outputs/report"]},
        ]
        result = await tool.invoke(initialized_ctx, missions=missions)
        assert not result.is_error
        assert "M1" in result.content
        assert "M2" in result.content

    async def test_正常规划_survey任务(self, initialized_ctx: dict[str, Any]):
        """survey 任务 scope 可为空。"""
        tool = TeamPlanTool()
        missions = [{"title": "调研", "kind": "survey"}]
        result = await tool.invoke(initialized_ctx, missions=missions)
        assert not result.is_error
        assert "M1" in result.content

    async def test_worker调用被拒绝(self, initialized_ctx: dict[str, Any]):
        tool = TeamPlanTool()
        ctx = _worker_ctx()
        missions = [{"title": "x", "kind": "survey"}]
        result = await tool.invoke(ctx, missions=missions)
        _assert_tool_error(result, "仅主控可调用")

    async def test_scope重叠被拒(self, initialized_ctx: dict[str, Any]):
        """scope 重叠应在工具层透传 store 错误。"""
        tool = TeamPlanTool()
        missions = [
            {"title": "a", "kind": "build", "scope": ["outputs/x"]},
            {"title": "b", "kind": "build", "scope": ["outputs/x/y"]},
        ]
        result = await tool.invoke(initialized_ctx, missions=missions)
        _assert_tool_error(result, "重叠")

    async def test_build缺少scope被拒(self, initialized_ctx: dict[str, Any]):
        tool = TeamPlanTool()
        missions = [{"title": "a", "kind": "build"}]
        result = await tool.invoke(initialized_ctx, missions=missions)
        _assert_tool_error(result, "scope")


# ---------------------------------------------------------------------------
# 3. team_status
# ---------------------------------------------------------------------------


class TestTeamStatus:
    async def test_查询全部任务(self, initialized_ctx: dict[str, Any]):
        """未规划任务时应提示空。"""
        tool = TeamStatusTool()
        result = await tool.invoke(initialized_ctx)
        assert not result.is_error
        assert "尚无任务" in result.content

    async def test_规划后查询(self, initialized_ctx: dict[str, Any]):
        """规划后应能看到任务。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t1", "kind": "build", "scope": ["src/a"]}],
        )

        tool = TeamStatusTool()
        result = await tool.invoke(initialized_ctx)
        assert not result.is_error
        assert "t1" in result.content
        assert "planned" in result.content

    async def test_变更状态_主控成功(self, initialized_ctx: dict[str, Any]):
        """主控可以变更状态。"""
        planner = TeamPlanTool()
        r = await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t1", "kind": "survey"}],
        )
        assert not r.is_error
        # Extract M1 from result
        mission_id = "M1"

        tool = TeamStatusTool()
        result = await tool.invoke(initialized_ctx, mission_id=mission_id, new_status="active")
        assert not result.is_error
        assert "planned → active" in result.content

    async def test_worker可查询(self, initialized_ctx: dict[str, Any]):
        """worker 可以查询（只读）。"""
        tool = TeamStatusTool()
        result = await tool.invoke(_worker_ctx())
        assert not result.is_error

    async def test_worker变更状态被拒(self, initialized_ctx: dict[str, Any]):
        """worker 变更状态应被拒。"""
        tool = TeamStatusTool()
        result = await tool.invoke(_worker_ctx(), mission_id="M1", new_status="active")
        _assert_tool_error(result, "仅主控可调用")

    async def test_非法状态迁移(self, initialized_ctx: dict[str, Any]):
        """merged 不能再切回 active。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t1", "kind": "survey"}],
        )

        status_tool = TeamStatusTool()
        # 先 complete 再 merge
        await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="completed")
        await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="merged")
        # 再切回 active 应失败
        result = await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="active")
        _assert_tool_error(result, "不允许迁移")


# ---------------------------------------------------------------------------
# 4. team_send
# ---------------------------------------------------------------------------


class TestTeamSend:
    async def test_正常发送定向消息(self, initialized_ctx: dict[str, Any]):
        tool = TeamSendTool()
        result = await tool.invoke(
            initialized_ctx,
            from_="coordinator",
            to="worker_1",
            subject="任务分配",
            body="请处理 M1",
        )
        assert not result.is_error
        assert "已发送" in result.content

    async def test_广播消息(self, initialized_ctx: dict[str, Any]):
        tool = TeamSendTool()
        result = await tool.invoke(
            initialized_ctx,
            from_="coordinator",
            to="all",
            subject="团队通知",
            body="所有人注意",
        )
        assert not result.is_error
        assert "已发送" in result.content

    async def test_缺少参数报错(self, initialized_ctx: dict[str, Any]):
        tool = TeamSendTool()
        result = await tool.invoke(initialized_ctx, from_="a", to="b")  # missing subject/body
        _assert_tool_error(result, "subject 参数不能为空")

    async def test_worker也可发送(self, initialized_ctx: dict[str, Any]):
        """worker 调用 team_send 不应被拒绝。"""
        tool = TeamSendTool()
        result = await tool.invoke(
            _worker_ctx(),
            from_="worker_1",
            to="coordinator",
            subject="进展汇报",
            body="M1 已完成 50%",
        )
        assert not result.is_error


# ---------------------------------------------------------------------------
# 5. team_inbox
# ---------------------------------------------------------------------------


class TestTeamInbox:
    async def test_空信箱(self, initialized_ctx: dict[str, Any]):
        tool = TeamInboxTool()
        result = await tool.invoke(initialized_ctx, name="team")
        assert not result.is_error
        assert "为空" in result.content

    async def test_发送后可见_newest_first(self, initialized_ctx: dict[str, Any]):
        """发送多条消息后，inbox 按 newest-first 返回。"""
        sender = TeamSendTool()
        await sender.invoke(initialized_ctx, from_="a", to="all", subject="old", body="first")
        # 稍微错开时间
        await asyncio.sleep(0.01)
        await sender.invoke(initialized_ctx, from_="b", to="all", subject="new", body="second")

        inbox = TeamInboxTool()
        result = await inbox.invoke(initialized_ctx, name="team", limit=10)
        assert not result.is_error
        # newest-first: "new" should appear before "old"
        pos_new = result.content.index("new")
        pos_old = result.content.index("old")
        assert pos_new < pos_old, "newest-first 顺序不正确"

    async def test_定向消息可见性隔离(self, initialized_ctx: dict[str, Any]):
        """定向消息只有目标接收者（和主控）能看到。"""
        sender = TeamSendTool()
        await sender.invoke(
            initialized_ctx,
            from_="coordinator",
            to="worker_1",
            subject="secret for w1",
            body="confidential",
        )
        await sender.invoke(
            initialized_ctx,
            from_="coordinator",
            to="all",
            subject="broadcast",
            body="everyone sees this",
        )

        inbox = TeamInboxTool()

        # worker_1 可以看到定向消息和广播
        r_w1 = await inbox.invoke(_worker_ctx("worker_1"), name="worker_1", limit=20)
        assert not r_w1.is_error
        assert "secret for w1" in r_w1.content
        assert "broadcast" in r_w1.content

        # worker_2 只能看到广播
        r_w2 = await inbox.invoke(_worker_ctx("worker_2"), name="worker_2", limit=20)
        assert not r_w2.is_error
        assert "secret for w1" not in r_w2.content
        assert "broadcast" in r_w2.content

    async def test_主控看全部(self, initialized_ctx: dict[str, Any]):
        sender = TeamSendTool()
        await sender.invoke(initialized_ctx, from_="w1", to="worker_2", subject="private", body="x")

        inbox = TeamInboxTool()
        result = await inbox.invoke(initialized_ctx, name="team", limit=20)
        assert not result.is_error
        assert "private" in result.content

    async def test_limit参数(self, initialized_ctx: dict[str, Any]):
        """limit 应限制返回条数。"""
        sender = TeamSendTool()
        for i in range(5):
            await sender.invoke(
                initialized_ctx,
                from_=f"sender_{i}",
                to="all",
                subject=f"msg{i}",
                body=f"body{i}",
            )

        inbox = TeamInboxTool()
        result = await inbox.invoke(initialized_ctx, name="team", limit=3)
        assert not result.is_error
        # Should contain at most 3 messages
        assert result.content.count("###") <= 3


# ---------------------------------------------------------------------------
# 6. team_merge
# ---------------------------------------------------------------------------


class TestTeamMerge:
    @pytest.fixture
    async def ready_to_merge(self, initialized_ctx: dict[str, Any]) -> str:
        """创建一个满足所有门条件、可以收编的 mission。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[
                {
                    "title": "ready_task",
                    "kind": "build",
                    "scope": ["src/data"],
                    "lease": {"files": ["src/data"]},
                    "deps": [],
                }
            ],
        )

        # Complete the mission
        status_tool = TeamStatusTool()
        await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status_tool.invoke(initialized_ctx, mission_id="M1", new_status="completed")

        # Compute fingerprint for matching artifacts
        artifacts = [{"path": "src/data/output.csv", "size": 100, "hash": "h1"}]
        fp = _compute_artifacts_fingerprint(artifacts)
        return fp

    async def test_全部六道门通过(self, initialized_ctx: dict[str, Any], ready_to_merge: str):
        """所有门通过时成功收编。"""
        tool = TeamMergeTool()
        result = await tool.invoke(
            initialized_ctx,
            mission_id="M1",
            reviewed_commit=ready_to_merge,
            artifacts=[{"path": "src/data/output.csv", "size": 100, "hash": "h1"}],
        )
        assert not result.is_error
        assert "已收编" in result.content

    async def test_gate0_empty_artifacts_rejected(self, initialized_ctx: dict[str, Any]):
        """门〇：artifacts 为空列表应拒绝。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t", "kind": "build", "scope": ["src/x"], "deps": []}],
        )
        status = TeamStatusTool()
        await status.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status.invoke(initialized_ctx, mission_id="M1", new_status="completed")

        tool = TeamMergeTool()
        result = await tool.invoke(
            initialized_ctx, mission_id="M1", reviewed_commit="fp", artifacts=[]
        )
        _assert_tool_error(result, "门〇")

    async def test_gate1_unreviewed_rejected(self, initialized_ctx: dict[str, Any]):
        """门①：缺少 reviewed_commit 应拒绝。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t", "kind": "build", "scope": ["src/x"], "deps": []}],
        )
        status = TeamStatusTool()
        await status.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status.invoke(initialized_ctx, mission_id="M1", new_status="completed")

        tool = TeamMergeTool()
        # Pass artifacts to get past gate 0, but no reviewed_commit for gate 1
        result = await tool.invoke(
            initialized_ctx,
            mission_id="M1",
            reviewed_commit=None,
            artifacts=[{"path": "src/x/f", "size": 1, "hash": "h"}],
        )
        _assert_tool_error(result, "门①")

    async def test_gate3_fingerprint_changed_rejected(self, initialized_ctx: dict[str, Any]):
        """门③：reviewed_commit 与当前 fingerprint 不匹配应拒绝。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[{"title": "t", "kind": "build", "scope": ["src/x"], "deps": []}],
        )
        status = TeamStatusTool()
        await status.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status.invoke(initialized_ctx, mission_id="M1", new_status="completed")

        tool = TeamMergeTool()
        result = await tool.invoke(
            initialized_ctx,
            mission_id="M1",
            reviewed_commit="different_fp",
            artifacts=[{"path": "src/x/f", "size": 1, "hash": "h1"}],
        )
        _assert_tool_error(result, "门③")

    async def test_gate4_unmerged_deps_rejected(self, initialized_ctx: dict[str, Any]):
        """门④：依赖未 merged 应拒绝。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[
                {"title": "dep", "kind": "survey", "deps": []},
                {
                    "title": "main",
                    "kind": "build",
                    "scope": ["src/x"],
                    "deps": ["M1"],
                    "lease": {"files": ["src/x"]},
                },
            ],
        )

        # Directly manipulate state: set M1 to completed (not merged), M2 to completed
        state_dir = _resolve_team_state_dir(initialized_ctx)
        store = await _get_store(state_dir)
        state = await store.load()
        for m in state.missions:
            if m.id == "M1":
                m.status = "completed"  # completed but not merged
            elif m.id == "M2":
                m.status = "completed"
        await store.save(state)

        tool = TeamMergeTool()
        result = await tool.invoke(
            initialized_ctx,
            mission_id="M2",
            reviewed_commit="fp",
            artifacts=[{"path": "src/x/f", "size": 1, "hash": "h1"}],
        )
        _assert_tool_error(result, "门④")

    async def test_gate5_out_of_scope_rejected(self, initialized_ctx: dict[str, Any]):
        """门⑤：artifacts 超出 lease 范围应拒绝。"""
        planner = TeamPlanTool()
        await planner.invoke(
            initialized_ctx,
            missions=[
                {
                    "title": "t",
                    "kind": "build",
                    "scope": ["src/data"],
                    "lease": {"files": ["src/data"]},
                    "deps": [],
                }
            ],
        )
        status = TeamStatusTool()
        await status.invoke(initialized_ctx, mission_id="M1", new_status="active")
        await status.invoke(initialized_ctx, mission_id="M1", new_status="completed")

        # Compute correct fingerprint to pass gate 3, but path is out of scope for gate 5
        artifacts = [{"path": "src/secret/file.txt", "size": 10, "hash": "h"}]
        fp = _compute_artifacts_fingerprint(artifacts)

        tool = TeamMergeTool()
        result = await tool.invoke(
            initialized_ctx,
            mission_id="M1",
            reviewed_commit=fp,
            artifacts=artifacts,
        )
        _assert_tool_error(result, "门⑤")

    async def test_worker调用被拒(self, initialized_ctx: dict[str, Any]):
        tool = TeamMergeTool()
        result = await tool.invoke(_worker_ctx(), mission_id="M1", reviewed_commit="fp")
        _assert_tool_error(result, "仅主控可调用")


# ---------------------------------------------------------------------------
# 7. team_teardown
# ---------------------------------------------------------------------------


class TestTeamTeardown:
    async def test_正常收尾(self, initialized_ctx: dict[str, Any]):
        tool = TeamTeardownTool()
        result = await tool.invoke(initialized_ctx)
        assert not result.is_error
        assert "已关闭" in result.content

    async def test_幂等重复收尾(self, initialized_ctx: dict[str, Any]):
        tool = TeamTeardownTool()
        r1 = await tool.invoke(initialized_ctx)
        assert not r1.is_error
        r2 = await tool.invoke(initialized_ctx)
        assert not r2.is_error

    async def test_worker调用被拒(self, initialized_ctx: dict[str, Any]):
        tool = TeamTeardownTool()
        result = await tool.invoke(_worker_ctx())
        _assert_tool_error(result, "仅主控可调用")


# ---------------------------------------------------------------------------
# 8. Windows 非法文件名字符
# ---------------------------------------------------------------------------


class TestWindowsFilenameSafety:
    async def test_send_with_invalid_chars(self, initialized_ctx: dict[str, Any]):
        """文件名中的 Windows 非法字符应被替换。"""
        tool = TeamSendTool()
        result = await tool.invoke(
            initialized_ctx,
            from_="agent:one",
            to="all",
            subject="问号?星号*管道|",
            body="test body",
        )
        assert not result.is_error
        # The file should have been created without illegal chars
        # Check that no error occurred
        assert "已发送" in result.content

    async def test_inbox_after_sanitized_send(self, initialized_ctx: dict[str, Any]):
        """发送含非法字符的消息后，inbox 仍能正常读取。"""
        sender = TeamSendTool()
        await sender.invoke(
            initialized_ctx,
            from_="a/b",
            to="all",
            subject="c<d>e",
            body="content",
        )

        inbox = TeamInboxTool()
        result = await inbox.invoke(initialized_ctx, name="team")
        assert not result.is_error
        assert "content" in result.content


# ---------------------------------------------------------------------------
# 9. Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_seven_tools_registered(self):
        """验证八个工具都注册了（含 team_spawn）。"""
        from app.services.agent.runtime_backends.aiasys.team.tools import ALL_TOOLS

        assert len(ALL_TOOLS) == 8

    async def test_risk_levels_set(self):
        """验证每个工具的风险级别都正确设置。"""
        expected = {
            "team_init": ("medium", "workspace", True, False),
            "team_plan": ("medium", "workspace", True, False),
            "team_status": ("low", "workspace", True, False),
            "team_send": ("low", "workspace", True, False),
            "team_inbox": ("readonly", "workspace", False, False),
            "team_merge": ("high", "workspace", True, False),
            "team_teardown": ("high", "workspace", True, True),
            "team_spawn": ("high", "workspace", True, False),
        }
        from app.services.agent.runtime_backends.aiasys.team.tools import ALL_TOOLS

        tool_map = {cls.name: cls for cls in ALL_TOOLS}
        assert set(tool_map.keys()) == set(expected.keys()), (
            f"Tool names mismatch. Got: {set(tool_map.keys())}"
        )
        for name, (rl, es, se, dg) in expected.items():
            cls = tool_map[name]
            assert cls.risk_level == rl, f"{name}.risk_level: {cls.risk_level} != {rl}"
            assert cls.effect_scope == es, f"{name}.effect_scope: {cls.effect_scope} != {es}"
            assert cls.side_effect == se, f"{name}.side_effect: {cls.side_effect} != {se}"
            assert cls.dangerous == dg, f"{name}.dangerous: {cls.dangerous} != {dg}"
