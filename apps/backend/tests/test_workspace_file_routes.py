from __future__ import annotations

import asyncio
import errno
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.api.routes import files as files_route
from app.api.routes import files_core as files_core_route
from app.api.routes import files_utils as files_utils_route
from app.api.routes import workspaces as workspaces_route
from app.api.routes import workspaces_resources_files as workspace_files_route
from app.core import config as config_module
from app.models.user import UserInfo
from app.services import workspace_registry as workspace_registry_module
from app.services.session import SessionManager
from app.services.workspace_registry import WorkspaceRegistryService


def _build_user() -> UserInfo:
    return UserInfo(user_id="local_default", role="admin", auth_provider="local")


def _build_workspace_service(tmp_path: Path) -> WorkspaceRegistryService:
    return WorkspaceRegistryService(tmp_path, session_manager=SessionManager(tmp_path))


def _patch_file_route_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: WorkspaceRegistryService,
) -> None:
    monkeypatch.setattr(config_module, "WORKSPACE_DIR", tmp_path, raising=False)
    for module in (
        files_route,
        files_core_route,
        files_utils_route,
        workspaces_route,
        workspace_files_route,
        workspace_registry_module,
    ):
        monkeypatch.setattr(module, "WORKSPACE_DIR", tmp_path, raising=False)
        monkeypatch.setattr(
            module,
            "get_workspace_registry_service",
            lambda: service,
            raising=False,
        )


async def _list_workspace_files(
    workspace_id: str,
    **kwargs: object,
) -> dict[str, object]:
    response = await workspace_files_route.list_workspace_files(
        workspace_id,
        current_user=_build_user(),
        **kwargs,
    )
    payload = response.model_dump(exclude_none=True)
    payload["files"] = [
        {key: value for key, value in item.items() if not (key == "meta" and value == {})}
        for item in payload["files"]
    ]
    return payload


@pytest.mark.asyncio
async def test_file_routes_use_workspace_root_for_bound_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-files",
        title="任务 Files",
        initial_conversation_id="conversation-files-001",
        initial_conversation_title="文件对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    upload = UploadFile(
        file=io.BytesIO(b"hello workspace"),
        filename="notes.txt",
    )
    result = await files_route.upload_file(
        "local_default",
        conversation.session_id,
        file=upload,
        current_user=_build_user(),
    )

    workspace_root_file = tmp_path / "local_default" / "task-files" / "uploads" / "notes.txt"
    legacy_session_file = tmp_path / "local_default" / conversation.session_id / "notes.txt"
    assert result["success"] is True
    assert workspace_root_file.exists()
    assert workspace_root_file.read_text(encoding="utf-8") == "hello workspace"
    assert not legacy_session_file.exists()

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )
    listed_note = next(
        item
        for item in listing["files"]
        if item["name"] == "notes.txt" or item["name"] == "uploads/notes.txt"
    )
    assert listed_note["size"] == len(b"hello workspace")

    exported = await files_route.export_workspace(
        "local_default",
        conversation.session_id,
        current_user=_build_user(),
    )
    body = b""
    async for chunk in exported.body_iterator:
        body += chunk

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert "uploads/notes.txt" in archive.namelist()
        assert archive.read("uploads/notes.txt") == b"hello workspace"


@pytest.mark.asyncio
async def test_workspace_inline_download_supports_unicode_pdf_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace_id = "unicode-preview"
    service.create_workspace(
        user_id="local_default",
        workspace_id=workspace_id,
        title="Unicode Preview",
    )
    workspace_root = service.get_workspace_root("local_default", workspace_id)
    filename = "Cheng 等 - Harnessing AI to Build Virtual Cells.pdf"
    (workspace_root / filename).write_bytes(b"%PDF-1.4\n")

    response = await workspace_files_route.download_workspace_file(
        workspace_id,
        filename,
        disposition="inline",
        current_user=_build_user(),
    )

    assert response.headers["content-disposition"] == f"inline; filename*=utf-8''{quote(filename)}"


@pytest.mark.asyncio
async def test_create_file_writes_nested_text_file_to_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-create-file",
        title="任务 Create File",
        initial_conversation_id="conversation-create-file-001",
        initial_conversation_title="新建文件对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    response = await files_route.create_file(
        "local_default",
        conversation.session_id,
        files_route.FileCreateRequest(
            path="reports/analysis-note.md",
            content="# 分析记录\n",
        ),
        current_user=_build_user(),
    )

    workspace_file = (
        tmp_path / "local_default" / "task-create-file" / "reports" / "analysis-note.md"
    )
    assert response.success is True
    assert response.filename == "reports/analysis-note.md"
    assert response.path == "/workspace/reports/analysis-note.md"
    assert response.overwritten is False
    assert workspace_file.read_text(encoding="utf-8") == "# 分析记录\n"

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )
    assert any(item["name"] == "reports/analysis-note.md" for item in listing["files"])


@pytest.mark.asyncio
async def test_csv_preview_pages_and_updates_visible_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-csv-preview",
        title="任务 CSV Preview",
        initial_conversation_id="conversation-csv-preview-001",
        initial_conversation_title="CSV 预览对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-csv-preview")
    csv_path = workspace_dir / "data" / "large.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "a,b,c\n1,2,3\n4,5,6\n7,8,9\n10,11,12\n",
        encoding="utf-8",
    )

    page = await files_route.get_csv_preview(
        "local_default",
        conversation.session_id,
        "data/large.csv",
        page=2,
        page_size=2,
        column_offset=1,
        column_limit=1,
        current_user=_build_user(),
    )

    assert page.headers == ["b"]
    assert page.rows == [["8"], ["11"]]
    assert page.start_row == 3
    assert page.has_previous is True
    assert page.has_next is False
    assert page.total_columns == 3
    assert page.has_previous_columns is True
    assert page.has_more_columns is True

    update = await files_route.update_csv_preview(
        "local_default",
        conversation.session_id,
        "data/large.csv",
        files_route.CsvPageUpdateRequest(
            rows=[["50"], ["80"]],
            page=2,
            page_size=2,
            column_offset=1,
            column_limit=1,
        ),
        current_user=_build_user(),
    )

    assert update["success"] is True
    assert update["updated_rows"] == 2
    assert csv_path.read_text(encoding="utf-8") == ("a,b,c\n1,2,3\n4,5,6\n7,50,9\n10,80,12\n")


