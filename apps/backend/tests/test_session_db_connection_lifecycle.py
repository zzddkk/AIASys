"""SessionDB 的连接生命周期回归。

背景（2026-08-11 现场）：删除工作区在 Windows 上返回 500，根因不是文件系统抖动，
而是 SessionDB 自己把连接泄漏了——6 处都写成 `with self._connect() as connection`，
而 sqlite3 的连接上下文管理器**只提交事务、不关闭连接**。于是每写一条消息就留下一个
未关闭的连接，WAL 模式还会额外持有 -wal / -shm 句柄。

在 Linux 上这只是个不可见的 fd 泄漏；在 Windows 上，目录内有未释放句柄时 os.rename
直接报 WinError 32，会话目录 detach 进 .trash 失败，删除接口 500，残留工作区无限堆积。

这个测试要跨平台可跑，所以不去制造 Windows 文件锁，而是直接断言「连接被关掉了」：
已关闭的 sqlite3 连接再执行语句会抛 ProgrammingError，这一点在三端一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.memory.session_db import SessionDB


@pytest.fixture
def connection_spy(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    """记录所有真实建立过的 sqlite 连接。"""
    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        created.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    return created


def _assert_all_closed(connections: list[sqlite3.Connection]) -> None:
    assert connections, "断言前提不成立：本次根本没有建立过连接，测试无效"
    for index, connection in enumerate(connections):
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        # 说明：若这里没抛，说明连接仍然可用，即句柄未释放。
        del index


class TestSessionDbConnectionLifecycle:
    def test_建库后连接已关闭(
        self, tmp_path: Path, connection_spy: list[sqlite3.Connection]
    ) -> None:
        SessionDB(tmp_path / "sessions.db")
        _assert_all_closed(connection_spy)

    def test_写消息后连接已关闭(
        self, tmp_path: Path, connection_spy: list[sqlite3.Connection]
    ) -> None:
        db = SessionDB(tmp_path / "sessions.db")
        db.add_message(session_id="s1", user_id="u1", role="user", content="你好")
        _assert_all_closed(connection_spy)

    def test_读写各路径都不泄漏连接(
        self, tmp_path: Path, connection_spy: list[sqlite3.Connection]
    ) -> None:
        db = SessionDB(tmp_path / "sessions.db")
        db.add_message(session_id="s1", user_id="u1", role="user", content="alpha beta")
        db.add_message(session_id="s1", user_id="u1", role="assistant", content="gamma")
        assert db.get_messages("s1")
        db.search_sessions("alpha")
        db.get_messages_after_id(0, session_id="s1")
        db.delete_session("s1")
        # 覆盖全部 6 个入口后，没有任何连接还活着。
        _assert_all_closed(connection_spy)

    def test_异常路径也关闭连接(
        self, tmp_path: Path, connection_spy: list[sqlite3.Connection]
    ) -> None:
        db = SessionDB(tmp_path / "sessions.db")
        # 制造一次执行期异常：FTS5 查询语法错误会在 with 块内抛出。
        with pytest.raises(sqlite3.Error):
            db.search_sessions('"')
        _assert_all_closed(connection_spy)

    def test_数据在关闭后仍可被新连接读到(self, tmp_path: Path) -> None:
        """关连接不能把未提交的数据一起丢掉——顺手锁住提交语义。"""
        path = tmp_path / "sessions.db"
        SessionDB(path).add_message(session_id="s1", user_id="u1", role="user", content="持久化")
        messages = SessionDB(path).get_messages("s1")
        assert [m["content"] for m in messages] == ["持久化"]
