"""Tests for HERMES_HOME os.environ race condition fix.

Concurrent workers in the same asyncio event loop used to corrupt each
other's HERMES_HOME via the global os.environ dict.  The fix stores the
per-task value in a contextvars.ContextVar; these tests verify both the
basic round-trip and the concurrent interleaving case.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.claw import ClawService
from app.vendors.hermes_agent.hermes_constants import get_hermes_home

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> ClawService:
    """Build a bare ClawService; callers add their own method patches."""
    return ClawService(workspace_root=tmp_path)


def _patch_scope(
    service: ClawService,
    tmp_path: Path,
    user_home: Path | None = None,
) -> tuple:
    """Return (fake_root, patcher_enter, patcher_home) for a scope call."""
    if user_home is None:
        user_home = tmp_path / "hermes_home"
    fake_root = tmp_path / "fake_hermes_runtime"
    fake_root.mkdir(parents=True, exist_ok=True)
    p_enter = patch.object(service, "_get_hermes_runtime_root", return_value=fake_root)
    p_home = patch.object(service, "_get_user_hermes_home", return_value=user_home)
    return fake_root, p_enter, p_home


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def service(tmp_path: Path):
    return _make_service(tmp_path)


# ---------------------------------------------------------------------------
# Basic (non-concurrent) round-trip
# ---------------------------------------------------------------------------


def test_sets_hermes_home_inside_scope(service: ClawService, tmp_path: Path) -> None:
    fake_home = Path("/tmp/fake_hermes")
    _, p_enter, p_home = _patch_scope(service, tmp_path, user_home=fake_home)
    with p_enter, p_home:
        with service._hermes_import_scope("user_1"):
            assert os.environ.get("HERMES_HOME") == str(fake_home)


def test_restores_hermes_home_after_scope(service: ClawService, tmp_path: Path) -> None:
    fake_home = Path("/tmp/fake_hermes")
    _, p_enter, p_home = _patch_scope(service, tmp_path, user_home=fake_home)
    os.environ["HERMES_HOME"] = "original_value"
    try:
        with p_enter, p_home:
            with service._hermes_import_scope("user_1"):
                pass  # just enter and exit
        # Scope exit always pops HERMES_HOME (safer than restoring stale
        # values that a concurrent task may have clobbered).
        assert "HERMES_HOME" not in os.environ
    finally:
        os.environ.pop("HERMES_HOME", None)


def test_restores_hermes_home_pop_on_exit(service: ClawService, tmp_path: Path) -> None:
    """Scope exit always removes HERMES_HOME from os.environ."""
    fake_home = Path("/tmp/fake_hermes")
    _, p_enter, p_home = _patch_scope(service, tmp_path, user_home=fake_home)
    os.environ["HERMES_HOME"] = "previous_value"
    try:
        with p_enter, p_home:
            with service._hermes_import_scope("user_1"):
                assert os.environ.get("HERMES_HOME") == str(fake_home)
        # After exit, HERMES_HOME is removed (not restored to the possibly-
        # stale "previous_value", which avoids clobbering a concurrent task).
        assert "HERMES_HOME" not in os.environ
    finally:
        os.environ.pop("HERMES_HOME", None)


# ---------------------------------------------------------------------------
# Concurrent race-condition tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_corrupt_each_other(
    service: ClawService, tmp_path: Path
) -> None:
    """Two concurrent tasks must not corrupt each other's HERMES_HOME.

    Run via asyncio.gather: both tasks enter/exits their scope concurrently.
    Regardless of scheduling order, both scopes pop HERMES_HOME from
    os.environ on exit — the final state must always be clean.
    """
    fake_home_a = tmp_path / "home_a"
    fake_home_b = tmp_path / "home_b"
    fake_home_a.mkdir()
    fake_home_b.mkdir()
    fake_runtime = tmp_path / "fake_runtime"
    fake_runtime.mkdir()

    p_enter = patch.object(service, "_get_hermes_runtime_root", return_value=fake_runtime)
    p_enter.start()
    try:

        async def task_a() -> None:
            with patch.object(service, "_get_user_hermes_home", return_value=fake_home_a):
                with service._hermes_import_scope("user_a"):
                    # Yield so task_b can run concurrently
                    await asyncio.sleep(0)

        async def task_b() -> None:
            with patch.object(service, "_get_user_hermes_home", return_value=fake_home_b):
                with service._hermes_import_scope("user_b"):
                    # Yield so task_a can run concurrently
                    await asyncio.sleep(0)

        await asyncio.wait_for(asyncio.gather(task_a(), task_b()), timeout=10)
    finally:
        p_enter.stop()

    # Both scopes have exited; HERMES_HOME must not be in os.environ.
    assert "HERMES_HOME" not in os.environ, (
        f"HERMES_HOME leaked into os.environ: {os.environ.get('HERMES_HOME')!r}"
    )


@pytest.mark.asyncio
async def test_concurrent_scopes_contextvar_values_are_correct(
    service: ClawService, tmp_path: Path
) -> None:
    """Each task's vendored code reads its own ContextVar via get_hermes_home().

    Uses asyncio.Lock to force deterministic interleaving:
      task_a acquires lock → enters scope → HERMES_HOME=home_a → yields
      task_b blocks on lock (cannot enter until task_a releases)
      task_a resumes → reads get_hermes_home() → gets home_a via ContextVar
      task_a exits scope → releases lock
      task_b acquires lock → enters scope → HERMES_HOME=home_b
      task_b reads get_hermes_home() → gets home_b via ContextVar

    This proves that even when both scopes run, each task's vendored code
    sees its own HERMES_HOME via the ContextVar (not os.environ).
    """
    fake_home_a = tmp_path / "home_a"
    fake_home_b = tmp_path / "home_b"
    fake_home_a.mkdir()
    fake_home_b.mkdir()
    fake_runtime = tmp_path / "fake_runtime"
    fake_runtime.mkdir()

    # Use a lock to ensure task_a starts and suspends before task_b enters.
    # The key yield happens INSIDE the locked section (task_a holds the lock
    # while suspended at await, so task_b can't start its scope until after
    # task_a has read get_hermes_home()).
    scope_lock = asyncio.Lock()

    p_enter = patch.object(service, "_get_hermes_runtime_root", return_value=fake_runtime)
    p_enter.start()
    try:

        async def task_a() -> str:
            async with scope_lock:
                with patch.object(service, "_get_user_hermes_home", return_value=fake_home_a):
                    with service._hermes_import_scope("user_a"):
                        # Yield while still holding lock — task_b will
                        # block on scope_lock until task_a finishes here.
                        await asyncio.sleep(0)
                        return str(get_hermes_home())

        async def task_b() -> str:
            async with scope_lock:
                with patch.object(service, "_get_user_hermes_home", return_value=fake_home_b):
                    with service._hermes_import_scope("user_b"):
                        return str(get_hermes_home())

        result_a, result_b = await asyncio.gather(task_a(), task_b())
    finally:
        p_enter.stop()

    # Each task's vendored code must see its own HERMES_HOME via ContextVar
    assert result_a == str(fake_home_a), (
        f"task_a get_hermes_home()={result_a!r}, expected {str(fake_home_a)!r}"
    )
    assert result_b == str(fake_home_b), (
        f"task_b get_hermes_home()={result_b!r}, expected {str(fake_home_b)!r}"
    )
    assert "HERMES_HOME" not in os.environ


@pytest.mark.asyncio
async def test_contextvar_inherited_by_child_tasks(service: ClawService, tmp_path: Path) -> None:
    """A child task created inside the scope inherits the parent's value.

    asyncio.create_task copies the parent context, so the ContextVar value
    is automatically propagated to the child.
    """
    fake_home = tmp_path / "child_home"
    fake_home.mkdir()
    fake_runtime = tmp_path / "fake_runtime"
    fake_runtime.mkdir()

    child_result: str | None = None

    async def child() -> None:
        nonlocal child_result
        child_result = str(get_hermes_home())

    async def parent() -> None:
        with (
            patch.object(service, "_get_user_hermes_home", return_value=fake_home),
            patch.object(service, "_get_hermes_runtime_root", return_value=fake_runtime),
        ):
            with service._hermes_import_scope("user_x"):
                task = asyncio.create_task(child())
                await task

    await parent()

    assert child_result == str(fake_home), (
        f"child task inherited HERMES_HOME={child_result!r}, expected {str(fake_home)!r}"
    )
    assert "HERMES_HOME" not in os.environ
