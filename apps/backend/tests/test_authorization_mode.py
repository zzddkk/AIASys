"""会话授权模式（authorization_mode）的持久化与端点测试。

设计文档：AIASys-product-design/交互设计/permission-mode-management.md
覆盖：SessionManager.update_authorization_mode 持久化、非法值 422、
会话不存在 404、正常切换后详情下发新值。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.session import SessionManager


def test_update_authorization_mode_persists(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    user_id = "local_default"
    session_id = "authz-mode-session"

    manager.create_session(
        session_id=session_id,
        user_id=user_id,
        title="授权模式测试",
        sandbox_mode="local",
    )

    updated = manager.update_authorization_mode(
        session_id=session_id,
        user_id=user_id,
        authorization_mode="manual",
    )

    assert updated is not None
    assert updated.authorization_mode == "manual"

    # 重新读取，确认落盘而非只改内存
    metadata = manager.get_session(session_id, user_id)
    assert metadata is not None
    assert metadata.authorization_mode == "manual"


def test_update_authorization_mode_missing_session_returns_none(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    result = manager.update_authorization_mode(
        session_id="nonexistent",
        user_id="local_default",
        authorization_mode="smart",
    )
    assert result is None


def test_authorization_mode_request_rejects_invalid_value() -> None:
    from pydantic import ValidationError

    from app.api.routes.sessions_models import UpdateAuthorizationModeRequest

    try:
        UpdateAuthorizationModeRequest(authorization_mode="yolo")
        raise AssertionError("非法档位应该被 Literal 校验拒绝")
    except ValidationError:
        pass

    # 合法四档全部可构造
    for mode in ("manual", "smart", "auto", "full_auto"):
        req = UpdateAuthorizationModeRequest(authorization_mode=mode)
        assert req.authorization_mode == mode


def test_authorization_mode_endpoint_roundtrip(tmp_path: Path) -> None:
    """端点级测试：POST 切换 → 会话详情可读到新值。"""
    from app.api.routes.sessions_branches import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    user_id = "local_default"
    session_id = "authz-endpoint-session"

    from app.api.routes import sessions_branches

    manager = SessionManager(tmp_path)
    manager.create_session(
        session_id=session_id,
        user_id=user_id,
        title="授权模式端点测试",
        sandbox_mode="local",
    )

    original_manager = sessions_branches.session_manager
    sessions_branches.session_manager = manager
    try:
        # 正常切换
        resp = client.post(
            f"/sessions/{user_id}/{session_id}/authorization-mode",
            json={"authorization_mode": "smart"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["authorization_mode"] == "smart"
        assert body["authorization_mode_effect"] == "next_run_only"

        # 非法值 422（Literal 校验）
        resp = client.post(
            f"/sessions/{user_id}/{session_id}/authorization-mode",
            json={"authorization_mode": "yolo"},
        )
        assert resp.status_code == 422

        # 会话不存在 404
        resp = client.post(
            f"/sessions/{user_id}/nonexistent-session/authorization-mode",
            json={"authorization_mode": "smart"},
        )
        assert resp.status_code == 404
    finally:
        sessions_branches.session_manager = original_manager
