"""
运行时数据库 broker 路由
"""

from __future__ import annotations

from fastapi import (  # noqa: F401  （tests 经 route_module.HTTPException 取用）
    APIRouter,
    HTTPException,
    Query,
    Request,
)

from app.api.routes._database_route_common import make_error_mappers
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
from app.services.database import (
    DatabaseAccessBroker,
    decode_runtime_database_token,
)
from app.services.session import SessionManager

router = APIRouter(prefix="/session-database", tags=["session-database"])
_SESSION_MANAGER = SessionManager(WORKSPACE_DIR)
_BROKER = DatabaseAccessBroker(WORKSPACE_DIR, session_manager=_SESSION_MANAGER)


# 三段错误映射收敛自本文件的重复实现，见 _database_route_common.py。
(
    _runtime_database_http_error,
    _map_value_error,
    _map_connector_access_error,
) = make_error_mappers(include_retryable=False)


def _get_runtime_db_context(request: Request):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _runtime_database_http_error(
            status_code=401,
            code="missing_runtime_database_token",
            category="auth",
            message="缺少 runtime database token",
        )

    token = auth_header[7:]
    context = decode_runtime_database_token(token)
    if context is None:
        raise _runtime_database_http_error(
            status_code=401,
            code="invalid_runtime_database_token",
            category="auth",
            message="runtime database token 无效",
        )
    return context


@router.get("/handles", response_model=RuntimeDatabaseHandlesResponse)
async def runtime_database_list_handles(request: Request):
    context = _get_runtime_db_context(request)
    try:
        return _BROKER.list_handles(
            user_id=context.user_id,
            session_id=context.session_id,
            sandbox_mode=context.sandbox_mode,
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
async def runtime_database_query(
    request: Request,
    payload: RuntimeDatabaseQueryRequest,
):
    context = _get_runtime_db_context(request)
    try:
        return await _BROKER.query_async(
            user_id=context.user_id,
            session_id=context.session_id,
            handle=payload.handle,
            sql=payload.sql,
            params=payload.params,
            limit=payload.limit,
            sandbox_mode=context.sandbox_mode,
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
async def runtime_database_execute(
    request: Request,
    payload: RuntimeDatabaseExecuteRequest,
):
    context = _get_runtime_db_context(request)
    try:
        return await _BROKER.execute(
            user_id=context.user_id,
            session_id=context.session_id,
            handle=payload.handle,
            sql=payload.sql,
            params=payload.params,
            sandbox_mode=context.sandbox_mode,
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
async def runtime_database_list_tables(
    request: Request,
    handle: str = Query("", description="数据库资源句柄"),
):
    context = _get_runtime_db_context(request)
    try:
        return await _BROKER.list_tables_async(
            user_id=context.user_id,
            session_id=context.session_id,
            handle=handle,
            sandbox_mode=context.sandbox_mode,
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
async def runtime_database_describe_table(
    request: Request,
    table_name: str,
    handle: str = Query("", description="数据库资源句柄"),
):
    context = _get_runtime_db_context(request)
    try:
        return await _BROKER.describe_table_async(
            user_id=context.user_id,
            session_id=context.session_id,
            handle=handle,
            table_name=table_name,
            sandbox_mode=context.sandbox_mode,
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