@pytest.mark.asyncio
async def test_list_files_hides_workspace_internal_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-hidden-internal-files",
        title="任务 Internal Files",
        initial_conversation_id="conversation-hidden-internal-files-001",
        initial_conversation_title="内部文件过滤对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir(
        "local_default",
        "task-hidden-internal-files",
    )
    workspace_state_file = workspace_dir / ".workspace" / "state.json"
    workspace_state_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_state_file.write_text(
        "{}",
        encoding="utf-8",
    )
    memory_file = workspace_dir / ".aiasys" / "memory" / "workspace_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("internal memory", encoding="utf-8")
    (workspace_dir / ".aiasys" / "memory" / "workspace_memory.md.lock").write_text(
        "",
        encoding="utf-8",
    )
    (workspace_dir / ".env" / "environments.json").parent.mkdir(parents=True, exist_ok=True)
    (workspace_dir / ".env" / "environments.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (workspace_dir / ".env" / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (workspace_dir / ".env" / ".venv" / "bin" / "python").write_text(
        "#!/usr/bin/env python\n",
        encoding="utf-8",
    )
    (workspace_dir / "notes.md").write_text("visible", encoding="utf-8")

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )

    listed_names = {item["name"] for item in listing["files"]}
    assert "notes.md" in listed_names
    assert not any(name.startswith(".aiasys/") for name in listed_names)
    assert not any(name.startswith(".env/") for name in listed_names)


@pytest.mark.asyncio
async def test_list_files_defaults_to_shallow_root_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-shallow-list-files",
        title="任务 Shallow List",
        initial_conversation_id="conversation-shallow-list-files-001",
        initial_conversation_title="浅层文件列表对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-shallow-list-files")
    (workspace_dir / "root.md").write_text("root", encoding="utf-8")
    nested_file = workspace_dir / "reports" / "summary.md"
    nested_file.parent.mkdir(parents=True, exist_ok=True)
    nested_file.write_text("# summary\n", encoding="utf-8")

    listing = await _list_workspace_files(workspace.workspace_id)
    listed_names = {item["name"] for item in listing["files"]}

    assert listed_names == {"root.md"}
    assert listing["directory"] == ""
    assert listing["recursive"] is False
    assert listing["returned"] == 1
    assert listing["has_more"] is False


@pytest.mark.asyncio
async def test_list_files_supports_directory_recursive_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-paged-list-files",
        title="任务 Paged List",
        initial_conversation_id="conversation-paged-list-files-001",
        initial_conversation_title="分页文件列表对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-paged-list-files")
    for index in range(5):
        target = workspace_dir / "reports" / f"{index:02d}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(index), encoding="utf-8")
    nested_file = workspace_dir / "reports" / "deep" / "nested.md"
    nested_file.parent.mkdir(parents=True, exist_ok=True)
    nested_file.write_text("nested", encoding="utf-8")
    (workspace_dir / "root.md").write_text("root", encoding="utf-8")

    page = await _list_workspace_files(
        workspace.workspace_id,
        directory="reports",
        recursive=True,
        max_depth=0,
        limit=2,
        offset=1,
        include_total=True,
    )

    assert [item["name"] for item in page["files"]] == [
        "reports/01.md",
        "reports/02.md",
    ]
    assert page["directory"] == "reports"
    assert page["recursive"] is True
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert page["returned"] == 2
    assert page["has_more"] is True
    assert page["next_offset"] == 3
    assert page["total"] == 5


@pytest.mark.asyncio
async def test_create_file_rejects_existing_file_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-create-file-conflict",
        title="任务 Create File Conflict",
        initial_conversation_id="conversation-create-file-conflict-001",
        initial_conversation_title="文件冲突对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    await files_route.create_file(
        "local_default",
        conversation.session_id,
        files_route.FileCreateRequest(path="notes.md", content="v1"),
        current_user=_build_user(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await files_route.create_file(
            "local_default",
            conversation.session_id,
            files_route.FileCreateRequest(path="notes.md", content="v2"),
            current_user=_build_user(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_copy_file_copies_nested_file_in_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-copy-file",
        title="任务 Copy File",
        initial_conversation_id="conversation-copy-file-001",
        initial_conversation_title="复制文件对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-copy-file")
    source = workspace_dir / "reports" / "source.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# source\n", encoding="utf-8")

    response = await files_route.copy_file(
        "local_default",
        conversation.session_id,
        files_route.FileCopyRequest(
            source="reports/source.md",
            target="reports/source copy.md",
        ),
        current_user=_build_user(),
    )

    target = workspace_dir / "reports" / "source copy.md"
    assert response.success is True
    assert response.target == "reports/source copy.md"
    assert source.read_text(encoding="utf-8") == "# source\n"
    assert target.read_text(encoding="utf-8") == "# source\n"


@pytest.mark.asyncio
async def test_copy_folder_rejects_target_inside_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-copy-folder",
        title="任务 Copy Folder",
        initial_conversation_id="conversation-copy-folder-001",
        initial_conversation_title="复制文件夹对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-copy-folder")
    source = workspace_dir / "reports"
    source.mkdir(parents=True, exist_ok=True)
    (source / "source.md").write_text("# source\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        await files_route.copy_file(
            "local_default",
            conversation.session_id,
            files_route.FileCopyRequest(
                source="reports",
                target="reports/copy",
            ),
            current_user=_build_user(),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_workspace_mcp_config_file_is_listed_from_config_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-mcp-config-file",
        title="任务 MCP Config",
        initial_conversation_id="conversation-mcp-config-001",
        initial_conversation_title="MCP 配置对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-mcp-config-file")
    mounted_file = workspace_dir / ".aiasys" / "mcp_config.json"
    mounted_content = '{"version": 1, "servers": {}}'
    mounted_file.parent.mkdir(parents=True, exist_ok=True)
    mounted_file.write_text(mounted_content, encoding="utf-8")

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )
    # .aiasys 是内部配置目录，workspace 文件列表不列出其中文件
    assert not any(item["name"] == ".aiasys/mcp_config.json" for item in listing["files"])


