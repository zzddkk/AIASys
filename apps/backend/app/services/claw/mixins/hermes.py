"""Claw vendored Hermes runtime import mixin."""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
from pathlib import Path
from typing import Iterator

from app.core.config import BASE_DIR

# Per-task HERMES_HOME override.  asyncio.create_task() copies the parent
# context, so child tasks automatically inherit the value set by the caller.
# This eliminates the global os.environ race when multiple workers run
# _hermes_import_scope concurrently in the same event loop.
_HERMES_HOME: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "HERMES_HOME", default=None
)


class ClawHermesMixin:
    def _get_repo_root(self) -> Path:
        return BASE_DIR.parent.parent

    def _get_hermes_runtime_root(self) -> Path:
        runtime_root = (
            self._get_repo_root() / "apps" / "backend" / "app" / "vendors" / "hermes_agent"
        )
        if not runtime_root.exists():
            raise RuntimeError(f"未找到 Claw 内置通信运行时目录: {runtime_root}")
        return runtime_root

    @contextlib.contextmanager
    def _hermes_import_scope(self, user_id: str) -> Iterator[None]:
        runtime_root = self._get_hermes_runtime_root()
        previous_path = list(sys.path)
        hermes_home = self._get_user_hermes_home(user_id)
        # os.environ is kept as a safety-net for subprocesses spawned inside
        # the scope; the per-task value lives in _HERMES_HOME ContextVar so
        # concurrent asyncio tasks cannot trample each other.
        token = _HERMES_HOME.set(str(hermes_home))
        os.environ["HERMES_HOME"] = str(hermes_home)
        if str(runtime_root) not in sys.path:
            sys.path.insert(0, str(runtime_root))
        try:
            yield
        finally:
            _HERMES_HOME.reset(token)
            os.environ.pop("HERMES_HOME", None)
            sys.path[:] = previous_path
