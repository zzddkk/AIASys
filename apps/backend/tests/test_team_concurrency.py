"""team 多 agent 真实并发行为测试（asyncio.gather 真并发，非串行逻辑断言）。

与 test_team_lease.py 的分工：那份 724 行、10 个测试类全部是**串行**调用下的逻辑
断言（依次 acquire、检查返回值），零个 asyncio.gather。串行测试能证明「冲突检测
的判断逻辑写对了」，但证明不了「两个 worker 同时抢时只有一个赢」——后者依赖
load-modify-save 全程持锁，而锁的失效在串行调用下完全观测不到。

本文件补的就是这一层：所有用例都用 asyncio.gather 发起真并发，断言的是并发安全
性质（恰好一个赢家 / 无 lost update / 失败路径不写盘），而非单次调用的返回值。

为什么这些测试能捕获真实 bug（每条都用反向探针实测过，见各用例注释）：
TeamStore 的 12 处写操作都是 `async with self._lock: state = await self._load()
... await self._save(state)` 的形态。`_load` 内部走 `asyncio.to_thread` 读文件，
这是一个真实的 await point——协程会在此让出控制权。所以一旦某处漏了锁，两个协程
就会各自读到同一份旧 state、各自修改、依次写回，后写的完全覆盖先写的（lost
update）。这是静默的：不抛异常、不报错，只是改动凭空消失。

覆盖：
- 并发抢同一独占租约：恰好一个赢家，错误信息指向真实持有者
- 失败的 acquire 不污染 state.json（异常在 _save 之前抛出）
- 非独占资源并发放行（多 mission 共享 dataset）
- 并发 set_status 无 lost update
- 重复状态迁移并发去重（防重复启动 worker）
- _store_cache 单例保证（这是实例级锁能生效的前提）
- 跨实例无互斥的设计边界（显式记录，防误以为有文件锁）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.agent.runtime_backends.aiasys.team.store import (
    TeamError,
    TeamMission,
    TeamStore,
    _normalize_lease_key,
)
from app.services.agent.runtime_backends.aiasys.team.tools import (
    _get_store,
    clear_store_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# 并发度。取 16 而非 2：只用 2 个协程时，即便漏锁也有相当概率恰好串行完成而误判
# 通过（事件循环的调度顺序不保证交错）。16 个并发下漏锁必然产生可观测的交错。
CONCURRENCY = 16


@pytest.fixture
def state_dir(tmp_path: Path) -> str:
    return str(tmp_path / "team")


@pytest.fixture
async def store(state_dir: str) -> TeamStore:
    s = TeamStore(state_dir)
    await s.init(repo_root=str(Path("/fake/repo").resolve()), base="main")
    return s


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """每个用例前后都清缓存：_store_cache 是模块级全局，会跨用例泄漏实例。"""
    clear_store_cache()
    yield
    clear_store_cache()


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


async def _gather_results(coros: list[Any]) -> tuple[list[Any], list[BaseException]]:
    """并发执行并把成功与异常分开。

    用 return_exceptions=True 而非 try/except 包每个协程：后者会让异常在协程内部
    就被吞掉，无法区分「没抛异常」和「抛了但被吃了」。
    """
    raw = await asyncio.gather(*coros, return_exceptions=True)
    ok = [r for r in raw if not isinstance(r, BaseException)]
    errs = [r for r in raw if isinstance(r, BaseException)]
    return ok, errs


def _read_state_file(state_dir: str) -> dict[str, Any]:
    """直接读 state.json，绕过 TeamStore 的内存表。

    不用 store.load()：那会返回内存中的 _runtime_lease_table，即便落盘失败也看不
    出来。要验证持久化的正确性必须读原始文件。
    """
    raw = (Path(state_dir) / "state.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


# ---------------------------------------------------------------------------
# 1. 并发抢占同一独占租约
# ---------------------------------------------------------------------------


class TestConcurrentLeaseAcquisition:
    """独占租约的核心承诺：N 个 mission 同时抢，恰好一个赢。"""

    async def test_sixteen_concurrent_acquires_exactly_one_winner(self, store: TeamStore) -> None:
        """16 个 mission 同时抢同一个 notebook，必须恰好 1 成功 15 失败。

        反向探针（2026-08-09 实测）：把 acquire_resource_lease 的
        `async with self._lock:` 改成 `if True:`，本条从 passed 变为
        `assert 16 == 1` —— 16 个协程全部拿到租约。这正是漏锁的真实后果：
        16 个 worker 同时以为自己独占了同一个 notebook。
        """
        coros = [
            store.acquire_resource_lease(
                mission_id=f"M{i}",
                resource_type="notebook",
                resource_id="nb-shared",
                lease_key="notebook:nb-shared",
                exclusive=True,
            )
            for i in range(CONCURRENCY)
        ]
        ok, errs = await _gather_results(coros)

        assert len(ok) == 1, f"独占租约应恰好一个赢家，实际 {len(ok)} 个成功"
        assert len(errs) == CONCURRENCY - 1
        assert all(isinstance(e, TeamError) for e in errs), (
            "败者应该拿到 TeamError（业务拒绝），而不是别的异常类型——"
            f"实际类型：{ {type(e).__name__ for e in errs} }"
        )

    async def test_persisted_holder_is_unique_and_consistent(
        self, store: TeamStore, state_dir: str
    ) -> None:
        """落盘的租约表里该资源只有一条记录，且与内存表一致。

        单独立这条而不是并进上一条：上一条测的是「调用返回值」层面只有一个赢家，
        这条测的是「持久化状态」层面没有并发写坏。两者可以独立失效——例如内存表
        用了锁但 _save 在锁外，返回值正确而 state.json 被交错写坏。
        """
        coros = [
            store.acquire_resource_lease(
                mission_id=f"M{i}",
                resource_type="notebook",
                resource_id="nb-x",
                lease_key="notebook:nb-x",
                exclusive=True,
            )
            for i in range(CONCURRENCY)
        ]
        await _gather_results(coros)

        norm_key = _normalize_lease_key("notebook", "nb-x")
        persisted = _read_state_file(state_dir)["resource_leases"]
        assert list(persisted.keys()) == [norm_key], (
            f"落盘的租约键应只有 {norm_key} 一个，实际 {list(persisted.keys())}"
        )
        assert persisted[norm_key]["mission_id"] == store.get_lease_holder("notebook", "nb-x"), (
            "落盘的持有者与内存表不一致——说明 _save 与内存表更新之间存在竞态"
        )

    async def test_loser_error_names_the_actual_holder(self, store: TeamStore) -> None:
        """败者的报错必须指向真正的持有者，不能是随便某个 mission id。

        这条防的是「错误信息用了自己的 mission_id」这类拼错——串行测试里
        持有者恰好是唯一的另一方，报错写谁都对，并发下才能暴露。
        """
        coros = [
            store.acquire_resource_lease(
                mission_id=f"M{i}",
                resource_type="notebook",
                resource_id="nb-y",
                lease_key="notebook:nb-y",
                exclusive=True,
            )
            for i in range(CONCURRENCY)
        ]
        _, errs = await _gather_results(coros)

        # 先钉住「确实有败者」再逐条检查报错内容。少了这一句，本条在互斥失效时
        # （errs 为空、16 个全赢）会因为 for 循环空转而恒真通过——2026-08-09 的
        # 反向探针实测到过：打掉 acquire 的锁后另外 3 条变红，唯独这条仍绿。
        # 遍历集合做断言时，空集合永远满足全体断言，必须先断言非空。
        assert len(errs) == CONCURRENCY - 1, (
            f"应有 {CONCURRENCY - 1} 个败者，实际 {len(errs)} 个——互斥已失效，"
            "下面的报错内容检查无从谈起"
        )

        holder = store.get_lease_holder("notebook", "nb-y")
        assert holder is not None
        for e in errs:
            assert holder in str(e), f"报错未指向真实持有者 {holder}：{e}"

    async def test_failed_acquire_does_not_pollute_state_file(
        self, store: TeamStore, state_dir: str
    ) -> None:
        """15 次失败的 acquire 不能在 state.json 里留下任何痕迹。

        acquire 的实现是「先检测冲突抛错、后 _save」，所以失败路径根本走不到写盘。
        这条锚定该顺序：若将来有人把 _save 提到冲突检测之前（比如为了「记录尝试
        历史」），败者的 entry 会被写进去，最后一个败者的 mission_id 甚至会成为
        state.json 里的持有者，与内存表分叉。
        """
        coros = [
            store.acquire_resource_lease(
                mission_id=f"M{i}",
                resource_type="notebook",
                resource_id="nb-z",
                lease_key="notebook:nb-z",
                exclusive=True,
            )
            for i in range(CONCURRENCY)
        ]
        ok, errs = await _gather_results(coros)
        assert len(ok) == 1 and len(errs) == CONCURRENCY - 1

        persisted = _read_state_file(state_dir)["resource_leases"]
        assert len(persisted) == 1, f"失败的 acquire 污染了落盘状态：{persisted}"

    async def test_non_exclusive_resource_allows_all_concurrent_holders(
        self, store: TeamStore
    ) -> None:
        """非独占资源（dataset）并发申请应全部成功——对照组。

        没有这条对照，上面几条「恰好一个赢家」的测试有个漏洞：把 acquire 改成
        无条件抛错也能让它们通过（0 个赢家？不，len(ok)==1 会失败）。更实际的
        风险是把 exclusive 参数忽略掉一律独占——那样上面全绿，只有这条会红。
        """
        coros = [
            store.acquire_resource_lease(
                mission_id=f"M{i}",
                resource_type="dataset",
                resource_id="ds-shared",
                lease_key="dataset:ds-shared",
                exclusive=False,
            )
            for i in range(CONCURRENCY)
        ]
        ok, errs = await _gather_results(coros)

        assert len(errs) == 0, f"非独占资源不应拒绝任何申请，却抛了 {errs[:2]}"
        assert len(ok) == CONCURRENCY


# ---------------------------------------------------------------------------
# 2. 并发状态迁移
# ---------------------------------------------------------------------------


class TestConcurrentStatusTransitions:
    """set_status 是 load-modify-save，是 lost update 的头号候选。"""

    async def test_concurrent_set_status_no_lost_update(self, store: TeamStore) -> None:
        """12 个不同 mission 并发转 active，12 个都必须落盘。

        这是 lost update 的经典形态：每个协程读到同一份旧 state、只改自己那个
        mission、然后整份写回，于是只有最后一个写入者的改动存活。

        反向探针（2026-08-09 实测）：去掉 set_status 的 `async with self._lock:`，
        本条从 passed 变为 `assert 1 == 12` —— 12 个改动里只有 1 个存活，另外
        11 个凭空消失。既不报错也不警告，正是这类 bug 最难查的原因。
        """
        n = 12
        await store.plan([_mission(title=f"t{i}", scope=[f"src/mod{i}/"]) for i in range(n)])

        ok, errs = await _gather_results(
            [store.set_status(f"M{i}", "active") for i in range(1, n + 1)]
        )
        assert len(errs) == 0, f"合法迁移不应失败：{errs[:2]}"
        assert len(ok) == n

        persisted = _read_state_file(store._state_dir.as_posix())["missions"]
        actives = [m["id"] for m in persisted if m["status"] == "active"]
        assert len(actives) == n, (
            f"lost update：{n} 个 mission 并发转 active，落盘只剩 {len(actives)} 个"
            f"（{sorted(actives)}）"
        )

    async def test_duplicate_transition_only_one_succeeds(self, store: TeamStore) -> None:
        """对同一 mission 并发发起同一迁移，只有一个能成功。

        真实场景：用户或上层逻辑重复触发 team_spawn（双击、重试、respawn 竞态）。
        期望是第二次起被状态机拒绝，而不是「启动两个 worker 抢同一个工作间」。

        机制上依赖 planned→active 合法而 active→active 不合法（VALID_TRANSITIONS
        里 active 的合法目标是 completed/blocked/paused）。所以第一个协程改完状态
        后，其余协程读到 active 就会被拒。锁失效时它们全都读到 planned，全部放行。

        反向探针（2026-08-09 实测）：去掉 set_status 的锁，本条从 passed 变为
        `assert 16 == 1`，16 个协程全部成功启动同一个 mission。
        """
        await store.plan([_mission(title="solo", scope=["src/solo/"])])

        ok, errs = await _gather_results(
            [store.set_status("M1", "active") for _ in range(CONCURRENCY)]
        )

        assert len(ok) == 1, (
            f"同一迁移并发触发应只有一次生效，实际 {len(ok)} 次成功——"
            "会导致同一 mission 启动多个 worker"
        )
        assert len(errs) == CONCURRENCY - 1
        assert all(isinstance(e, TeamError) for e in errs)

    async def test_dependency_gate_holds_under_concurrency(self, store: TeamStore) -> None:
        """依赖未 merged 时，并发启动也必须全部被拒。

        依赖门控是「系统门，非 prompt 约束」（store.py 原注释）。串行下已有测试
        覆盖，但门控读的是 state.missions 里依赖项的状态——并发下若锁失效，可能
        读到中间态。这条确保门控在并发下同样不可绕过。
        """
        await store.plan([_mission(title="base", scope=["src/base/"])])
        await store.plan([_mission(title="dependent", scope=["src/dep/"], deps=["M1"])])

        ok, errs = await _gather_results(
            [store.set_status("M2", "active") for _ in range(CONCURRENCY)]
        )

        assert len(ok) == 0, "依赖未 merged，任何一次启动都不该成功"
        assert len(errs) == CONCURRENCY
        assert all("尚未全部 merged" in str(e) for e in errs), (
            f"应因依赖门控被拒，实际错误：{ {str(e)[:40] for e in errs} }"
        )


# ---------------------------------------------------------------------------
# 3. _store_cache 单例保证
# ---------------------------------------------------------------------------


class TestStoreCacheSingleton:
    """TeamStore 的锁是实例级的（self._lock），所以「同一 state_dir 只有一个
    实例」是全部并发安全性的前提。这个前提由 tools._store_cache 提供。"""

    async def test_concurrent_get_store_returns_identical_instance(self, state_dir: str) -> None:
        """并发 _get_store 必须返回同一个对象（is 判等，不是 ==）。

        这条是上面所有租约测试的地基：TeamStore 用实例级 asyncio.Lock 做互斥，
        如果并发下产生了两个实例，就有两把互不相干的锁，「恰好一个赢家」的保证
        立刻失效——而且失效方式是静默的。

        _get_store 是 check-then-act：`if key in cache: return` 与
        `cache[key] = store` 之间没有 await point，靠 asyncio 单线程不抢占才侥幸
        原子。用 32 并发把这个不变式钉住：将来若有人在中间插入 await（例如给
        TeamStore 加异步初始化），本条立刻变红。
        """
        stores = await asyncio.gather(*[_get_store(state_dir) for _ in range(32)])

        first = stores[0]
        assert all(s is first for s in stores), (
            "并发 _get_store 返回了多个不同实例——实例级锁失效，租约互斥与 lost update 防护同时失效"
        )

    async def test_distinct_state_dirs_get_distinct_stores(self, tmp_path: Path) -> None:
        """不同 state_dir 必须是不同实例——对照组。

        防的是「缓存键写错」（例如忽略 state_dir 一律返回同一个 store），那样
        上一条会通过而多用户/多工作区之间会串状态。
        """
        a, b = str(tmp_path / "t1"), str(tmp_path / "t2")
        s1, s2 = await asyncio.gather(_get_store(a), _get_store(b))
        assert s1 is not s2, "不同 state_dir 复用了同一个 TeamStore，会串状态"


# ---------------------------------------------------------------------------
# 4. acquire / release 交错
# ---------------------------------------------------------------------------


class TestConcurrentAcquireRelease:
    async def test_acquire_release_interleaved_leaves_no_residue(
        self, store: TeamStore, state_dir: str
    ) -> None:
        """一批 mission 各自 acquire 自己的资源再全部 release，最终表必须为空。

        每个 mission 用不同 resource_id，所以全部 acquire 都该成功；随后全部
        release。若 release 的删除操作与 _save 之间有竞态，会残留幽灵租约——
        那会导致该资源此后永久无法被任何 mission 申请（死锁）。
        """
        n = 12
        await _gather_results(
            [
                store.acquire_resource_lease(
                    mission_id=f"M{i}",
                    resource_type="notebook",
                    resource_id=f"nb-{i}",
                    lease_key=f"notebook:nb-{i}",
                    exclusive=True,
                )
                for i in range(n)
            ]
        )
        assert len(_read_state_file(state_dir)["resource_leases"]) == n

        await _gather_results(
            [
                store.release_resource_lease(
                    mission_id=f"M{i}", resource_type="notebook", resource_id=f"nb-{i}"
                )
                for i in range(n)
            ]
        )

        persisted = _read_state_file(state_dir)["resource_leases"]
        assert persisted == {}, f"release 后残留幽灵租约（该资源将永久不可申请）：{persisted}"

    async def test_release_by_non_holder_is_noop_under_concurrency(self, store: TeamStore) -> None:
        """非持有者并发 release 不能抢走别人的租约。

        release 的实现校验了 mission_id 匹配才删除。这条确保该校验在并发下有效：
        否则任何 mission 都能释放别人的独占租约，独占语义直接崩塌。
        """
        await store.acquire_resource_lease(
            mission_id="M1",
            resource_type="notebook",
            resource_id="nb-owned",
            lease_key="notebook:nb-owned",
            exclusive=True,
        )

        await _gather_results(
            [
                store.release_resource_lease(
                    mission_id=f"M{i}", resource_type="notebook", resource_id="nb-owned"
                )
                for i in range(2, CONCURRENCY + 2)
            ]
        )

        assert store.get_lease_holder("notebook", "nb-owned") == "M1", (
            "非持有者的 release 抢走了 M1 的租约——独占语义崩塌"
        )


# ---------------------------------------------------------------------------
# 5. 设计边界：跨实例无互斥
# ---------------------------------------------------------------------------


class TestCrossInstanceLimitation:
    """显式记录当前的并发保护边界，避免误以为有文件级锁。"""

    async def test_two_independent_instances_do_not_mutually_exclude(self, state_dir: str) -> None:
        """两个手工 new 出来的 TeamStore 指向同一 state_dir 时，互斥失效。

        这不是 bug，是当前设计的边界：互斥由实例级 asyncio.Lock 提供，跨实例
        （以及跨进程）没有任何保护。生产路径上由 tools._store_cache 保证同一
        state_dir 只有一个实例，所以边界成立（见 TestStoreCacheSingleton）。

        把它写成测试而不是只写注释，是为了让边界可执行：如果将来引入文件锁或
        跨进程协调（多 worker 进程模型），本条会变红，届时应当改成断言「跨实例
        也只有一个赢家」而不是删掉它。
        """
        s1 = TeamStore(state_dir)
        await s1.init(repo_root=str(Path("/fake/repo").resolve()), base="main")
        s2 = TeamStore(state_dir)

        ok, _ = await _gather_results(
            [
                s1.acquire_resource_lease(
                    mission_id="M1",
                    resource_type="notebook",
                    resource_id="nb-cross",
                    lease_key="notebook:nb-cross",
                    exclusive=True,
                ),
                s2.acquire_resource_lease(
                    mission_id="M2",
                    resource_type="notebook",
                    resource_id="nb-cross",
                    lease_key="notebook:nb-cross",
                    exclusive=True,
                ),
            ]
        )

        assert len(ok) == 2, (
            "跨实例互斥的行为变了。如果这是因为引入了文件锁或跨进程协调，"
            "请把本条改为断言「恰好一个赢家」，不要直接删除。"
        )