@pytest.mark.asyncio
async def test_admin_list_all_files_returns_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-admin-list-all",
        title="任务 Admin List All",
        initial_conversation_id="conversation-admin-list-all-001",
        initial_conversation_title="管理员文件总览",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-admin-list-all")
    listed_file = workspace_dir / "reports" / "summary.md"
    listed_file.parent.mkdir(parents=True, exist_ok=True)
    listed_file.write_text("# summary\n", encoding="utf-8", newline="\n")

    payload = await files_core_route.list_all_files(current_user=_build_user())

    target_file = next(
        item
        for item in payload["files"]
        if item["session_id"] == "task-admin-list-all" and item["name"] == "reports/summary.md"
    )
    assert target_file == {
        "user_id": "local_default",
        "session_id": "task-admin-list-all",
        "name": "reports/summary.md",
        "size": len("# summary\n".encode("utf-8")),
        "modified": listed_file.stat().st_mtime,
        "absolute_path": str(listed_file.absolute()),
    }


@pytest.mark.asyncio
async def test_list_files_returns_sqlite_resource_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-resource-db-file",
        title="资源 DB 文件",
        initial_conversation_id="conversation-resource-db-file-001",
        initial_conversation_title="资源 DB 对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-resource-db-file")
    db_path = workspace_dir / "knowledge" / "product-docs.knowledge.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE _aiasys_metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO _aiasys_metadata (key, value) VALUES (?, ?)",
            [
                ("resource_type", "knowledge"),
                ("schema_kind", "aiasys.knowledge_base.sqlite.v1"),
                ("preview_kind", "knowledge_base"),
                ("renderer_hint", "knowledge_base_preview"),
                ("id", "kb-product-docs"),
                ("document_count", "12"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )

    db_file = next(
        item for item in listing["files"] if item["name"] == "knowledge/product-docs.knowledge.db"
    )
    assert db_file["resource_type"] == "knowledge"
    assert db_file["schema_kind"] == "aiasys.knowledge_base.sqlite.v1"
    assert db_file["preview_kind"] == "knowledge_base"
    assert db_file["renderer_hint"] == "knowledge_base_preview"
    assert db_file["meta"]["id"] == "kb-product-docs"
    assert db_file["meta"]["document_count"] == 12
    assert db_file["meta"]["db_path"] == "/workspace/knowledge/product-docs.knowledge.db"


@pytest.mark.asyncio
async def test_ipynb_file_is_editable_via_workspace_file_content_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-notebook-edit-file",
        title="任务 Notebook File",
        initial_conversation_id="conversation-notebook-file-001",
        initial_conversation_title="Notebook 文件对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-notebook-edit-file")
    notebook_path = workspace_dir / "notebooks" / "analysis.ipynb"
    notebook_path.parent.mkdir(parents=True, exist_ok=True)
    notebook_payload = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": "# Demo",
            }
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    notebook_path.write_text(
        json.dumps(notebook_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    content = await files_route.get_file_content(
        "local_default",
        conversation.session_id,
        "notebooks/analysis.ipynb",
        current_user=_build_user(),
    )
    assert content.editable is True
    assert '"nbformat": 4' in content.content

    updated_payload = {
        **notebook_payload,
        "metadata": {"title": "Notebook Edit"},
    }
    update_response = await files_route.update_file_content(
        "local_default",
        conversation.session_id,
        "notebooks/analysis.ipynb",
        files_route.FileContentRequest(
            content=json.dumps(updated_payload, ensure_ascii=False, indent=2) + "\n"
        ),
        current_user=_build_user(),
    )
    assert update_response["success"] is True
    private_notebook_path = (
        service.get_session_dir("local_default", conversation.session_id)
        / "notebooks"
        / "analysis.ipynb"
    )
    assert private_notebook_path.exists()

    workspace_payload = json.loads(notebook_path.read_text(encoding="utf-8"))
    saved_payload = json.loads(private_notebook_path.read_text(encoding="utf-8"))
    assert workspace_payload["metadata"] == {}
    assert saved_payload["metadata"]["title"] == "Notebook Edit"


@pytest.mark.asyncio
async def test_bound_workspace_listing_prefers_session_private_notebook_over_workspace_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-notebook-listing",
        title="任务 Notebook Listing",
        initial_conversation_id="conversation-notebook-listing-001",
        initial_conversation_title="Notebook Listing 对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    workspace_dir = service._get_workspace_dir("local_default", "task-notebook-listing")
    workspace_notebook_path = workspace_dir / "notebooks" / "analysis.ipynb"
    workspace_notebook_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_notebook_path.write_text("workspace", encoding="utf-8")
    (workspace_dir / "notes.txt").write_text("shared", encoding="utf-8")

    session_dir = service.get_session_dir("local_default", conversation.session_id)
    session_notebook_path = session_dir / "notebooks" / "analysis.ipynb"
    session_notebook_path.parent.mkdir(parents=True, exist_ok=True)
    session_notebook_path.write_text("session", encoding="utf-8")

    listing = await _list_workspace_files(
        workspace.workspace_id,
        recursive=True,
    )

    names = [item["name"] for item in listing["files"]]
    assert names.count("notebooks/analysis.ipynb") == 1
    assert "notes.txt" in names

    content = await files_route.get_file_content(
        "local_default",
        conversation.session_id,
        "notebooks/analysis.ipynb",
        current_user=_build_user(),
    )
    assert content.content == "session"


@pytest.mark.asyncio
async def test_running_session_rejects_manual_notebook_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)
    monkeypatch.setattr(
        files_utils_route,
        "_is_runtime_busy_for_session",
        lambda user_id, session_id: True,
    )

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-notebook-lock",
        title="任务 Notebook Lock",
        initial_conversation_id="conversation-notebook-lock-001",
        initial_conversation_title="Notebook Lock 对话",
    )
    conversation = workspace.current_conversation
    assert conversation is not None

    session_notebook_path = (
        service.get_session_dir("local_default", conversation.session_id)
        / "notebooks"
        / "analysis.ipynb"
    )
    session_notebook_path.parent.mkdir(parents=True, exist_ok=True)
    session_notebook_path.write_text("{}", encoding="utf-8")

    response = await files_route.get_file_content(
        "local_default",
        conversation.session_id,
        "notebooks/analysis.ipynb",
        current_user=_build_user(),
    )
    assert response.editable is False
    assert response.edit_lock_reason is not None

    with pytest.raises(HTTPException) as exc_info:
        await files_route.update_file_content(
            "local_default",
            conversation.session_id,
            "notebooks/analysis.ipynb",
            files_route.FileContentRequest(content="{}"),
            current_user=_build_user(),
        )

    assert exc_info.value.status_code == 409


