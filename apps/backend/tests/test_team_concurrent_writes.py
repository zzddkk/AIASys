"""team 多 worker 并发写入的端到端测试。

与 test_team_write_guard.py 的差异：那里验证「单个守卫执行」的放行/拦截，
这里验证「两个守卫实例真实并发」时的行为——

1. 两个 worker 各自带 write_allow_root，asyncio.gather 并发执行写：
   各自范围内的写全部成功，文件内容完整（无交错污染）。
2. 并发中互相尝试越界写对方范围：全部被硬拒绝，目标文件从不存在。
3. 守卫状态不存在跨并发执行串扰：item_ctx 里的 write_allow_root 是
   每次调用独立传入的，并发交替不能让 A 的范围泄漏给 B。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.tool_result import ToolResult
from tests.test_team_write_guard import _make_async_gen, _make_mixin


def _write_call(call_id: str, path: Path, content: str, allow_root: list[str]) -> dict[str, Any]:
    return {
        "item": {
            "id": call_id,
            "type": "function",
            "function": {"name": "WriteFile"},
            "arguments": {"path": str(path), "content": content},
        },
        "item_ctx": {"write_allow_root": allow_root},
        "side_effect": True,
    }


async def _run_write(mixin: Any, call: dict[str, Any]) -> list[Any]:
    events = []
    async for e in mixin._execute_write_tool(call):
        events.append(e)
    return events


def _result_events(events: list[Any]) -> list[Any]:
    return [e for e in events if e.kind == "tool_result"]


class TestConcurrentGuardedWrites:
    @pytest.mark.asyncio
    async def test_two_workers_concurrent_writes_stay_in_own_scope(self, tmp_path: Path):
        """两个 worker 并发写各自范围：全部成功且内容互不污染。"""
        scope_a = tmp_path / "worker_a"
        scope_b = tmp_path / "worker_b"
        scope_a.mkdir()
        scope_b.mkdir()

        mixin_a = _make_mixin(write_allow_root=[str(scope_a)])
        mixin_b = _make_mixin(write_allow_root=[str(scope_b)])

        from unittest.mock import MagicMock

        # 两个 worker 的底层工具都真实写盘（不是 mock 吞掉），才能验证内容完整性
        async def _real_write(path: str, content: str) -> None:
            Path(path).write_text(content, encoding="utf-8")

        def _make_registry(tag: str) -> MagicMock:
            registry = MagicMock()

            async def _invoke(*args: Any, **kwargs: Any):
                # invoke_stream 的调用签名里带 arguments；从最近的 item 取不到，
                # 这里按调用顺序从闭包队列取（每次调用前先压入）
                path, content = _pending_writes[tag].pop(0)
                await _real_write(path, content)
                yield ToolResult(content="written", is_error=False)

            registry.invoke_stream = MagicMock(side_effect=lambda *a, **kw: _invoke(*a, **kw))
            return registry

        _pending_writes: dict[str, list[tuple[str, str]]] = {"a": [], "b": []}

        async def worker(tag: str, mixin: Any, scope: Path, count: int = 10) -> None:
            registry = _make_registry(tag)
            mixin._tool_registry = registry
            mixin._finish_tool_execution = MagicMock(side_effect=lambda *a, **kw: _make_async_gen())
            for i in range(count):
                target = scope / f"{tag}_file_{i}.txt"
                _pending_writes[tag].append((str(target), f"{tag}-content-{i}"))
                events = await _run_write(
                    mixin,
                    _write_call(f"{tag}_{i}", target, f"{tag}-content-{i}", [str(scope)]),
                )
                results = _result_events(events)
                assert not results or not results[0].is_error, f"{tag} 范围内写被拒: {events}"

        await asyncio.gather(worker("a", mixin_a, scope_a), worker("b", mixin_b, scope_b))

        for tag, scope in (("a", scope_a), ("b", scope_b)):
            written = sorted(scope.glob(f"{tag}_file_*.txt"))
            assert len(written) == 10, f"{tag} 应有 10 个文件，实际 {len(written)}"
            for f in written:
                content = f.read_text(encoding="utf-8")
                assert content.startswith(f"{tag}-content-"), f"{f} 内容被污染: {content}"

    @pytest.mark.asyncio
    async def test_concurrent_cross_scope_writes_all_blocked(self, tmp_path: Path):
        """并发中互相越界写：全部硬拒绝，目标文件从不存在。"""
        scope_a = tmp_path / "worker_a"
        scope_b = tmp_path / "worker_b"
        scope_a.mkdir()
        scope_b.mkdir()

        mixin_a = _make_mixin(write_allow_root=[str(scope_a)])
        mixin_b = _make_mixin(write_allow_root=[str(scope_b)])

        async def attack(attacker: Any, attacker_tag: str, victim_scope: Path, count: int = 10):
            blocked = 0
            for i in range(count):
                target = victim_scope / f"hacked_by_{attacker_tag}_{i}.txt"
                events = await _run_write(
                    attacker,
                    _write_call(
                        f"{attacker_tag}_hack_{i}",
                        target,
                        "pwned",
                        [] if False else [str(scope_a if attacker_tag == "a" else scope_b)],
                    ),
                )
                results = _result_events(events)
                assert results, f"越界写没有返回 tool_result: {events}"
                assert results[0].is_error is True
                assert "硬拒绝" in (results[0].content or "")
                blocked += 1
            return blocked

        blocked_a, blocked_b = await asyncio.gather(
            attack(mixin_a, "a", scope_b),
            attack(mixin_b, "b", scope_a),
        )

        assert blocked_a == 10 and blocked_b == 10
        # 底层工具从未被调用（守卫拦截在调用前）
        mixin_a._tool_registry.invoke_stream.assert_not_called()
        mixin_b._tool_registry.invoke_stream.assert_not_called()
        # 目标文件不存在
        assert list(scope_a.glob("hacked_by_*.txt")) == []
        assert list(scope_b.glob("hacked_by_*.txt")) == []

    @pytest.mark.asyncio
    async def test_interleaved_scope_and_cross_scope_no_leak(self, tmp_path: Path):
        """范围内/越界写交错并发：守卫状态不串扰，范围内仍放行、越界仍拒绝。"""
        scope_a = tmp_path / "worker_a"
        scope_b = tmp_path / "worker_b"
        scope_a.mkdir()
        scope_b.mkdir()

        mixin_a = _make_mixin(write_allow_root=[str(scope_a)])

        own_target = scope_a / "own.txt"
        foreign_target = scope_b / "foreign.txt"

        own_call = _write_call("own", own_target, "mine", [str(scope_a)])
        cross_call = _write_call("cross", foreign_target, "pwned", [str(scope_a)])

        own_events, cross_events = await asyncio.gather(
            _run_write(mixin_a, own_call),
            _run_write(mixin_a, cross_call),
        )

        # 范围内的那次进入了底层工具调用路径
        own_results = _result_events(own_events)
        assert not own_results or not own_results[0].is_error
        # 越界的那次被硬拒绝
        cross_results = _result_events(cross_events)
        assert cross_results[0].is_error is True
        assert not foreign_target.exists()
