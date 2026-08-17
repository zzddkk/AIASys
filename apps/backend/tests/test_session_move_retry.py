"""move_path_with_retry / rmtree_with_retry 的行为锁定。

背景：删除工作区在 Windows 上会 500，根因是会话目录 detach 进 .trash 时
os.rename 撞上未释放的文件句柄（WinError 5）。Linux 允许重命名带打开句柄的目录，
所以这个缺陷只在 Windows 现场出现，CI 的 Linux runner 天然测不到。

因此这里不去真的制造一把 Windows 文件锁（那样测试本身就不跨平台了），
而是用注入的假 shutil 精确锁定重试语义：
  1. 瞬时占用错误会重试，并在句柄释放后成功；
  2. 非瞬时错误（如 ENOENT）立即抛出，不做无意义等待；
  3. 重试耗尽后抛出的是最后一次的原始异常，不被包装吞掉。
"""

from __future__ import annotations

import errno

import pytest

from app.services.session import core as session_core


def _win_permission_error(winerror: int = 5) -> PermissionError:
    """构造带 winerror 的 PermissionError，模拟 Windows 句柄占用。"""
    exc = PermissionError(13, "拒绝访问。")
    # winerror 在非 Windows 上不是内置属性，显式挂上以覆盖判定分支。
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


class TestIsTransientLockError:
    @pytest.mark.parametrize("winerror", [5, 32, 33, 145])
    def test_windows_占用类错误判为可重试(self, winerror: int) -> None:
        assert session_core._is_transient_lock_error(_win_permission_error(winerror)) is True

    def test_其他_winerror_不重试(self) -> None:
        # 例如 WinError 2（找不到文件），重试没有意义。
        assert session_core._is_transient_lock_error(_win_permission_error(2)) is False

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EBUSY])
    def test_posix_eacces_ebusy_判为可重试(self, code: int) -> None:
        assert session_core._is_transient_lock_error(OSError(code, "busy")) is True

    def test_posix_enoent_不重试(self) -> None:
        assert session_core._is_transient_lock_error(OSError(errno.ENOENT, "missing")) is False


class TestMovePathWithRetry:
    def test_首次即成功不睡眠(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []
        sleeps: list[float] = []
        monkeypatch.setattr(session_core.shutil, "move", lambda s, d: calls.append((s, d)))
        monkeypatch.setattr(session_core.time, "sleep", lambda s: sleeps.append(s))

        session_core.move_path_with_retry("src", "dst")

        assert calls == [("src", "dst")]
        assert sleeps == []

    def test_占用后重试并最终成功(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def fake_move(src: str, dst: str) -> None:
            attempts["n"] += 1
            # 前两次撞锁，第三次句柄已释放。
            if attempts["n"] < 3:
                raise _win_permission_error(5)

        monkeypatch.setattr(session_core.shutil, "move", fake_move)
        monkeypatch.setattr(session_core.time, "sleep", lambda s: sleeps.append(s))

        session_core.move_path_with_retry("src", "dst")

        assert attempts["n"] == 3
        # 退避递增，而不是等长轮询。
        assert sleeps == [0.1, 0.2]

    def test_非瞬时错误立即抛出且不重试(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def fake_move(src: str, dst: str) -> None:
            attempts["n"] += 1
            raise OSError(errno.ENOENT, "no such file")

        monkeypatch.setattr(session_core.shutil, "move", fake_move)
        monkeypatch.setattr(session_core.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(OSError) as excinfo:
            session_core.move_path_with_retry("src", "dst")

        assert excinfo.value.errno == errno.ENOENT
        assert attempts["n"] == 1, "非瞬时错误不应重试"
        assert sleeps == []

    def test_重试耗尽后抛出最后一次原始异常(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def fake_move(src: str, dst: str) -> None:
            attempts["n"] += 1
            raise _win_permission_error(32)

        monkeypatch.setattr(session_core.shutil, "move", fake_move)
        monkeypatch.setattr(session_core.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(PermissionError) as excinfo:
            session_core.move_path_with_retry("src", "dst", attempts=4, base_delay=0.01)

        assert getattr(excinfo.value, "winerror", None) == 32
        assert attempts["n"] == 4, "应当用满给定的重试次数"
        # 最后一次失败后不再睡眠，避免白等一轮退避。
        assert len(sleeps) == 3


class TestRmtreeWithRetry:
    def test_占用后重试并最终成功(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}
        monkeypatch.setattr(session_core.time, "sleep", lambda s: None)

        def fake_rmtree(target: str) -> None:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise _win_permission_error(5)

        monkeypatch.setattr(session_core.shutil, "rmtree", fake_rmtree)
        session_core.rmtree_with_retry("target")
        assert attempts["n"] == 2

    def test_目录已不存在视为成功(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_rmtree(target: str) -> None:
            raise FileNotFoundError(errno.ENOENT, "missing")

        monkeypatch.setattr(session_core.shutil, "rmtree", fake_rmtree)
        # 幂等：并发删除或已被清走时不应报错。
        session_core.rmtree_with_retry("target")

    def test_非瞬时错误立即抛出(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_rmtree(target: str) -> None:
            raise _win_permission_error(2)

        monkeypatch.setattr(session_core.shutil, "rmtree", fake_rmtree)
        with pytest.raises(PermissionError):
            session_core.rmtree_with_retry("target")