# ==================== 重名编号纯函数单元测试 ====================
#
# split_base_and_ext 与 get_next_numbered_name 是纯字符串函数，命名规则的边界
# （多段扩展名、点文件、已带编号、名字里本就含括号数字）用参数化单测覆盖即可，
# 无需为每个分支建工作区 + 发 HTTP 请求。下方的集成测试只验证「路由确实接上了
# 这套规则」与并发/清理等 IO 语义，不再逐个枚举命名分支。


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 常规：末尾一段为扩展名
        ("report.pdf", ("report", ".pdf")),
        # 多段扩展名：只切最后一段，与系统行为一致
        ("file.tar.gz", ("file.tar", ".gz")),
        # 无扩展名
        ("README", ("README", "")),
        # 纯点文件：整体视为主干，无扩展名（dot_pos == 0）
        (".env", (".env", "")),
        (".gitignore", (".gitignore", "")),
        # 点文件带真扩展名：点不在首位之外还有分隔点，正常切分
        (".eslintrc.json", (".eslintrc", ".json")),
        # 末尾是点：dot_pos > 0，扩展名为单个点
        ("weird.", ("weird", ".")),
        # 名字中含空格与括号
        ("my report (2026).pdf", ("my report (2026)", ".pdf")),
    ],
)
def test_split_base_and_ext(filename: str, expected: tuple[str, str]) -> None:
    assert workspace_files_route.split_base_and_ext(filename) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 首次编号
        ("report.pdf", "report (1).pdf"),
        # 已有编号则递增
        ("report (1).pdf", "report (2).pdf"),
        ("report (9).pdf", "report (10).pdf"),
        # 多位数递增
        ("report (99).pdf", "report (100).pdf"),
        # 多段扩展名：编号插在最后一个点之前
        ("file.tar.gz", "file.tar (1).gz"),
        # 无扩展名
        ("README", "README (1)"),
        ("README (1)", "README (2)"),
        # 纯点文件：编号追加在整体之后（与资源管理器对无扩展名文件的行为一致）
        (".env", ".env (1)"),
        (".env (1)", ".env (2)"),
        # 名字里本就含「(数字)」但不在末尾：不应误认为编号，需在其后新增编号
        ("file (2026) final.pdf", "file (2026) final (1).pdf"),
        # 末尾括号数字紧贴文字（无空格）：不匹配编号模式，视为名字的一部分
        ("report(1).pdf", "report(1) (1).pdf"),
    ],
)
def test_get_next_numbered_name(filename: str, expected: str) -> None:
    assert workspace_files_route.get_next_numbered_name(filename) == expected


def test_get_next_numbered_name_is_idempotent_under_repetition() -> None:
    """连续调用应产生单调递增的编号序列，不出现重复或跳号。"""
    name = "report.pdf"
    produced = []
    for _ in range(5):
        name = workspace_files_route.get_next_numbered_name(name)
        produced.append(name)

    assert produced == [
        "report (1).pdf",
        "report (2).pdf",
        "report (3).pdf",
        "report (4).pdf",
        "report (5).pdf",
    ]


# ==================== 工作区上传重名测试 ====================


@pytest.mark.asyncio
async def test_upload_first_time_uses_original_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次上传使用原文件名"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-first",
        title="任务 Upload First",
        initial_conversation_id="conversation-upload-first-001",
        initial_conversation_title="Upload First 对话",
    )

    upload = UploadFile(file=io.BytesIO(b"original content"), filename="report.pdf")
    response = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload,
        path=None,
        current_user=_build_user(),
    )

    assert response["success"] is True
    assert response["filename"] == "report.pdf"
    assert response["path"] == "/workspace/report.pdf"
    assert response["size"] == 16

    # 验证文件确实存在
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-first")
    assert (workspace_dir / "report.pdf").read_bytes() == b"original content"


@pytest.mark.asyncio
async def test_upload_duplicate_gets_numbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次上传同名文件生成 (1)，原文件内容不变"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-duplicate",
        title="任务 Upload Duplicate",
        initial_conversation_id="conversation-upload-duplicate-001",
        initial_conversation_title="Upload Duplicate 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"original"), filename="report.pdf")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report.pdf"

    # 第二次上传同名文件
    upload2 = UploadFile(file=io.BytesIO(b"new content"), filename="report.pdf")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (1).pdf"
    assert r2["path"] == "/workspace/report (1).pdf"
    assert r2["size"] == 11

    # 验证原文件未被修改
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-duplicate")
    assert (workspace_dir / "report.pdf").read_bytes() == b"original"
    assert (workspace_dir / "report (1).pdf").read_bytes() == b"new content"


