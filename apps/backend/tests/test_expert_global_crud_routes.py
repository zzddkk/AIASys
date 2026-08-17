"""测试我的默认（global）层协作专家 REST API。

与 test_expert_crud_routes.py（workspace 层）互为镜像：global 层端点此前
没有任何直接测试，本文件补齐 create/detail/update/delete/enable 的覆盖，
作为 global/workspace 两侧端点收敛重构的行为探针。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.routes import workspaces_core as workspaces_core_module
from app.models.expert import CreateExpertRequest, EnableBuiltinExpertRequest, UpdateExpertRequest
from app.models.user import UserInfo
from app.services import expert_roles as expert_roles_module
from app.services.agent import subagent_catalog
from app.services.session import SessionManager
from app.services.workspace_registry import WorkspaceRegistryService


def _build_user(user_id: str = "local_default") -> UserInfo:
    return UserInfo(user_id=user_id, role="admin", auth_provider="local")


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspaceRegistryService:
    service = WorkspaceRegistryService(tmp_path, session_manager=SessionManager(tmp_path))
    monkeypatch.setattr(
        workspaces_core_module,
        "get_workspace_registry_service",
        lambda: service,
    )
    monkeypatch.setattr(
        expert_roles_module,
        "get_workspace_registry_service",
        lambda: service,
    )
    # global 层数据落在 WORKSPACE_DIR/<user>/global_workspace/.aiasys 下，改指向 tmp_path 隔离
    monkeypatch.setattr(subagent_catalog, "WORKSPACE_DIR", tmp_path)
    return service


@pytest.mark.asyncio
async def test_create_global_expert_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    response = await workspaces_core_module.create_global_expert(
        CreateExpertRequest(
            name="custom_analyst",
            description="自定义数据分析专家",
            system_prompt="你是一个专业的数据分析专家。",
            model="kimi-k2",
            tools=["python", "shell"],
            scope="global",
        ),
        current_user=_build_user(),
    )

    assert response.name == "custom_analyst"
    assert response.description == "自定义数据分析专家"
    assert response.system_prompt == "你是一个专业的数据分析专家。"
    assert response.model == "kimi-k2"
    assert response.tools == ["python", "shell"]
    assert response.scope == "global"
    assert response.source == "custom"


@pytest.mark.asyncio
async def test_create_global_expert_rejects_workspace_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.create_global_expert(
            CreateExpertRequest(
                name="scope_mismatch",
                description="描述",
                system_prompt="prompt",
                scope="workspace",
            ),
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_global_expert_rejects_invalid_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.create_global_expert(
            CreateExpertRequest(
                name="123_invalid",
                description="描述",
                system_prompt="prompt",
                scope="global",
            ),
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400
    assert "格式无效" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_global_expert_rejects_system_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.create_global_expert(
            CreateExpertRequest(
                name="coder",
                description="描述",
                system_prompt="prompt",
                scope="global",
            ),
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail


@pytest.mark.asyncio
async def test_get_global_expert_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    await workspaces_core_module.create_global_expert(
        CreateExpertRequest(
            name="detail_test",
            description="详情测试",
            system_prompt="测试 system prompt",
            scope="global",
        ),
        current_user=_build_user(),
    )

    detail = await workspaces_core_module.get_global_expert_detail(
        "detail_test",
        current_user=_build_user(),
    )

    assert detail.name == "detail_test"
    assert detail.system_prompt == "测试 system prompt"
    assert detail.scope == "global"


@pytest.mark.asyncio
async def test_get_global_expert_detail_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.get_global_expert_detail(
            "nonexistent_expert",
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_update_global_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    await workspaces_core_module.create_global_expert(
        CreateExpertRequest(
            name="update_test",
            description="原始描述",
            system_prompt="原始 prompt",
            model="model-a",
            scope="global",
        ),
        current_user=_build_user(),
    )

    updated = await workspaces_core_module.update_global_expert(
        "update_test",
        UpdateExpertRequest(
            description="更新后的描述",
            system_prompt="更新后的 prompt",
            model="model-b",
            tools=["file_write"],
        ),
        current_user=_build_user(),
    )

    assert updated.description == "更新后的描述"
    assert updated.system_prompt == "更新后的 prompt"
    assert updated.model == "model-b"
    assert updated.tools == ["file_write"]


@pytest.mark.asyncio
async def test_cannot_modify_system_preset_via_global_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.update_global_expert(
            "coder",
            UpdateExpertRequest(description=" hacked"),
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 403
    assert "系统预设" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_delete_global_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    await workspaces_core_module.create_global_expert(
        CreateExpertRequest(
            name="delete_test",
            description="待删除",
            system_prompt="prompt",
            scope="global",
        ),
        current_user=_build_user(),
    )

    result = await workspaces_core_module.delete_global_expert(
        "delete_test",
        current_user=_build_user(),
    )
    assert result["success"] is True
    assert result["name"] == "delete_test"

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.get_global_expert_detail(
            "delete_test",
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_enable_global_builtin_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    response = await workspaces_core_module.enable_global_builtin_expert(
        "coder",
        EnableBuiltinExpertRequest(role_id="coder"),
        current_user=_build_user(),
    )

    assert response.name == "coder"
    assert response.scope == "global"
    assert subagent_catalog.is_subagent_installed_to_scope(
        user_id="local_default",
        name="coder",
        scope="global",
    )


@pytest.mark.asyncio
async def test_enable_global_builtin_expert_rejects_role_id_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await workspaces_core_module.enable_global_builtin_expert(
            "coder",
            EnableBuiltinExpertRequest(role_id="data_analyst"),
            current_user=_build_user(),
        )
    assert exc_info.value.status_code == 400
