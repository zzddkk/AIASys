"""TeamStore 的单元测试。

覆盖：
- 六态每条合法迁移
- 至少三条非法迁移
- 依赖未满足时拒绝 activate
- 依赖引用不存在 id 时拒绝
- scope 重叠被拒（含 src/data vs src/database 易错例）
- survey 空 scope 放行
- 原子写后能正确恢复
- 并发访问不损坏状态
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.agent.runtime_backends.aiasys.team.store import (
    TeamError,
    TeamMission,
    TeamStore,
    _scope_covers_path,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> str:
    """临时团队状态目录。"""
    return str(tmp_path / "team")


@pytest.fixture
async def store(state_dir: str) -> TeamStore:
    """已初始化的 TeamStore。"""
    s = TeamStore(state_dir)
    await s.init(repo_root="/fake/repo", base="main")
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mission(**kwargs: object) -> TeamMission:
    """快速构造 TeamMission，默认填入必填字段。"""
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
        "created_at": "2026-01-01T00:00:00",
    }
    data.update(kwargs)
    return TeamMission(**data)


async def _plan_and_get_id(store: TeamStore, m: TeamMission) -> str:
    """plan 一个任务并返回分配后的 id。"""
    result = await store.plan([m])
    return result[0].id


async def _set_status(store: TeamStore, mission_id: str, status: str) -> None:
    await store.set_status(mission_id, status)


# ---------------------------------------------------------------------------
# 1. State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    """六态状态机：合法迁移与非法迁移。"""

    @pytest.mark.parametrize(
        "src,dst",
        [
            ("planned", "active"),
            ("planned", "blocked"),
            ("planned", "paused"),
            ("active", "completed"),
            ("active", "blocked"),
            ("active", "paused"),
            ("blocked", "active"),
            ("paused", "active"),
            ("completed", "active"),
            ("completed", "merged"),
            ("blocked", "merged"),
        ],
    )
    async def test_合法迁移(self, store: TeamStore, src: str, dst: str):
        """每条合法迁移都应该成功。"""
        m = _mission(status=src, kind="survey", scope=[])
        mid = await _plan_and_get_id(store, m)
        # 先把状态推到 src（如果不是 planned）
        if src == "active":
            pass  # 已经是 active
        elif src == "completed":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "completed")
        elif src == "blocked":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "blocked")
        elif src == "paused":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "paused")
        elif src == "merged":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "completed")
            await _set_status(store, mid, "merged")
        result = await store.set_status(mid, dst)
        assert result.status == dst

    @pytest.mark.parametrize(
        "src,dst",
        [
            ("merged", "active"),
            ("merged", "planned"),
            ("active", "planned"),
            ("completed", "planned"),
            ("blocked", "planned"),
            ("paused", "planned"),
            ("paused", "completed"),
            ("active", "merged"),
        ],
    )
    async def test_非法迁移被拒绝(self, store: TeamStore, src: str, dst: str):
        """非法迁移应抛 TeamError。"""
        m = _mission(status="planned", kind="survey", scope=[])
        mid = await _plan_and_get_id(store, m)
        # 先把状态推到 src
        if src == "active":
            await _set_status(store, mid, "active")
        elif src == "completed":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "completed")
        elif src == "blocked":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "blocked")
        elif src == "paused":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "paused")
        elif src == "merged":
            await _set_status(store, mid, "active")
            await _set_status(store, mid, "completed")
            await _set_status(store, mid, "merged")

        with pytest.raises(TeamError, match="不允许迁移"):
            await store.set_status(mid, dst)


# ---------------------------------------------------------------------------
# 2. Dependency gating
# ---------------------------------------------------------------------------


class TestDependencyGating:
    async def test_依赖未满足时拒绝_activate(self, store: TeamStore):
        """依赖未全部 merged 时拒绝切到 active。"""
        dep = _mission(status="completed", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(deps=[dep_id], status="planned", kind="build", scope=["src/x"])
        m_id = await _plan_and_get_id(store, m)
        # dep 还没 merged
        with pytest.raises(TeamError, match="尚未全部 merged"):
            await store.set_status(m_id, "active")

    async def test_依赖引用不存在的_id_被拒绝(self, store: TeamStore):
        """依赖引用了不存在的 mission id 时在 plan 阶段就拒绝。"""
        m = _mission(deps=["ghost"], status="planned", kind="build", scope=["src/x"])
        with pytest.raises(TeamError, match="依赖了不存在的任务"):
            await store.plan([m])

    async def test_依赖全部_merged_后允许_activate(self, store: TeamStore):
        """依赖全部 merged 后可以激活。"""
        dep = _mission(status="merged", kind="build", scope=["src/x"])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(deps=[dep_id], status="planned", kind="build", scope=["src/y"])
        m_id = await _plan_and_get_id(store, m)
        result = await store.set_status(m_id, "active")
        assert result.status == "active"


# ---------------------------------------------------------------------------
# 3. Scope overlap
# ---------------------------------------------------------------------------


class TestScopeOverlap:
    async def test_build_scope重叠被拒(self, store: TeamStore):
        """build 类 scope 重叠时整批拒绝。"""
        m1 = _mission(kind="build", scope=["outputs/a"])
        m2 = _mission(kind="build", scope=["outputs/a/b"])
        with pytest.raises(TeamError, match="重叠"):
            await store.plan([m1, m2])

    async def test_survey_空scope放行(self, store: TeamStore):
        """survey 类允许 scope 为空（只读不占位）。"""
        m1 = _mission(kind="survey", scope=[])
        m2 = _mission(kind="survey", scope=[])
        result = await store.plan([m1, m2])
        assert len(result) == 2

    async def test_survey与build不冲突(self, store: TeamStore):
        """survey 不参与互斥检查，与 build 同 scope 也放行。"""
        m1 = _mission(kind="build", scope=["src/data"])
        m2 = _mission(kind="survey", scope=["src/data"])
        result = await store.plan([m1, m2])
        assert len(result) == 2

    async def test_src_data_与_src_database_不误匹配(self, store: TeamStore):
        """`src/data` 不误匹配 `src/database`（路径分量前缀比对）。"""
        m1 = _mission(kind="build", scope=["src/data"])
        m2 = _mission(kind="build", scope=["src/database"])
        result = await store.plan([m1, m2])
        assert len(result) == 2

    async def test_路径规范化后符号链接绕过失败(self, store: TeamStore):
        """路径比对前用 os.path.realpath() 规范化，符号链接无法绕过。"""
        # 用实际存在的路径来模拟：创建一个真实目录和同名符号链接
        real_dir = Path(store._state_dir) / "real"
        link_dir = Path(store._state_dir) / "link"
        real_dir.mkdir()
        try:
            link_dir.symlink_to(real_dir, target_is_directory=True)
        except OSError:
            # Windows 可能需要管理员权限，跳过
            pytest.skip("symlink not supported on this system")

        m1 = _mission(kind="build", scope=[str(real_dir)])
        m2 = _mission(kind="build", scope=[str(link_dir)])
        with pytest.raises(TeamError, match="重叠"):
            await store.plan([m1, m2])

    @pytest.mark.parametrize(
        "scope,file,expected",
        [
            ("src/data", "src/data/x.ts", True),
            ("src/data/", "src/data/x.ts", True),
            ("src/data", "src/data", True),
            ("src/data", "src/database/x.ts", False),
            ("src/data", "src/data2/x.ts", False),
            ("outputs/a", "outputs/a/b/c.txt", True),
            ("outputs/a", "outputs/ab/c.txt", False),
        ],
    )
    def test_scopeCoversPath_parametrized(self, scope: str, file: str, expected: bool):
        """路径分量前缀比对的参数化验证。"""
        assert _scope_covers_path(scope, file) == expected


# ---------------------------------------------------------------------------
# 4. Atomic write + recovery
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    async def test_atomic_write_and_reload(self, state_dir: str):
        """原子写后能正确恢复状态。"""
        store = TeamStore(state_dir)
        await store.init(repo_root="/fake", base="main")

        m = _mission(status="completed", kind="build", scope=["src/x"])
        await store.plan([m])

        # 重新加载验证
        state = await store.load()
        assert len(state.missions) == 1
        assert state.missions[0].status == "completed"

    async def test_init_is_idempotent(self, state_dir: str):
        """重复 init 不应覆盖已有状态。"""
        store = TeamStore(state_dir)
        await store.init(repo_root="/fake", base="main")
        await store.plan([_mission(status="active", kind="build", scope=["x"])])

        # 再次 init
        store2 = TeamStore(state_dir)
        await store2.init(repo_root="/fake", base="main")
        state = await store2.load()
        assert len(state.missions) == 1


# ---------------------------------------------------------------------------
# 5. Concurrency safety
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_并发访问不损坏状态(self, state_dir: str):
        """多协程并发读写，状态文件不损坏。"""
        store = TeamStore(state_dir)
        await store.init(repo_root="/fake", base="main")

        async def add_mission(idx: int) -> None:
            m = _mission(status="planned", kind="build", scope=[f"src/task{idx}"])
            await store.plan([m])

        # 50 个协程并发 plan
        await asyncio.gather(*[add_mission(i) for i in range(50)])

        state = await store.load()
        assert len(state.missions) == 50
        ids = {m.id for m in state.missions}
        assert len(ids) == 50  # 没有重复 id


# ---------------------------------------------------------------------------
# 6. Merge gates（六道门中的可测部分）
# ---------------------------------------------------------------------------


class TestMergeGates:
    async def test_gate0_empty_artifacts_rejected(self, store: TeamStore):
        """Gate 0: task with no artifacts is rejected."""
        dep = _mission(status="merged", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(
            deps=[dep_id],
            status="completed",
            kind="build",
            scope=["src/x"],
            lease={"files": ["src/x"]},
        )
        m_id = await _plan_and_get_id(store, m)
        with pytest.raises(TeamError, match="门〇"):
            await store.merge(m_id, reviewed_commit="fp1", artifacts=[])

    async def test_gate1_unreviewed_rejected(self, store: TeamStore):
        """Gate 1: missing reviewed_commit is rejected."""
        dep = _mission(status="merged", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(deps=[dep_id], status="completed", kind="build", scope=["src/x"])
        m_id = await _plan_and_get_id(store, m)
        with pytest.raises(TeamError, match="门①"):
            await store.merge(m_id, reviewed_commit=None)

    async def test_gate4_unmerged_deps_rejected(self, store: TeamStore):
        """Gate 4: unmerged dependencies block merge."""
        dep = _mission(status="completed", kind="survey", scope=[])  # not merged
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(deps=[dep_id], status="completed", kind="build", scope=["src/x"])
        m_id = await _plan_and_get_id(store, m)
        with pytest.raises(TeamError, match="门④"):
            await store.merge(m_id, reviewed_commit="fp1")

    async def test_gate5_out_of_scope_rejected(self, store: TeamStore):
        """Gate 5: artifacts outside lease scope are rejected."""
        dep = _mission(status="merged", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(
            deps=[dep_id],
            status="completed",
            kind="build",
            scope=["src/x"],
            lease={"files": ["src/x"]},
        )
        m_id = await _plan_and_get_id(store, m)
        artifacts = [{"path": "src/y/file.txt", "size": 100, "hash": "abc"}]
        # 先算 fingerprint，让门③通过，才能测到门⑤
        from app.services.agent.runtime_backends.aiasys.team.store import (
            _compute_artifacts_fingerprint,
        )

        fp = _compute_artifacts_fingerprint(artifacts)
        with pytest.raises(TeamError, match="门⑤"):
            await store.merge(m_id, reviewed_commit=fp, artifacts=artifacts)

    async def test_gate3_fingerprint_changed_rejected(self, store: TeamStore):
        """Gate 3: fingerprint mismatch blocks merge."""
        dep = _mission(status="merged", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(
            deps=[dep_id],
            status="completed",
            kind="build",
            scope=["src/x"],
            lease={"files": ["src/x"]},
        )
        m_id = await _plan_and_get_id(store, m)
        artifacts = [{"path": "src/x/file.txt", "size": 100, "hash": "hash1"}]
        with pytest.raises(TeamError, match="门③"):
            await store.merge(m_id, reviewed_commit="different_fp", artifacts=artifacts)

    async def test_all_gates_pass(self, store: TeamStore):
        """Six gates all pass, mission successfully merged."""
        dep = _mission(status="merged", kind="survey", scope=[])
        dep_id = await _plan_and_get_id(store, dep)
        m = _mission(
            deps=[dep_id],
            status="completed",
            kind="build",
            scope=["src/x"],
            lease={"files": ["src/x"]},
        )
        m_id = await _plan_and_get_id(store, m)
        # 构造匹配的指纹：先算当前 fingerprint，再传入
        artifacts = [{"path": "src/x/file.txt", "size": 100, "hash": "h1"}]
        from app.services.agent.runtime_backends.aiasys.team.store import (
            _compute_artifacts_fingerprint,
        )

        fp = _compute_artifacts_fingerprint(artifacts)
        result = await store.merge(m_id, reviewed_commit=fp, artifacts=artifacts)
        assert "conflictsWith" in result
