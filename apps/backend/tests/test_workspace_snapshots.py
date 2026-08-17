"""工作区快照服务与路由测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.routes import workspaces_resources_snapshots as snapshots_route
from app.core import config as config_module
from app.models.user import UserInfo
from app.services import workspace_registry as workspace_registry_module
from app.services.session import SessionManager
from app.services.workspace_registry import WorkspaceRegistryService
from app.services.workspace_snapshots import workspace_snapshot_service


def _build_user() -> UserInfo:
    return UserInfo(user_id="local_default", role="admin", auth_provider="local")


def _patch_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path, service: WorkspaceRegistryService
) -> None:
    monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        workspace_registry_module,
        "get_workspace_registry_service",
        lambda: service,
    )
    monkeypatch.setattr(
        snapshots_route,
        "get_workspace_registry_service",
        lambda: service,
    )


@pytest.mark.asyncio
async def test_create_and_list_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceRegistryService(
        tmp_path,
        session_manager=SessionManager(tmp_path),
    )
    service.create_workspace(
        user_id="local_default",
        workspace_id="snapshot-workspace",
        title="快照测试工作区",
    )
    _patch_roots(monkeypatch, tmp_path, service)
    user = _build_user()

    workspace_root = service.get_workspace_root("local_default", "snapshot-workspace")
    (workspace_root / "notes.md").write_text("v1\n", encoding="utf-8")

    created = await snapshots_route.create_workspace_snapshot(
        "snapshot-workspace",
        snapshots_route.CreateSnapshotRequest(title="初始版本"),
        current_user=user,
    )
    assert created.title == "初始版本"
    assert created.file_count == 1
    assert created.source == "manual"

    listed = await snapshots_route.list_workspace_snapshots(
        "snapshot-workspace",
        current_user=user,
    )
    assert listed.total == 1
    assert len(listed.snapshots) == 1
    assert listed.snapshots[0].id == created.id


@pytest.mark.asyncio
async def test_apply_snapshot_restores_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceRegistryService(
        tmp_path,
        session_manager=SessionManager(tmp_path),
    )
    service.create_workspace(
        user_id="local_default",
        workspace_id="apply-workspace",
        title="应用快照测试",
    )
    _patch_roots(monkeypatch, tmp_path, service)
    user = _build_user()

    workspace_root = service.get_workspace_root("local_default", "apply-workspace")
    notes = workspace_root / "notes.md"
    notes.write_text("old\n", encoding="utf-8")

    snapshot = await snapshots_route.create_workspace_snapshot(
        "apply-workspace",
        snapshots_route.CreateSnapshotRequest(title="保存旧版本"),
        current_user=user,
    )

    notes.write_text("new\n", encoding="utf-8")
    assert notes.read_text(encoding="utf-8") == "new\n"

    result = await snapshots_route.apply_workspace_snapshot(
        "apply-workspace",
        snapshot.id,
        snapshots_route.ApplySnapshotRequest(mode="soft"),
        current_user=user,
    )
    assert result.success is True
    assert "notes.md" in result.restored_files
    assert result.deleted_files == []
    assert notes.read_text(encoding="utf-8") == "old\n"

    # 应用后应该产生自动备份快照
    listed = await snapshots_route.list_workspace_snapshots(
        "apply-workspace",
        source="auto_switch_backup",
        current_user=user,
    )
    assert len(listed.snapshots) == 1


@pytest.mark.asyncio
async def test_hard_reset_deletes_new_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceRegistryService(
        tmp_path,
        session_manager=SessionManager(tmp_path),
    )
    service.create_workspace(
        user_id="local_default",
        workspace_id="reset-workspace",
        title="硬重置测试",
    )
    _patch_roots(monkeypatch, tmp_path, service)
    user = _build_user()

    workspace_root = service.get_workspace_root("local_default", "reset-workspace")
    (workspace_root / "keep.md").write_text("keep\n", encoding="utf-8")

    snapshot = await snapshots_route.create_workspace_snapshot(
        "reset-workspace",
        snapshots_route.CreateSnapshotRequest(title="只有 keep.md"),
        current_user=user,
    )

    (workspace_root / "extra.md").write_text("extra\n", encoding="utf-8")

    result = await snapshots_route.apply_workspace_snapshot(
        "reset-workspace",
        snapshot.id,
        snapshots_route.ApplySnapshotRequest(mode="hard"),
        current_user=user,
    )
    assert result.success is True
    assert "extra.md" in result.deleted_files
    assert not (workspace_root / "extra.md").exists()
    assert (workspace_root / "keep.md").exists()


@pytest.mark.asyncio
async def test_delete_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceRegistryService(
        tmp_path,
        session_manager=SessionManager(tmp_path),
    )
    service.create_workspace(
        user_id="local_default",
        workspace_id="delete-snapshot-workspace",
        title="删除快照测试",
    )
    _patch_roots(monkeypatch, tmp_path, service)
    user = _build_user()

    created = await snapshots_route.create_workspace_snapshot(
        "delete-snapshot-workspace",
        snapshots_route.CreateSnapshotRequest(title="待删除"),
        current_user=user,
    )

    deleted = await snapshots_route.delete_workspace_snapshot(
        "delete-snapshot-workspace",
        created.id,
        current_user=user,
    )
    assert deleted["success"] is True

    listed = await snapshots_route.list_workspace_snapshots(
        "delete-snapshot-workspace",
        current_user=user,
    )
    assert listed.total == 0


def test_snapshot_service_captures_current_state(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "a.md").write_text("a\n", encoding="utf-8")
    (workspace_root / "b.md").write_text("b\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="test",
        created_by="user",
    )
    assert snapshot.title == "test"
    assert set(snapshot.files.keys()) == {"a.md", "b.md"}
    assert all(entry_id is not None for entry_id in snapshot.files.values())


def test_snapshot_service_soft_apply_preserves_new_files(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "a.md").write_text("old\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="old-state",
        created_by="user",
    )

    (workspace_root / "a.md").write_text("new\n", encoding="utf-8")
    (workspace_root / "b.md").write_text("extra\n", encoding="utf-8")

    result = workspace_snapshot_service.apply_snapshot(
        workspace_root,
        "test-workspace",
        snapshot.id,
        mode="soft",
        actor="user",
    )
    assert "a.md" in result.restored_files
    assert "b.md" not in result.deleted_files
    assert (workspace_root / "a.md").read_text(encoding="utf-8") == "old\n"
    assert (workspace_root / "b.md").exists()


def test_snapshot_service_hard_reset_deletes_directory(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "keep.md").write_text("keep\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="only-keep",
        created_by="user",
    )

    extra_dir = workspace_root / "extra"
    extra_dir.mkdir()
    (extra_dir / "file.md").write_text("inside\n", encoding="utf-8")

    result = workspace_snapshot_service.apply_snapshot(
        workspace_root,
        "test-workspace",
        snapshot.id,
        mode="hard",
        actor="user",
    )
    assert "extra/file.md" in result.deleted_files
    assert not extra_dir.exists()
    assert (workspace_root / "keep.md").exists()


def test_snapshot_creates_history_entry_for_untracked_file(tmp_path) -> None:
    """如果文件没有历史记录，创建快照时应自动补一条 before_snapshot 历史。"""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "notes.md").write_text("only\n", encoding="utf-8")

    from app.services.file_history import file_history_service

    assert file_history_service.list_entries(workspace_root, "notes.md") == []

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="first",
        created_by="user",
    )
    assert snapshot.files["notes.md"] is not None
    entries = file_history_service.list_entries(workspace_root, "notes.md")
    assert len(entries) == 1
    assert entries[0].operation == "before_snapshot"


def test_snapshot_excludes_internal_directories(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "notes.md").write_text("ok\n", encoding="utf-8")
    (workspace_root / ".aiasys").mkdir()
    (workspace_root / ".aiasys" / "secret.yaml").write_text("secret\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="exclude-internal",
        created_by="user",
    )
    assert ".aiasys/secret.yaml" not in snapshot.files
    assert "notes.md" in snapshot.files


def test_snapshot_diff_detects_changes(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "a.md").write_text("old\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="base",
        created_by="user",
    )

    (workspace_root / "a.md").write_text("new\n", encoding="utf-8")
    (workspace_root / "b.md").write_text("extra\n", encoding="utf-8")

    changes = workspace_snapshot_service.diff_snapshot(workspace_root, snapshot.id)
    paths = {change[0] for change in changes}
    assert paths == {"a.md", "b.md"}


def test_snapshot_apply_with_missing_history_entry_is_skipped(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    (workspace_root / "notes.md").write_text("old\n", encoding="utf-8")

    snapshot = workspace_snapshot_service.create_snapshot(
        workspace_root,
        "test-workspace",
        title="base",
        created_by="user",
    )

    # 删除底层历史文件，让 restore_entry 找不到内容
    history_root = workspace_root / ".aiasys" / "file-history"
    entries_dir = history_root / "entries"
    for entry_file in entries_dir.iterdir():
        entry_file.unlink()

    result = workspace_snapshot_service.apply_snapshot(
        workspace_root,
        "test-workspace",
        snapshot.id,
        mode="soft",
        actor="user",
    )
    assert "notes.md" in result.skipped_files


class TestWorkspaceSnapshotHTTPRoutes:
    """HTTP 路由层测试，验证前端请求格式。"""

    def test_create_snapshot_with_json_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """前端应发送 application/json，后端应正常接受并返回 200。"""
        service = WorkspaceRegistryService(
            tmp_path,
            session_manager=SessionManager(tmp_path),
        )
        service.create_workspace(
            user_id="local_default",
            workspace_id="http-snapshot-ws",
            title="HTTP 快照测试",
        )
        _patch_roots(monkeypatch, tmp_path, service)

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)

        response = client.post(
            "/api/workspaces/http-snapshot-ws/snapshots",
            json={"title": "HTTP 测试版本"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "HTTP 测试版本"
        assert data["source"] == "manual"

    def test_apply_snapshot_with_json_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """应用快照接口也应接受 application/json。"""
        service = WorkspaceRegistryService(
            tmp_path,
            session_manager=SessionManager(tmp_path),
        )
        service.create_workspace(
            user_id="local_default",
            workspace_id="http-apply-ws",
            title="HTTP 应用测试",
        )
        _patch_roots(monkeypatch, tmp_path, service)

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)

        create_resp = client.post(
            "/api/workspaces/http-apply-ws/snapshots",
            json={"title": "待应用版本"},
        )
        assert create_resp.status_code == 200
        snapshot_id = create_resp.json()["id"]

        apply_resp = client.post(
            f"/api/workspaces/http-apply-ws/snapshots/{snapshot_id}/apply",
            json={"mode": "soft"},
        )
        assert apply_resp.status_code == 200
        data = apply_resp.json()
        assert data["success"] is True
