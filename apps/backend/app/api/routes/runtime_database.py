"""
面向前端登录态的运行时数据库 API。

说明：
- 前端使用正常登录态 + session_id 调用，不直接暴露 runtime database token
- 内部仍复用统一的 DatabaseAccessBroker，保证内置 DuckDB 与外部连接器共用一套能力边界
"""

from __future__ import annotations

from typing import Any

from fastapi import (  # noqa: F401  （tests 经 route_module.HTTPException 取用）
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from pydantic import Field

from app.api.routes._database_route_common import make_error_mappers
from app.core.auth import require_auth
from app.core.config import WORKSPACE_DIR
from app.models.database_access import (
    RuntimeDatabaseDescribeTableResponse,
    RuntimeDatabaseExecuteRequest,
    RuntimeDatabaseExecuteResponse,
    RuntimeDatabaseHandlesResponse,
    RuntimeDatabaseListTablesResponse,
    RuntimeDatabaseQueryRequest,
    RuntimeDatabaseQueryResponse,
)
from app.models.user import UserInfo
from app.services.connector import (
    DatabaseConnectorApprovalRejectedError,
    DatabaseConnectorApprovalRequiredError,
    DatabaseConnectorApprovalTimeoutError,
    DatabaseConnectorAttachmentMissingError,
    DatabaseConnectorCapabilityDeniedError,
    DatabaseConnectorGrantDeniedError,
    DatabaseConnectorNotFoundError,
    DatabaseConnectorPlatformRejectionError,
    DatabaseConnectorRemoteExecutionError,
    DatabaseConnectorRemotePermissionError,
)
from app.services.database import DatabaseAccessBroker
from app.services.session import SessionManager

router = APIRouter(prefix="/database/runtime", tags=["database-runtime"])
_SESSION_MANAGER = SessionManager(WORKSPACE_DIR)
_BROKER = DatabaseAccessBroker(WORKSPACE_DIR, session_manager=_SESSION_MANAGER)
_FRONTEND_SANDBOX_MODE = "analysis_ui"


class SessionRuntimeDatabaseQueryRequest(RuntimeDatabaseQueryRequest):
    """前端登录态查询请求。"""

    session_id: str = Field(..., min_length=1, description="目标会话 ID")


class SessionRuntimeDatabaseExecuteRequest(RuntimeDatabaseExecuteRequest):
    """前端登录态执行请求。"""

    session_id: str = Field(..., min_length=1, description="目标会话 ID")


# 三段错误映射收敛自本文件的重复实现，见 _database_route_common.py。
(
    _runtime_database_http_error,
    _map_value_error,
    _map_connector_access_error,
) = make_error_mappers(include_retryable=True)


async def _broker_query(
    *,
    user_id: str,
    session_id: str,
    handle: str,
    sql: str,
    params: list[Any] | dict[str, Any] | None,
    limit: int | None = None,
) -> RuntimeDatabaseQueryResponse:
    return await _BROKER.query_async(
        user_id=user_id,
        session_id=session_id,
        handle=handle,
        sql=sql,
        params=params,
        limit=limit,
        sandbox_mode=_FRONTEND_SANDBOX_MODE,
    )


@router.get("/handles", response_model=RuntimeDatabaseHandlesResponse)
async def list_runtime_database_handles(
    session_id: str = Query(..., min_length=1, description="目标会话 ID"),
    current_user: UserInfo = Depends(require_auth()),
):
    try:
        return _BROKER.list_handles(
            user_id=current_user.user_id,
            session_id=session_id,
            sandbox_mode=_FRONTEND_SANDBOX_MODE,
        )
    except ValueError as exc:
        raise _map_value_error(exc) from exc
    except RuntimeError as exc:
        raise _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=str(exc),
            retryable=True,
        ) from exc


