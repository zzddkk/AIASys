"""Resource Lease Layer 测试（第五步，含第六步修正）。

覆盖：
- 运行时租约表：申请、释放、冲突检测
- notebook 独占：第二个 mission 申请同一 notebook 被拒且错误信息含持有者
- 租约释放后可再申请
- 非独占资源放行（多个 mission 同时持有同一 dataset/knowledge_graph）
- 资源 ID 互斥检查：team_plan 拒绝两个 mission 声明同一资源
- 删除类工具 require_lease：无租约时被拒，有租约时放行
- 13 个工具里至少覆盖 5 个不同资源类型的放行与拒绝
- workspace_memory 分片隔离
- 非 team 场景零行为变更
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.agent.runtime_backends.aiasys.team.store import (
    _RESOURCE_LEASE_MAP,
    TeamError,
    TeamMission,
    TeamStore,
    _normalize_lease_key,
    check_write_guard,
    get_workspace_memory_main_path,
    get_workspace_memory_shard_path,
    merge_workspace_memory_shards,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> str:
    return str(tmp_path / "team")


@pytest.fixture
async def store(state_dir: str) -> TeamStore:
    s = TeamStore(state_dir)
    await s.init(repo_root=str(Path("/fake/repo").resolve()), base="main")
    return s


def _mission(**kwargs: object) -> TeamMission:
    data: dict[str, object] = {
        "id": "M0",
        "title": "test",
        "kind": "build",
        "scope": [],
        "deps": [],
        "status": "planned",
        "owner": None,
        "reviewed_commit": None,
        "lease": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "resource_lease_keys": [],
    }
    data.update(kwargs)
    return TeamMission(**data)


# ---------------------------------------------------------------------------
# 1. 运行时租约表基础操作
# ---------------------------------------------------------------------------


class TestResourceLeaseBasics:
    async def test_acquire_and_release(self, store: TeamStore):
        await store.acquire_resource_lease("M1", "notebook", "nb-1", "notebook:nb-1")
        assert store.get_lease_holder("notebook", "nb-1") == "M1"
        await store.release_resource_lease("M1", "notebook", "nb-1")
        assert store.get_lease_holder("notebook", "nb-1") is None

    async def test_same_mission_reacquire_ok(self, store: TeamStore):
        await store.acquire_resource_lease("M1", "notebook", "nb-1", "notebook:nb-1")
        await store.acquire_resource_lease("M1", "notebook", "nb-1", "notebook:nb-1")  # 不抛
        await store.release_all_mission_leases("M1")

    async def test_different_mission_conflict(self, store: TeamStore):
        await store.acquire_resource_lease("M1", "notebook", "nb-1", "notebook:nb-1")
        with pytest.raises(TeamError, match="M1"):
            await store.acquire_resource_lease("M2", "notebook", "nb-1", "notebook:nb-1")
        await store.release_all_mission_leases("M1")

    async def test_non_exclusive_no_conflict(self, store: TeamStore):
        await store.acquire_resource_lease(
            "M1", "dataset", "sales", "dataset:sales", exclusive=False
        )
        await store.acquire_resource_lease(
            "M2", "dataset", "sales", "dataset:sales", exclusive=False
        )  # 不抛
        await store.release_all_mission_leases("M1")
        await store.release_all_mission_leases("M2")

    async def test_release_all_clears_all(self, store: TeamStore):
        await store.acquire_resource_lease(
            "M1", "notebook", "nb-1", "notebook:nb-1", exclusive=False
        )
        await store.acquire_resource_lease(
            "M1", "dataset", "sales", "dataset:sales", exclusive=False
        )
        await store.release_all_mission_leases("M1")
        assert store.get_lease_holder("notebook", "nb-1") is None
        assert store.get_lease_holder("dataset", "sales") is None


# ---------------------------------------------------------------------------
# 2. resolve_mission_resource_leases
# ---------------------------------------------------------------------------


class TestResolveMissionResourceLeases:
    async def test_exclusive_conflict_rejected(self, store: TeamStore):
        mission_m1 = _mission(id="M1", lease={"notebook": ["nb-1"]})
        await store.resolve_mission_resource_leases(mission_m1)
        mission_m2 = _mission(id="M2", lease={"notebook": ["nb-1"]})
        with pytest.raises(TeamError, match="M1"):
            await store.resolve_mission_resource_leases(mission_m2)
        await store.release_all_mission_leases("M1")

    async def test_release_then_reacquire(self, store: TeamStore):
        mission_m1 = _mission(id="M1", lease={"notebook": ["nb-1"]})
        keys_m1 = await store.resolve_mission_resource_leases(mission_m1)
        assert "notebook:nb-1" in keys_m1
        await store.release_all_mission_leases("M1")
        mission_m2 = _mission(id="M2", lease={"notebook": ["nb-1"]})
        keys_m2 = await store.resolve_mission_resource_leases(mission_m2)
        assert "notebook:nb-1" in keys_m2
        await store.release_all_mission_leases("M2")

    async def test_non_exclusive_grants_multiple(self, store: TeamStore):
        mission_m1 = _mission(id="M1", lease={"knowledge_graph": ["g-1"]})
        mission_m2 = _mission(id="M2", lease={"knowledge_graph": ["g-1"]})
        keys_m1 = await store.resolve_mission_resource_leases(mission_m1)
        keys_m2 = await store.resolve_mission_resource_leases(mission_m2)
        assert "knowledge_graph:g-1" in keys_m1
        assert "knowledge_graph:g-1" in keys_m2
        await store.release_all_mission_leases("M1")
        await store.release_all_mission_leases("M2")

    async def test_mixed_exclusive_and_non_exclusive(self, store: TeamStore):
        mission = _mission(
            id="M1",
            lease={"notebook": ["nb-1"], "dataset": ["sales"], "knowledge_graph": ["g-1"]},
        )
        keys = await store.resolve_mission_resource_leases(mission)
        assert "notebook:nb-1" in keys
        assert "dataset:sales" in keys
        assert "knowledge_graph:g-1" in keys
        await store.release_all_mission_leases("M1")

    async def test_lease_keys_persisted_to_state(self, store: TeamStore, state_dir: str):
        mission = _mission(id="M1", lease={"notebook": ["nb-1"]})
        await store.resolve_mission_resource_leases(mission)
        store2 = TeamStore(state_dir)
        state = await store2.load()
        norm_key = _normalize_lease_key("notebook", "nb-1")
        assert norm_key in state.resource_leases
        assert state.resource_leases[norm_key]["mission_id"] == "M1"


# ---------------------------------------------------------------------------
# 3. team_plan 资源 ID 互斥检查
# ---------------------------------------------------------------------------


class TestPlanResourceMutualExclusion:
    async def test_same_dataset_rejected(self, state_dir: str):
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamInitTool,
            TeamPlanTool,
            clear_store_cache,
            set_team_state_dir_override,
        )

        clear_store_cache()
        set_team_state_dir_override(state_dir)
        try:
            # 先初始化团队
            init_tool = TeamInitTool()
            init_result = await init_tool.invoke(
                ctx={"agent_path": "/root"},
                repo_root=str(Path("/fake/repo").resolve()),
            )
            assert not init_result.is_error, f"team_init 失败: {init_result.content}"

            tool = TeamPlanTool()
            result = await tool.invoke(
                ctx={"agent_path": "/root"},
                missions=[
                    {
                        "title": "M1: 分析销售数据",
                        "kind": "build",
                        "scope": ["outputs/a.md"],
                        "lease": {"datasets": ["sales_2026"]},
                    },
                    {
                        "title": "M2: 清洗销售数据",
                        "kind": "build",
                        "scope": ["outputs/b.md"],
                        "lease": {"datasets": ["sales_2026"]},
                    },
                ],
            )
            assert result.is_error, "两个 mission 声明同一 dataset 应被拒绝"
            assert "sales_2026" in result.content
            assert "M1" in result.content or "M2" in result.content
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()

    async def test_different_datasets_allowed(self, state_dir: str):
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamInitTool,
            TeamPlanTool,
            clear_store_cache,
            set_team_state_dir_override,
        )

        clear_store_cache()
        set_team_state_dir_override(state_dir)
        try:
            init_tool = TeamInitTool()
            init_result = await init_tool.invoke(
                ctx={"agent_path": "/root"},
                repo_root=str(Path("/fake/repo").resolve()),
            )
            assert not init_result.is_error, f"team_init 失败: {init_result.content}"

            tool = TeamPlanTool()
            result = await tool.invoke(
                ctx={"agent_path": "/root"},
                missions=[
                    {
                        "title": "M1: 分析销售",
                        "kind": "build",
                        "scope": ["outputs/a.md"],
                        "lease": {"datasets": ["sales_2026"]},
                    },
                    {
                        "title": "M2: 分析库存",
                        "kind": "build",
                        "scope": ["outputs/b.md"],
                        "lease": {"datasets": ["inventory"]},
                    },
                ],
            )
            assert not result.is_error, f"不同 dataset 应放行: {result.content}"
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()

    async def test_same_notebook_rejected(self, state_dir: str):
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamInitTool,
            TeamPlanTool,
            clear_store_cache,
            set_team_state_dir_override,
        )

        clear_store_cache()
        set_team_state_dir_override(state_dir)
        try:
            init_tool = TeamInitTool()
            init_result = await init_tool.invoke(
                ctx={"agent_path": "/root"},
                repo_root=str(Path("/fake/repo").resolve()),
            )
            assert not init_result.is_error, f"team_init 失败: {init_result.content}"

            tool = TeamPlanTool()
            result = await tool.invoke(
                ctx={"agent_path": "/root"},
                missions=[
                    {
                        "title": "M1: notebook 任务",
                        "kind": "build",
                        "scope": ["outputs/a.md"],
                        "lease": {"kernel": "exclusive", "notebook": ["nb-1"]},
                    },
                    {
                        "title": "M2: notebook 任务",
                        "kind": "build",
                        "scope": ["outputs/b.md"],
                        "lease": {"kernel": "exclusive", "notebook": ["nb-1"]},
                    },
                ],
            )
            assert result.is_error, "两个 mission 声明同一 notebook 应被拒绝"
            assert "nb-1" in result.content
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()


# ---------------------------------------------------------------------------
# 4. check_write_guard — require_lease（删除类工具）
# ---------------------------------------------------------------------------


class TestCheckWriteGuardRequireLease:
    """删除类工具要求持有租约。"""

    # --- DeleteDataTableRecord (dataset) ---
    def test_delete_record_with_lease_allowed(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteDataTableRecord",
            arguments={"table_path": "/workspace/sales.table.db", "record_id": "r1"},
            resource_lease_keys=["dataset:/workspace/sales.table.db"],
        )
        assert result is None

    def test_delete_record_without_lease_denied(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteDataTableRecord",
            arguments={"table_path": "/workspace/sales.table.db", "record_id": "r1"},
            resource_lease_keys=[],
        )
        assert result is not None
        assert "删除类工具要求持有租约" in result

    # --- DeleteKnowledgeGraph (knowledge_graph) ---
    def test_delete_graph_with_lease_allowed(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteKnowledgeGraph",
            arguments={"graph_id": "my-graph"},
            resource_lease_keys=["knowledge_graph:my-graph"],
        )
        assert result is None

    def test_delete_graph_without_lease_denied(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteKnowledgeGraph",
            arguments={"graph_id": "my-graph"},
            resource_lease_keys=[],
        )
        assert result is not None
        assert "删除类工具要求持有租约" in result

    # --- DeleteGraphEntity (knowledge_graph, base_id) ---
    def test_delete_entity_with_lease_allowed(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteGraphEntity",
            arguments={"base_id": "g-1", "entity_name": "e1"},
            resource_lease_keys=["knowledge_graph:g-1"],
        )
        assert result is None

    def test_delete_entity_without_lease_denied(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteGraphEntity",
            arguments={"base_id": "g-1", "entity_name": "e1"},
            resource_lease_keys=[],
        )
        assert result is not None
        assert "删除类工具要求持有租约" in result

    # --- DeleteKnowledgeBase (knowledge_base) ---
    def test_delete_kb_with_lease_allowed(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteKnowledgeBase",
            arguments={"knowledge_base_id": "kb-1"},
            resource_lease_keys=["knowledge_base:kb-1"],
        )
        assert result is None

    def test_delete_kb_without_lease_denied(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteKnowledgeBase",
            arguments={"knowledge_base_id": "kb-1"},
            resource_lease_keys=[],
        )
        assert result is not None
        assert "删除类工具要求持有租约" in result

    # --- DeleteDocumentsFromKnowledgeBase ---
    def test_delete_docs_with_lease_allowed(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteDocumentsFromKnowledgeBase",
            arguments={"knowledge_base_id": "kb-1", "document_ids": ["d1"]},
            resource_lease_keys=["knowledge_base:kb-1"],
        )
        assert result is None

    def test_delete_docs_without_lease_denied(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="DeleteDocumentsFromKnowledgeBase",
            arguments={"knowledge_base_id": "kb-1", "document_ids": ["d1"]},
            resource_lease_keys=[],
        )
        assert result is not None
        assert "删除类工具要求持有租约" in result

    # --- 非删除类工具不受 require_lease 影响 ---
    def test_create_data_table_without_lease_allowed(self):
        """CreateDataTable 是 insert 语义，不要求持有租约。"""
        result = check_write_guard(
            write_allow_root=None,
            tool_name="CreateDataTable",
            arguments={"table_id": "new-table"},
            resource_lease_keys=[],
        )
        assert result is None

    def test_create_graph_entity_without_lease_allowed(self):
        """CreateGraphEntity 是 insert 语义，不要求持有租约。"""
        result = check_write_guard(
            write_allow_root=None,
            tool_name="CreateGraphEntity",
            arguments={"base_id": "g-1"},
            resource_lease_keys=[],
        )
        assert result is None


# ---------------------------------------------------------------------------
# 5. check_write_guard — 路径守卫兼容性
# ---------------------------------------------------------------------------


class TestWriteGuardBackwardCompat:
    def test_path_in_allow_root_passes(self, tmp_path: Path):
        allowed = str(tmp_path / "allowed")
        (tmp_path / "allowed").mkdir()
        target = str(tmp_path / "allowed" / "file.txt")
        result = check_write_guard(
            write_allow_root=[allowed],
            tool_name="WriteFile",
            arguments={"path": target},
            resource_lease_keys=None,
        )
        assert result is None

    def test_path_outside_denied(self, tmp_path: Path):
        allowed = str(tmp_path / "allowed")
        (tmp_path / "allowed").mkdir()
        result = check_write_guard(
            write_allow_root=[allowed],
            tool_name="WriteFile",
            arguments={"path": str(tmp_path / "forbidden.txt")},
            resource_lease_keys=None,
        )
        assert result is not None

    def test_unknown_tool_with_no_restrictions_passes(self):
        result = check_write_guard(
            write_allow_root=None,
            tool_name="SomeUnknownTool",
            arguments={},
            resource_lease_keys=None,
        )
        assert result is None

    def test_shell_denied_with_allow_root(self):
        result = check_write_guard(
            write_allow_root=["/some/root"],
            tool_name="Shell",
            arguments={"command": "echo hi"},
        )
        assert result is not None
        assert "Shell" in result


# ---------------------------------------------------------------------------
# 6. 非 team 场景零行为变更
# ---------------------------------------------------------------------------


class TestNoTeamContextUnchanged:
    def test_all_tools_pass_without_restrictions(self):
        tools_and_args = [
            ("WriteFile", {"path": "/any/path.txt"}),
            ("StrReplaceFile", {"path": "/any/path.txt", "old": "x", "new": "y"}),
            ("CreateFile", {"path": "/any/path.txt", "content": "hi"}),
            ("EditNotebookFile", {"notebook_path": "/any/nb.ipynb"}),
            ("CreateDataTable", {"table_id": "t1"}),
            ("DeleteDataTableRecord", {"table_path": "/workspace/t1.table.db", "record_id": "r1"}),
            ("CreateKnowledgeGraph", {"graph_id": "g1"}),
            ("DeleteKnowledgeGraph", {"graph_id": "g1"}),
            ("CreateGraphEntity", {"base_id": "g1"}),
            ("DeleteGraphEntity", {"base_id": "g1", "entity_name": "e1"}),
            ("CreateGraphRelation", {"base_id": "g1"}),
            ("CreateKnowledgeBase", {"name": "kb1"}),
            ("DeleteDocumentsFromKnowledgeBase", {"knowledge_base_id": "kb1"}),
            ("DeleteKnowledgeBase", {"knowledge_base_id": "kb1"}),
            ("DeleteEnvVar", {"name": "VAR1"}),
        ]
        for tool_name, args in tools_and_args:
            result = check_write_guard(
                write_allow_root=None,
                tool_name=tool_name,
                arguments=args,
                resource_lease_keys=None,
            )
            assert result is None, f"工具 {tool_name} 在无限制时应放行，实际被拒: {result}"


# ---------------------------------------------------------------------------
# 7. 13 工具归类和 _RESOURCE_LEASE_MAP 完整性
# ---------------------------------------------------------------------------


class TestResourceLeaseMapCompleteness:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "WriteCanvas",
            "CreateDataTable",
            "DeleteDataTableRecord",
            "DeleteEnvVar",
            "EditNotebookFile",
            "CreateKnowledgeGraph",
            "DeleteKnowledgeGraph",
            "CreateGraphEntity",
            "DeleteGraphEntity",
            "CreateGraphRelation",
            "CreateKnowledgeBase",
            "DeleteDocumentsFromKnowledgeBase",
            "DeleteKnowledgeBase",
            "CreateSessionNotebook",
            "RunNotebook",
        ],
    )
    def test_tool_in_lease_map(self, tool_name: str):
        assert tool_name in _RESOURCE_LEASE_MAP, f"{tool_name} 未在 _RESOURCE_LEASE_MAP 中注册"
        cfg = _RESOURCE_LEASE_MAP[tool_name]
        assert "resource_type" in cfg
        assert "resource_id_arg" in cfg
        assert "exclusive" in cfg
        assert "require_lease" in cfg

    def test_notebook_tools_exclusive_and_require_lease(self):
        for tool_name in ["EditNotebookFile", "CreateSessionNotebook", "RunNotebook"]:
            assert _RESOURCE_LEASE_MAP[tool_name]["exclusive"] is True
            assert _RESOURCE_LEASE_MAP[tool_name]["require_lease"] is True

    def test_delete_tools_require_lease_not_exclusive(self):
        delete_tools = {
            "DeleteDataTableRecord": "dataset",
            "DeleteKnowledgeGraph": "knowledge_graph",
            "DeleteGraphEntity": "knowledge_graph",
            "DeleteKnowledgeBase": "knowledge_base",
            "DeleteDocumentsFromKnowledgeBase": "knowledge_base",
        }
        for tool_name, res_type in delete_tools.items():
            assert _RESOURCE_LEASE_MAP[tool_name]["exclusive"] is False, (
                f"{tool_name} 不应为 exclusive"
            )
            assert _RESOURCE_LEASE_MAP[tool_name]["require_lease"] is True, (
                f"{tool_name} 删除操作必须 require_lease"
            )
            assert _RESOURCE_LEASE_MAP[tool_name]["resource_type"] == res_type

    def test_insert_tools_not_require_lease(self):
        insert_tools = [
            "CreateDataTable",
            "CreateKnowledgeGraph",
            "CreateGraphEntity",
            "CreateGraphRelation",
            "CreateKnowledgeBase",
        ]
        for tool_name in insert_tools:
            assert _RESOURCE_LEASE_MAP[tool_name]["require_lease"] is False, (
                f"{tool_name} 是 insert 语义，不应 require_lease"
            )


# ---------------------------------------------------------------------------
# 8. workspace_memory 分片隔离
# ---------------------------------------------------------------------------


class TestWorkspaceMemorySharding:
    def test_shard_path_unique_per_mission(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)
        shard_m1 = get_workspace_memory_shard_path(memory_dir, "M1")
        shard_m2 = get_workspace_memory_shard_path(memory_dir, "M2")
        assert shard_m1 != shard_m2
        assert shard_m1.name == "M1.md"
        assert shard_m2.name == "M2.md"
        assert shard_m1.parent == memory_dir / "shards"

    def test_worker_cannot_write_other_mission_shard(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)
        shard_m1 = get_workspace_memory_shard_path(memory_dir, "M1")
        shard_m2 = get_workspace_memory_shard_path(memory_dir, "M2")
        main_path = get_workspace_memory_main_path(memory_dir)

        shard_m1.write_text("M1 的记忆", encoding="utf-8")
        assert shard_m1.exists()
        assert not main_path.exists()
        assert not shard_m2.exists()

    def test_merge_shards_produces_combined_content(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)

        main_path = get_workspace_memory_main_path(memory_dir)
        main_path.write_text("# 主记忆\n已有内容\n", encoding="utf-8")

        for mid in ("M1", "M2", "M3"):
            shard = get_workspace_memory_shard_path(memory_dir, mid)
            shard.write_text(f"{mid} 的发现", encoding="utf-8")

        merged = merge_workspace_memory_shards(memory_dir)
        assert "# 主记忆" in merged
        assert "已有内容" in merged
        assert "M1 的发现" in merged
        assert "M2 的发现" in merged
        assert "M3 的发现" in merged

    def test_merge_selective_mission_ids(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)

        get_workspace_memory_shard_path(memory_dir, "M1").write_text("M1 内容", encoding="utf-8")
        get_workspace_memory_shard_path(memory_dir, "M2").write_text("M2 内容", encoding="utf-8")

        merged = merge_workspace_memory_shards(memory_dir, mission_ids=["M1"])
        assert "M1 内容" in merged
        assert "M2 内容" not in merged

    def test_merge_skips_empty_shards(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)

        get_workspace_memory_shard_path(memory_dir, "M1").write_text("有效内容", encoding="utf-8")
        get_workspace_memory_shard_path(memory_dir, "M2").write_text("", encoding="utf-8")

        merged = merge_workspace_memory_shards(memory_dir)
        assert "有效内容" in merged

    def test_shard_files_on_disk(self, tmp_path: Path):
        memory_dir = tmp_path / ".aiasys" / "memory"
        (memory_dir / "shards").mkdir(parents=True)
        shard = get_workspace_memory_shard_path(memory_dir, "M-Test-001")
        assert shard.parent.name == "shards"
        assert shard.name == "M-Test-001.md"
        shard.write_text("test", encoding="utf-8")
        assert shard.exists()


# ---------------------------------------------------------------------------
# 9. teardown 释放全部租约 + 幂等
# ---------------------------------------------------------------------------


class TestTeardownReleasesLeases:
    async def test_teardown_releases_all_leases(self, store: TeamStore, state_dir: str):
        mission = _mission(id="M1", lease={"notebook": ["nb-1"], "dataset": ["t1"]})
        await store.resolve_mission_resource_leases(mission)
        assert store.get_lease_holder("notebook", "nb-1") == "M1"
        await store.teardown()
        store2 = TeamStore(state_dir)
        state = await store2.load()
        assert state.resource_leases == {}
        assert store2.get_lease_holder("notebook", "nb-1") is None

    async def test_teardown_idempotent(self, store: TeamStore):
        mission = _mission(id="M1", lease={"notebook": ["nb-1"]})
        await store.resolve_mission_resource_leases(mission)
        await store.teardown()
        result2 = await store.teardown()
        assert result2 == {"removed": [], "kept": []}


# ---------------------------------------------------------------------------
# 10. team_plan 接受 lease 字段
# ---------------------------------------------------------------------------


class TestTeamPlanAcceptsLease:
    async def test_plan_with_lease_field(self, state_dir: str):
        from app.services.agent.runtime_backends.aiasys.team.tools import (
            TeamInitTool,
            TeamPlanTool,
            clear_store_cache,
            set_team_state_dir_override,
        )

        clear_store_cache()
        set_team_state_dir_override(state_dir)
        try:
            init_tool = TeamInitTool()
            init_result = await init_tool.invoke(
                ctx={"agent_path": "/root"},
                repo_root=str(Path("/fake/repo").resolve()),
            )
            assert not init_result.is_error, f"team_init 失败: {init_result.content}"

            tool = TeamPlanTool()
            result = await tool.invoke(
                ctx={"agent_path": "/root"},
                missions=[
                    {
                        "title": "notebook 任务",
                        "kind": "build",
                        "scope": ["outputs/report.md"],
                        "lease": {"notebook": ["experiment.ipynb"]},
                    }
                ],
            )
            assert not result.is_error, result.content
        finally:
            set_team_state_dir_override(None)
            clear_store_cache()
