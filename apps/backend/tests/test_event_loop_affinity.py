"""事件循环亲和性约束（模块级 asyncio 同步原语禁令）。

## 背景：一个自己引爆的缺陷

2026-08-09 给 team/tools.py 的 `_get_store` 补 double-checked locking 时，用上了
早已声明但零处引用的 `_store_cache_lock = asyncio.Lock()`。补完后 13 条并发测试
全绿，但把「临界区内插入一个 await」的探针打进去，第二个测试用例直接炸：

    File "asyncio/locks.py", line 105, in acquire
        fut = self._get_loop().create_future()
    RuntimeError: ... is bound to a different event loop

## 机制

Python 3.10 起 asyncio 同步原语不再接受 loop 参数，改为**首次真实 acquire 时**
惰性绑定当前事件循环（`_get_loop()` 会把 loop 记进对象并在后续校验一致性）。于是
模块级的 asyncio.Lock 有两个致命性质叠在一起：

1. 它跨事件循环存活（模块只导入一次），而 pytest-asyncio 为每个用例新建一个循环，
   生产里也可能多次 `asyncio.run`（脚本入口、后台 worker、同步桥接层）。
2. 绑定只发生在**真正需要等待**的时候。无竞争时 `acquire()` 走快路径直接返回，
   压根不调 `_get_loop()`。

叠加结果就是最坏的一类 bug：平时完全正常，只在第一次发生真实锁争抢的那一刻抛
RuntimeError，且抛在哪个循环里取决于运行时序。测试全绿不能证明它不存在——它需要
「跨循环」与「真争抢」同时成立才显形。

## 为什么这些锁当时提供的保护是零

asyncio 是单线程协作式调度：临界区内没有 await point，就不会被其他协程抢占，本来
就是原子的。普查到的两处（team/tools.py 的 `_store_cache_lock`、
tools/task_tool.py 的 `_background_tasks_lock`）临界区都是单行字典操作，没有任何
await。所以这两把 asyncio.Lock 既没有提供互斥（本来就不需要），又埋下了跨循环
崩溃的引信——纯负收益。

## 结论与规则

保护同步内存临界区一律用 `threading.Lock`：跨事件循环安全，且真正防住从
`asyncio.to_thread` 里调进来的多线程竞争。代价是不能持锁跨 await（threading.Lock
会阻塞整个线程导致死锁），但这恰好是我们想要的约束——模块级共享状态的临界区就该
短小且无 IO。

真需要在临界区内 await 时，不要退回模块级 asyncio.Lock，应当把锁下沉为「每事件
循环一把」或改造成实例级并保证实例不跨循环复用。

## 已知的相邻风险（本文件不拦）

`TeamStore.__init__` 里的 `self._lock = asyncio.Lock()` 是实例级的，正常情况下每
个循环用自己的实例，安全。但这些实例被 `tools._store_cache` 模块级缓存跨循环复用，
理论上有同样的隐患。当前不构成问题：生产是单循环，测试每个用例都 clear_store_cache
（见 test_team_concurrency.py 的 autouse fixture）。若将来引入多循环运行模型，这里
需要一并处理。
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path

from app.services.agent.runtime_backends.aiasys.team.tools import _store_cache_lock
from app.services.agent.runtime_backends.aiasys.tools.task_tool import (
    _background_tasks_lock,
)

# asyncio 里那些会惰性绑定事件循环的同步原语。
# Queue 在 3.10+ 同样通过 _get_loop 绑定；Condition/Semaphore 内部持有 Lock。
_LOOP_BOUND_PRIMITIVES = frozenset(
    {
        "Lock",
        "Event",
        "Condition",
        "Semaphore",
        "BoundedSemaphore",
        "Queue",
        "LifoQueue",
        "PriorityQueue",
        "Barrier",
    }
)

_APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# threading.Lock() 返回的是 _thread.lock，没有公开类名可直接 isinstance，
# 用一个实例取类型。
_ThreadLockType = type(threading.Lock())


def _iter_app_modules() -> list[Path]:
    return sorted(p for p in _APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _module_level_loop_bound_assignments(tree: ast.Module) -> list[tuple[str, int]]:
    """找出模块级（含类体）创建 asyncio 同步原语的赋值。

    只看 Module.body 和 ClassDef.body：这两处的对象生命周期跨事件循环。函数体内
    创建的（包括 __init__ 里的 self._lock）不在范围内——那些随实例走，由实例的
    生命周期决定，不是本约束要管的问题。
    """
    hits: list[tuple[str, int]] = []

    def _check_call(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        # 形态一：asyncio.Lock()
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
            and func.attr in _LOOP_BOUND_PRIMITIVES
        ):
            return f"asyncio.{func.attr}"
        # 形态二：from asyncio import Lock 之后直接 Lock()
        # 这里只按名字判断，可能误伤 threading.Lock 的同名导入，所以在断言里
        # 会把命中行原文一起报出来供人工判断。
        if isinstance(func, ast.Name) and func.id in _LOOP_BOUND_PRIMITIVES:
            return func.id
        return None

    def _scan_body(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = stmt.value
                if value is not None:
                    name = _check_call(value)
                    if name is not None:
                        hits.append((name, stmt.lineno))
            elif isinstance(stmt, ast.ClassDef):
                _scan_body(stmt.body)

    _scan_body(tree.body)
    return hits


class TestNoModuleLevelAsyncioPrimitives:
    """全库扫描：禁止模块级 / 类级的 asyncio 同步原语。"""

    def test_app_tree_has_no_module_level_loop_bound_primitives(self) -> None:
        """扫 app/ 下全部 .py，模块级 asyncio 同步原语必须为零。

        这条是本文件的主承重项：它不是守住某个已修的点，而是守住整类缺陷不再进来。
        逐个修已知的两处只解决当下，扫描能拦住下一个人明天新写的第三处。

        反向探针（2026-08-09 实测）：把 team/tools.py 第 72 行改回
        `_store_cache_lock = asyncio.Lock()`，本条从 passed 变为 1 failed，
        报错里直接指出文件、行号和原语类型。
        """
        offenders: list[str] = []
        scanned = 0
        for path in _iter_app_modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                # 解析失败不静默跳过：那会让扫描范围悄悄缩小，属于「空结果」陷阱
                offenders.append(f"{path}: 无法解析（{type(exc).__name__}: {exc}）")
                continue
            scanned += 1
            for name, lineno in _module_level_loop_bound_assignments(tree):
                rel = path.relative_to(_APP_ROOT.parent)
                source_line = path.read_text(encoding="utf-8").splitlines()[lineno - 1]
                offenders.append(f"{rel}:{lineno}: {name} —— {source_line.strip()}")

        assert not offenders, (
            "发现模块级 asyncio 同步原语（跨事件循环会抛 "
            "'bound to a different event loop'，且只在首次真实争抢时显形）。\n"
            "保护同步临界区请改用 threading.Lock；确实需要在临界区内 await 的，"
            "改为每事件循环一把或实例级并保证实例不跨循环。\n详见本文件模块头注释。\n"
            + "\n".join(f"  - {o}" for o in offenders)
        )

    def test_scan_actually_covers_the_tree(self) -> None:
        """扫描器自身的有效性验证：确认它真的读到了大量文件。

        「零违规」有两种解释：真的没有，或者扫描根本没跑到文件。没有这条，
        上一条在 _APP_ROOT 路径写错时会以「扫了 0 个文件、零违规」的形式恒绿。
        """
        modules = _iter_app_modules()
        assert len(modules) > 100, (
            f"只扫到 {len(modules)} 个模块，扫描范围可疑（_APP_ROOT={_APP_ROOT}）——"
            "零违规的结论不可采信"
        )

    def test_scanner_detects_a_known_positive(self, tmp_path: Path) -> None:
        """扫描器有效性验证之二：喂一个已知违规样本，必须被抓出。

        这是「空结果不等于不存在」的正面校验——先证明检索方法对已知阳性有效，
        再采信它在真实代码上给出的零结果。上一条验证覆盖范围，这条验证识别能力。
        """
        sample = tmp_path / "offender.py"
        sample.write_text(
            "import asyncio\n"
            "_bad_lock = asyncio.Lock()\n"
            "_bad_queue: asyncio.Queue = asyncio.Queue()\n"
            "class C:\n"
            "    shared = asyncio.Event()\n"
            "def f():\n"
            "    local_ok = asyncio.Lock()\n"
            "    return local_ok\n",
            encoding="utf-8",
        )
        tree = ast.parse(sample.read_text(encoding="utf-8"))
        hits = _module_level_loop_bound_assignments(tree)
        names = [n for n, _ in hits]

        assert names == ["asyncio.Lock", "asyncio.Queue", "asyncio.Event"], (
            f"扫描器漏检或误检：{hits}"
        )
        assert all(lineno != 7 for _, lineno in hits), (
            "函数体内的局部 asyncio.Lock 被误判为违规——那是安全的，不该拦"
        )


class TestLockTypes:
    """两处已修的锁：类型断言，防止被改回去。"""

    def test_store_cache_lock_is_threading_lock(self) -> None:
        """team/tools.py 的缓存锁必须是 threading.Lock。

        单独立这条而不只依赖全库扫描：扫描按 AST 静态匹配，若有人写成
        `_store_cache_lock = _make_lock()` 这类间接构造，静态扫描看不出来，
        运行时类型断言能。
        """
        assert isinstance(_store_cache_lock, _ThreadLockType), (
            f"_store_cache_lock 类型是 {type(_store_cache_lock)}，应为 threading.Lock。"
            "它保护的是 _store_cache 这个跨事件循环存活的模块级字典。"
        )

    def test_background_tasks_lock_is_threading_lock(self) -> None:
        """task_tool.py 的后台任务表锁必须是 threading.Lock。"""
        assert isinstance(_background_tasks_lock, _ThreadLockType), (
            f"_background_tasks_lock 类型是 {type(_background_tasks_lock)}，应为 threading.Lock。"
        )


class TestCrossLoopReuse:
    """真实争抢 + 跨循环复用的端到端回归。"""

    def test_module_lock_survives_real_contention_across_two_loops(self) -> None:
        """在两个独立事件循环里各制造一次真实的多线程争抢，都必须正常完成。

        为什么用 to_thread 而不是两个协程：threading.Lock 在协程间不会产生争抢
        （临界区无 await，先进入者一口气跑完），只有真实多线程才会实际阻塞等待，
        从而走到「获取已被持有的锁」这条路径。

        为什么跑两次 asyncio.run：这是复现条件的核心。第一次调用建立循环绑定
        （若锁是 asyncio.Lock），第二次换了新循环才会抛 RuntimeError。只跑一次
        测不出任何东西。

        承重性（2026-08-09 实测）：把 _store_cache_lock 改回 asyncio.Lock，本条
        立刻红——同步 `with` 在 asyncio.Lock 上抛
        `TypeError: 'Lock' object does not support the context manager protocol`。
        即便有人把它包装成兼容同步 with 的形式，第二次 asyncio.run 也会在真实
        争抢时抛 'bound to a different event loop'。
        """
        hold_order: list[str] = []

        def _sync_critical_section(tag: str) -> None:
            with _store_cache_lock:
                hold_order.append(tag)
                # 真的占住一会儿，确保另一个线程撞上已被持有的锁
                time.sleep(0.02)

        async def _contend(loop_tag: str) -> None:
            await asyncio.gather(
                asyncio.to_thread(_sync_critical_section, f"{loop_tag}-a"),
                asyncio.to_thread(_sync_critical_section, f"{loop_tag}-b"),
            )

        # 两个互相独立的事件循环，中间没有任何共享状态重置
        asyncio.run(_contend("loop1"))
        asyncio.run(_contend("loop2"))

        assert sorted(hold_order) == [
            "loop1-a",
            "loop1-b",
            "loop2-a",
            "loop2-b",
        ], f"四次临界区应全部执行完成，实际 {hold_order}"

    def test_background_tasks_lock_survives_loop_switch(self) -> None:
        """同上，覆盖 task_tool 的锁——它是两处里唯一原本就在生产路径上被用的。"""
        counter = {"n": 0}

        def _bump() -> None:
            with _background_tasks_lock:
                counter["n"] += 1
                time.sleep(0.02)

        async def _contend() -> None:
            await asyncio.gather(asyncio.to_thread(_bump), asyncio.to_thread(_bump))

        asyncio.run(_contend())
        asyncio.run(_contend())

        assert counter["n"] == 4, f"四次自增应全部生效，实际 {counter['n']}"