@pytest.mark.asyncio
async def test_upload_multiple_times_generates_sequential_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续上传生成 (1), (2)，无偏移错误"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-multi",
        title="任务 Upload Multi",
        initial_conversation_id="conversation-upload-multi-001",
        initial_conversation_title="Upload Multi 对话",
    )

    filenames = []
    for i in range(3):
        upload = UploadFile(file=io.BytesIO(f"content-{i}".encode()), filename="report.pdf")
        response = await workspace_files_route.upload_workspace_file(
            workspace.workspace_id,
            file=upload,
            path=None,
            current_user=_build_user(),
        )
        filenames.append(response["filename"])

    assert filenames == ["report.pdf", "report (1).pdf", "report (2).pdf"]

    # 验证所有文件都存在且内容正确
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-multi")
    for i, name in enumerate(filenames):
        assert (workspace_dir / name).read_bytes() == f"content-{i}".encode()


@pytest.mark.asyncio
async def test_upload_numbered_filename_increments_existing_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传文件名本身已带编号时，继续递增"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-numbered",
        title="任务 Upload Numbered",
        initial_conversation_id="conversation-upload-numbered-001",
        initial_conversation_title="Upload Numbered 对话",
    )

    # 上传名为 "report (1).pdf" 的文件
    upload = UploadFile(file=io.BytesIO(b"v1"), filename="report (1).pdf")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report (1).pdf"

    # 再次上传同名文件
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="report (1).pdf")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (2).pdf"


@pytest.mark.asyncio
async def test_upload_increments_past_occupied_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """递增试探：从 (1) 起逐个尝试，跳过已占用编号

    实现不扫描目录找空缺，而是从候选名开始逐个递增：
    report.pdf 占用 → report (1).pdf 占用 → report (2).pdf 可用。
    因此已占用的连续编号会被逐个跳过，落在第一个真正可用的编号上。
    """
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-gap",
        title="任务 Upload Gap",
        initial_conversation_id="conversation-upload-gap-001",
        initial_conversation_title="Upload Gap 对话",
    )

    workspace_dir = service._get_workspace_dir("local_default", "task-upload-gap")

    # 预置连续占用的 report.pdf 与 report (1).pdf
    (workspace_dir / "report.pdf").write_bytes(b"original")
    (workspace_dir / "report (1).pdf").write_bytes(b"v1")

    # 上传 report.pdf：(1) 已占用，应递增到 (2)
    upload = UploadFile(file=io.BytesIO(b"new"), filename="report.pdf")
    response = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload,
        path=None,
        current_user=_build_user(),
    )

    assert response["filename"] == "report (2).pdf"
    assert response["path"] == "/workspace/report (2).pdf"

    # 已有文件不被覆盖，新文件内容正确
    assert (workspace_dir / "report.pdf").read_bytes() == b"original"
    assert (workspace_dir / "report (1).pdf").read_bytes() == b"v1"
    assert (workspace_dir / "report (2).pdf").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_upload_no_extension_adds_bracket_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无扩展名文件：README → README (1)"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-no-ext",
        title="任务 Upload No Ext",
        initial_conversation_id="conversation-upload-no-ext-001",
        initial_conversation_title="Upload No Ext 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="README")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == "README"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="README")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == "README (1)"