@router.post("/query", response_model=RuntimeDatabaseQueryResponse)
async def query_runtime_database(
    payload: SessionRuntimeDatabaseQueryRequest,
    current_user: UserInfo = Depends(require_auth()),
):
    try:
        return await _broker_query(
            user_id=current_user.user_id,
            session_id=payload.session_id,
            handle=payload.handle,
            sql=payload.sql,
            params=payload.params,
            limit=payload.limit,
        )
    except (
        DatabaseConnectorApprovalRequiredError,
        DatabaseConnectorAttachmentMissingError,
        DatabaseConnectorCapabilityDeniedError,
        DatabaseConnectorGrantDeniedError,
        DatabaseConnectorNotFoundError,
        DatabaseConnectorPlatformRejectionError,
        DatabaseConnectorRemoteExecutionError,
        DatabaseConnectorRemotePermissionError,
    ) as exc:
        raise _map_connector_access_error(exc) from exc
    except ValueError as exc:
        raise _map_value_error(exc) from exc
    except RuntimeError as exc:
        raise _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=str(exc),
            retryable=True,
        ) from exc


@router.post("/execute", response_model=RuntimeDatabaseExecuteResponse)
async def execute_runtime_database(
    payload: SessionRuntimeDatabaseExecuteRequest,
    current_user: UserInfo = Depends(require_auth()),
):
    try:
        return await _BROKER.execute(
            user_id=current_user.user_id,
            session_id=payload.session_id,
            handle=payload.handle,
            sql=payload.sql,
            params=payload.params,
            sandbox_mode=_FRONTEND_SANDBOX_MODE,
        )
    except (
        DatabaseConnectorApprovalRequiredError,
        DatabaseConnectorApprovalRejectedError,
        DatabaseConnectorApprovalTimeoutError,
        DatabaseConnectorAttachmentMissingError,
        DatabaseConnectorCapabilityDeniedError,
        DatabaseConnectorGrantDeniedError,
        DatabaseConnectorNotFoundError,
        DatabaseConnectorPlatformRejectionError,
        DatabaseConnectorRemoteExecutionError,
        DatabaseConnectorRemotePermissionError,
    ) as exc:
        raise _map_connector_access_error(exc) from exc
    except ValueError as exc:
        raise _map_value_error(exc) from exc
    except RuntimeError as exc:
        raise _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=str(exc),
            retryable=True,
        ) from exc


@router.get("/tables", response_model=RuntimeDatabaseListTablesResponse)
async def list_runtime_database_tables(
    session_id: str = Query(..., min_length=1, description="目标会话 ID"),
    handle: str = Query("", description="数据库资源句柄"),
    current_user: UserInfo = Depends(require_auth()),
):
    try:
        return await _BROKER.list_tables_async(
            user_id=current_user.user_id,
            session_id=session_id,
            handle=handle,
            sandbox_mode=_FRONTEND_SANDBOX_MODE,
        )
    except (
        DatabaseConnectorApprovalRequiredError,
        DatabaseConnectorAttachmentMissingError,
        DatabaseConnectorCapabilityDeniedError,
        DatabaseConnectorGrantDeniedError,
        DatabaseConnectorNotFoundError,
        DatabaseConnectorPlatformRejectionError,
        DatabaseConnectorRemoteExecutionError,
        DatabaseConnectorRemotePermissionError,
    ) as exc:
        raise _map_connector_access_error(exc) from exc
    except ValueError as exc:
        raise _map_value_error(exc) from exc
    except RuntimeError as exc:
        raise _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=str(exc),
            retryable=True,
        ) from exc


@router.get("/tables/{table_name}", response_model=RuntimeDatabaseDescribeTableResponse)
async def describe_runtime_database_table(
    table_name: str,
    session_id: str = Query(..., min_length=1, description="目标会话 ID"),
    handle: str = Query("", description="数据库资源句柄"),
    current_user: UserInfo = Depends(require_auth()),
):
    try:
        return await _BROKER.describe_table_async(
            user_id=current_user.user_id,
            session_id=session_id,
            handle=handle,
            table_name=table_name,
            sandbox_mode=_FRONTEND_SANDBOX_MODE,
        )
    except (
        DatabaseConnectorApprovalRequiredError,
        DatabaseConnectorAttachmentMissingError,
        DatabaseConnectorCapabilityDeniedError,
        DatabaseConnectorGrantDeniedError,
        DatabaseConnectorNotFoundError,
        DatabaseConnectorPlatformRejectionError,
        DatabaseConnectorRemoteExecutionError,
        DatabaseConnectorRemotePermissionError,
    ) as exc:
        raise _map_connector_access_error(exc) from exc
    except ValueError as exc:
        raise _map_value_error(exc) from exc
    except RuntimeError as exc:
        raise _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=str(exc),
            retryable=True,
        ) from exc