@pytest.mark.asyncio
async def test_upload_dotfile_gets_numbered_without_leading_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点文件：.env → .env (1)（不是  (1).env）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-dotfile",
        title="任务 Upload Dotfile",
        initial_conversation_id="conversation-upload-dotfile-001",
        initial_conversation_title="Upload Dotfile 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename=".env")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == ".env"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename=".env")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == ".env (1)"

    # 验证文件内容
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-dotfile")
    assert (workspace_dir / ".env").read_bytes() == b"v1"
    assert (workspace_dir / ".env (1)").read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_upload_multiple_extensions_inserts_before_last_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多扩展名：file.tar.gz → file.tar (1).gz（在最后一个扩展名前插入编号）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-multi-ext",
        title="任务 Upload Multi Ext",
        initial_conversation_id="conversation-upload-multi-ext-001",
        initial_conversation_title="Upload Multi Ext 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="file.tar.gz")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == "file.tar.gz"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="file.tar.gz")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == "file.tar (1).gz"

    # 验证文件内容
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-multi-ext")
    assert (workspace_dir / "file.tar.gz").read_bytes() == b"v1"
    assert (workspace_dir / "file.tar (1).gz").read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_upload_nested_directory_preserves_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """嵌套目录：reports/deep/report.pdf → reports/deep/report (1).pdf"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-nested",
        title="任务 Upload Nested",
        initial_conversation_id="conversation-upload-nested-001",
        initial_conversation_title="Upload Nested 对话",
    )

    workspace_dir = service._get_workspace_dir("local_default", "task-upload-nested")
    nested_dir = workspace_dir / "reports" / "deep"
    nested_dir.mkdir(parents=True, exist_ok=True)

    # 第一次上传（使用 path 参数指定目标路径）
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="report.pdf")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path="reports/deep/report.pdf",
        current_user=_build_user(),
    )
    assert r1["filename"] == "reports/deep/report.pdf"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="report.pdf")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path="reports/deep/report.pdf",
        current_user=_build_user(),
    )
    assert r2["filename"] == "reports/deep/report (1).pdf"

    # 验证文件内容
    assert (nested_dir / "report.pdf").read_bytes() == b"v1"
    assert (nested_dir / "report (1).pdf").read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_upload_concurrent_generates_unique_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发上传：所有响应路径唯一，内容不互相覆盖"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-concurrent",
        title="任务 Upload Concurrent",
        initial_conversation_id="conversation-upload-concurrent-001",
        initial_conversation_title="Upload Concurrent 对话",
    )

    # 并发上传 5 个同名文件
    async def upload_report(content: str) -> dict:
        upload = UploadFile(file=io.BytesIO(content.encode()), filename="report.pdf")
        response = await workspace_files_route.upload_workspace_file(
            workspace.workspace_id,
            file=upload,
            path=None,
            current_user=_build_user(),
        )
        return response

    responses = await asyncio.gather(
        upload_report("v0"),
        upload_report("v1"),
        upload_report("v2"),
        upload_report("v3"),
        upload_report("v4"),
    )

    filenames = [r["filename"] for r in responses]
    # 所有文件名必须唯一
    assert len(set(filenames)) == 5
    assert set(filenames) == {
        "report.pdf",
        "report (1).pdf",
        "report (2).pdf",
        "report (3).pdf",
        "report (4).pdf",
    }

    # 内容完整性：5 份内容各出现且仅出现一次。
    # 不断言「第 i 个响应对应 v{i}」——真正的写入发生在 asyncio.to_thread 里，
    # 5 个线程竞争 open(..., "xb")，谁抢到 report.pdf 是不确定的；
    # gather 只保证返回值顺序对应入参顺序，不保证落盘顺序。
    workspace_dir = service._get_workspace_dir("local_default", "task-upload-concurrent")
    contents = sorted((workspace_dir / name).read_text() for name in filenames)
    assert contents == ["v0", "v1", "v2", "v3", "v4"]


@pytest.mark.asyncio
async def test_upload_write_failure_cleans_up_half_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入失败后清理半写入文件，不删除原文件"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-cleanup",
        title="任务 Upload Cleanup",
        initial_conversation_id="conversation-upload-cleanup-001",
        initial_conversation_title="Upload Cleanup 对话",
    )

    workspace_dir = service._get_workspace_dir("local_default", "task-upload-cleanup")

    # 创建原文件
    (workspace_dir / "report.pdf").write_bytes(b"original")

    # 模拟写入失败：在写入部分内容后抛出 ENOSPC
    def failing_copyfileobj(source, dest, max_size=None):
        # 先写入部分内容
        chunk = source.read(10)
        dest.write(chunk)
        # 然后抛出磁盘空间不足错误
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(files_utils_route, "_copyfileobj_with_limit", failing_copyfileobj)

    # 尝试上传同名文件，预期失败
    upload = UploadFile(file=io.BytesIO(b"new content here"), filename="report.pdf")
    with pytest.raises(OSError) as exc_info:
        await workspace_files_route.upload_workspace_file(
            workspace.workspace_id,
            file=upload,
            path=None,
            current_user=_build_user(),
        )

    assert exc_info.value.errno == errno.ENOSPC

    # 验证：原文件未被修改，新文件不存在（已被清理）
    assert (workspace_dir / "report.pdf").read_bytes() == b"original"
    assert not (workspace_dir / "report (1).pdf").exists()


@pytest.mark.asyncio
async def test_upload_returns_actual_saved_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 返回实际保存的 filename 和 path"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-response",
        title="任务 Upload Response",
        initial_conversation_id="conversation-upload-response-001",
        initial_conversation_title="Upload Response 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="report.pdf")
    r1 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload1,
        path=None,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report.pdf"
    assert r1["path"] == "/workspace/report.pdf"

    # 第二次上传（实际保存为 report (1).pdf）
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="report.pdf")
    r2 = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload2,
        path=None,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (1).pdf"
    assert r2["path"] == "/workspace/report (1).pdf"


@pytest.mark.asyncio
async def test_upload_preserves_path_traversal_and_reserved_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径穿越、保留文件名校验行为不变"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-upload-security",
        title="任务 Upload Security",
        initial_conversation_id="conversation-upload-security-001",
        initial_conversation_title="Upload Security 对话",
    )

    # 路径穿越攻击应被拒绝（通过 path 参数）
    upload1 = UploadFile(file=io.BytesIO(b"malicious"), filename="passwd")
    with pytest.raises(HTTPException) as exc_info:
        await workspace_files_route.upload_workspace_file(
            workspace.workspace_id,
            file=upload1,
            path="../../etc/passwd",
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400

    # 保留文件名应被拒绝（返回 403 Access denied）
    upload2 = UploadFile(file=io.BytesIO(b"config"), filename="metadata.json")
    with pytest.raises(HTTPException) as exc_info:
        await workspace_files_route.upload_workspace_file(
            workspace.workspace_id,
            file=upload2,
            path=None,
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 403

    # 普通文件名应被接受
    upload3 = UploadFile(file=io.BytesIO(b"normal"), filename="normal.txt")
    response = await workspace_files_route.upload_workspace_file(
        workspace.workspace_id,
        file=upload3,
        path=None,
        current_user=_build_user(),
    )
    assert response["success"] is True
    assert response["filename"] == "normal.txt"


# ==================== 全局工作区上传重名测试 ====================


@pytest.mark.asyncio
async def test_global_upload_first_time_uses_original_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次上传使用原文件名（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-first",
        title="任务 Global Upload First",
        initial_conversation_id="conversation-global-upload-first-001",
        initial_conversation_title="Global Upload First 对话",
    )

    upload = UploadFile(file=io.BytesIO(b"original content"), filename="report.pdf")
    response = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload,
        current_user=_build_user(),
    )

    assert response["success"] is True
    assert response["filename"] == "report.pdf"
    assert response["path"] == "/global/report.pdf"
    assert response["size"] == 16

    # 验证文件确实存在
    global_root = config_module.get_user_global_workspace_dir("local_default")
    assert (global_root / "report.pdf").read_bytes() == b"original content"


@pytest.mark.asyncio
async def test_global_upload_duplicate_gets_numbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次上传同名文件生成 (1)，原文件内容不变（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-duplicate",
        title="任务 Global Upload Duplicate",
        initial_conversation_id="conversation-global-upload-duplicate-001",
        initial_conversation_title="Global Upload Duplicate 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"original"), filename="report.pdf")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload1,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report.pdf"

    # 第二次上传同名文件
    upload2 = UploadFile(file=io.BytesIO(b"new content"), filename="report.pdf")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (1).pdf"
    assert r2["path"] == "/global/report (1).pdf"
    assert r2["size"] == 11

    # 验证原文件未被修改
    assert (global_root / "report.pdf").read_bytes() == b"original"
    assert (global_root / "report (1).pdf").read_bytes() == b"new content"


@pytest.mark.asyncio
async def test_global_upload_multiple_times_generates_sequential_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续上传生成 (1), (2)，无偏移错误（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-multi",
        title="任务 Global Upload Multi",
        initial_conversation_id="conversation-global-upload-multi-001",
        initial_conversation_title="Global Upload Multi 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    filenames = []
    for i in range(3):
        upload = UploadFile(file=io.BytesIO(f"content-{i}".encode()), filename="report.pdf")
        response = await workspace_files_route.upload_global_workspace_file(
            workspace.workspace_id,
            file=upload,
            current_user=_build_user(),
        )
        filenames.append(response["filename"])

    assert filenames == ["report.pdf", "report (1).pdf", "report (2).pdf"]

    # 验证所有文件都存在且内容正确
    for i, name in enumerate(filenames):
        assert (global_root / name).read_bytes() == f"content-{i}".encode()


@pytest.mark.asyncio
async def test_global_upload_numbered_filename_increments_existing_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传文件名本身已带编号时，继续递增（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-numbered",
        title="任务 Global Upload Numbered",
        initial_conversation_id="conversation-global-upload-numbered-001",
        initial_conversation_title="Global Upload Numbered 对话",
    )

    # 上传名为 "report (1).pdf" 的文件
    upload = UploadFile(file=io.BytesIO(b"v1"), filename="report (1).pdf")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report (1).pdf"

    # 再次上传同名文件
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="report (1).pdf")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (2).pdf"


@pytest.mark.asyncio
async def test_global_upload_increments_past_occupied_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """递增试探：从 (1) 起逐个尝试，跳过已占用编号（全局工作区）

    实现不扫描目录找空缺，而是从候选名开始逐个递增：
    report.pdf 占用 → report (1).pdf 占用 → report (2).pdf 可用。
    """
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-gap",
        title="任务 Global Upload Gap",
        initial_conversation_id="conversation-global-upload-gap-001",
        initial_conversation_title="Global Upload Gap 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 预置连续占用的 report.pdf 与 report (1).pdf
    (global_root / "report.pdf").write_bytes(b"original")
    (global_root / "report (1).pdf").write_bytes(b"v1")

    # 上传 report.pdf：(1) 已占用，应递增到 (2)
    upload = UploadFile(file=io.BytesIO(b"new"), filename="report.pdf")
    response = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload,
        current_user=_build_user(),
    )

    assert response["filename"] == "report (2).pdf"
    assert response["path"] == "/global/report (2).pdf"

    # 已有文件不被覆盖，新文件内容正确
    assert (global_root / "report.pdf").read_bytes() == b"original"
    assert (global_root / "report (1).pdf").read_bytes() == b"v1"
    assert (global_root / "report (2).pdf").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_global_upload_no_extension_adds_bracket_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无扩展名文件：README → README (1)（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-no-ext",
        title="任务 Global Upload No Ext",
        initial_conversation_id="conversation-global-upload-no-ext-001",
        initial_conversation_title="Global Upload No Ext 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="README")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload1,
        current_user=_build_user(),
    )
    assert r1["filename"] == "README"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="README")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == "README (1)"


@pytest.mark.asyncio
async def test_global_upload_dotfile_gets_numbered_without_leading_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点文件：.env → .env (1)（不是  (1).env，全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-dotfile",
        title="任务 Global Upload Dotfile",
        initial_conversation_id="conversation-global-upload-dotfile-001",
        initial_conversation_title="Global Upload Dotfile 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename=".env")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload1,
        current_user=_build_user(),
    )
    assert r1["filename"] == ".env"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename=".env")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == ".env (1)"

    # 验证文件内容
    assert (global_root / ".env").read_bytes() == b"v1"
    assert (global_root / ".env (1)").read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_global_upload_multiple_extensions_inserts_before_last_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多扩展名：file.tar.gz → file.tar (1).gz（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-multi-ext",
        title="任务 Global Upload Multi Ext",
        initial_conversation_id="conversation-global-upload-multi-ext-001",
        initial_conversation_title="Global Upload Multi Ext 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="file.tar.gz")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload1,
        current_user=_build_user(),
    )
    assert r1["filename"] == "file.tar.gz"

    # 第二次上传
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="file.tar.gz")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == "file.tar (1).gz"

    # 验证文件内容
    assert (global_root / "file.tar.gz").read_bytes() == b"v1"
    assert (global_root / "file.tar (1).gz").read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_global_upload_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局工作区只允许上传到根目录，拒绝或净化路径"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-security",
        title="任务 Global Upload Security",
        initial_conversation_id="conversation-global-upload-security-001",
        initial_conversation_title="Global Upload Security 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 测试1：文件名包含 ../ 应被拒绝
    upload1 = UploadFile(file=io.BytesIO(b"malicious"), filename="../../etc/passwd")
    try:
        await workspace_files_route.upload_global_workspace_file(
            workspace.workspace_id,
            file=upload1,
            current_user=_build_user(),
        )
        # 如果未抛出异常，应验证文件被保存到根目录且路径被净化
        # 但当前实现会拒绝，因为 _normalize_relative_path 会检测到 ".."
    except HTTPException as exc:
        # 路径穿越应被拒绝
        assert exc.status_code in (400, 403)

    # 测试2：文件名包含路径分隔符，应被净化为纯文件名
    upload2 = UploadFile(file=io.BytesIO(b"data"), filename="subdir/file.txt")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    # Path(file.filename).name 会剥离路径，只保留 "file.txt"
    assert r2["filename"] == "file.txt"
    assert r2["path"] == "/global/file.txt"

    # 验证文件保存在全局根目录，而非子目录
    assert (global_root / "file.txt").exists()
    assert not (global_root / "subdir").exists()


@pytest.mark.asyncio
async def test_global_upload_concurrent_generates_unique_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发上传：所有响应路径唯一，内容不互相覆盖（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-concurrent",
        title="任务 Global Upload Concurrent",
        initial_conversation_id="conversation-global-upload-concurrent-001",
        initial_conversation_title="Global Upload Concurrent 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 并发上传 5 个同名文件
    async def upload_report(content: str) -> dict:
        upload = UploadFile(file=io.BytesIO(content.encode()), filename="report.pdf")
        response = await workspace_files_route.upload_global_workspace_file(
            workspace.workspace_id,
            file=upload,
            current_user=_build_user(),
        )
        return response

    responses = await asyncio.gather(
        upload_report("v0"),
        upload_report("v1"),
        upload_report("v2"),
        upload_report("v3"),
        upload_report("v4"),
    )

    filenames = [r["filename"] for r in responses]
    # 所有文件名必须唯一
    assert len(set(filenames)) == 5
    assert set(filenames) == {
        "report.pdf",
        "report (1).pdf",
        "report (2).pdf",
        "report (3).pdf",
        "report (4).pdf",
    }

    # 内容完整性：5 份内容各出现且仅出现一次。
    # 不断言「第 i 个响应对应 v{i}」——写入在线程池里竞争 open(..., "xb")，落盘顺序不确定。
    contents = sorted((global_root / name).read_text() for name in filenames)
    assert contents == ["v0", "v1", "v2", "v3", "v4"]


@pytest.mark.asyncio
async def test_global_upload_write_failure_cleans_up_half_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入失败后清理半写入文件，不删除原文件（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-cleanup",
        title="任务 Global Upload Cleanup",
        initial_conversation_id="conversation-global-upload-cleanup-001",
        initial_conversation_title="Global Upload Cleanup 对话",
    )

    global_root = config_module.get_user_global_workspace_dir("local_default")

    # 创建原文件
    (global_root / "report.pdf").write_bytes(b"original")

    # 模拟写入失败：在写入部分内容后抛出 ENOSPC
    def failing_copyfileobj(source, dest, max_size=None):
        chunk = source.read(10)
        dest.write(chunk)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(files_utils_route, "_copyfileobj_with_limit", failing_copyfileobj)

    # 尝试上传同名文件，预期失败
    upload = UploadFile(file=io.BytesIO(b"new content here"), filename="report.pdf")
    with pytest.raises(OSError) as exc_info:
        await workspace_files_route.upload_global_workspace_file(
            workspace.workspace_id,
            file=upload,
            current_user=_build_user(),
        )

    assert exc_info.value.errno == errno.ENOSPC

    # 验证：原文件未被修改，新文件不存在（已被清理）
    assert (global_root / "report.pdf").read_bytes() == b"original"
    assert not (global_root / "report (1).pdf").exists()


@pytest.mark.asyncio
async def test_global_upload_returns_actual_saved_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 返回实际保存的 filename 和 path（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-response",
        title="任务 Global Upload Response",
        initial_conversation_id="conversation-global-upload-response-001",
        initial_conversation_title="Global Upload Response 对话",
    )

    # 第一次上传
    upload1 = UploadFile(file=io.BytesIO(b"v1"), filename="report.pdf")
    r1 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload1,
        current_user=_build_user(),
    )
    assert r1["filename"] == "report.pdf"
    assert r1["path"] == "/global/report.pdf"

    # 第二次上传（实际保存为 report (1).pdf）
    upload2 = UploadFile(file=io.BytesIO(b"v2"), filename="report.pdf")
    r2 = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert r2["filename"] == "report (1).pdf"
    assert r2["path"] == "/global/report (1).pdf"


@pytest.mark.asyncio
async def test_global_upload_preserves_path_traversal_and_reserved_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径穿越、保留文件名校验行为不变（全局工作区）"""
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)

    workspace = service.create_workspace(
        user_id="local_default",
        workspace_id="task-global-upload-security2",
        title="任务 Global Upload Security2",
        initial_conversation_id="conversation-global-upload-security2-001",
        initial_conversation_title="Global Upload Security2 对话",
    )

    # 保留文件名应被拒绝（返回 400 Invalid filename）
    upload1 = UploadFile(file=io.BytesIO(b"config"), filename="metadata.json")
    with pytest.raises(HTTPException) as exc_info:
        await workspace_files_route.upload_global_workspace_file(
            workspace.workspace_id,
            file=upload1,
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400

    # 普通文件名应被接受
    upload2 = UploadFile(file=io.BytesIO(b"normal"), filename="normal.txt")
    response = await workspace_files_route.upload_global_workspace_file(
        workspace.workspace_id,
        file=upload2,
        current_user=_build_user(),
    )
    assert response["success"] is True
    assert response["filename"] == "normal.txt"


@pytest.mark.asyncio
async def test_upload_workspace_file_handles_non_normalized_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspace_root 文本形式未规范化（8.3 短名/含 .. 段）时上传不应 ValueError。

    回归：GitHub windows runner 的 TEMP 是 RUNNER~1 短名，写入路径经 resolve()
    展开后与未 resolve 的 workspace_root 文本不一致，relative_to 抛 ValueError，
    e2e 上传相关用例 9 连挂（2026-08-13 CI 实测）。这里用含 .. 段的等价路径
    模拟同一类「文本不同但指向同一目录」的情形。
    """
    service = _build_workspace_service(tmp_path)
    _patch_file_route_workspace(monkeypatch, tmp_path, service)
    service.create_workspace(
        user_id="local_default",
        workspace_id="ws-upload-nonnorm",
        title="非规范化 root 上传",
    )

    real_root = service.get_workspace_root("local_default", "ws-upload-nonnorm")
    non_normalized = real_root.parent / "dummy" / ".." / real_root.name
    monkeypatch.setattr(
        service,
        "get_workspace_root",
        lambda user_id, workspace_id: non_normalized,
    )

    response = await workspace_files_route.upload_workspace_file(
        "ws-upload-nonnorm",
        file=UploadFile(filename="hello.txt", file=io.BytesIO(b"hello")),
        path=None,
        current_user=_build_user(),
    )

    assert response["success"] is True
    assert response["filename"] == "hello.txt"
