"""runtime_database 与 session_database 路由共享的错误映射。

两个路由面向不同调用方（前端登录态 vs runtime token 直连），但底层都是
DatabaseAccessBroker，异常 → HTTP 错误码的契约必须一致。此前是两份逐字
相同的实现（_map_value_error / _map_connector_access_error 共约 150 行），
任何一边新增映射另一边静默漏掉，两套 API 返回的错误码就会分叉——
2026-08-13 函数级重复扫描发现，收敛到本模块。

唯一允许的行为差异通过 include_retryable 显式声明：session 侧的
_runtime_database_http_error 历史上不输出 retryable 字段（响应模型 dump
时排除），runtime 侧输出。工厂把它做成参数，差异保留在调用方而不是
藏在两份拷贝里。
"""

from __future__ import annotations

from fastapi import HTTPException

from app.models.database_access import RuntimeDatabaseErrorDetail
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


def make_error_mappers(*, include_retryable: bool):
    """返回 (http_error, map_value_error, map_connector_access_error)。

    include_retryable=False 时 retryable 参数被接受但不进响应体，
    保持 session_database 路由的历史行为不变。
    """

    def _runtime_database_http_error(
        *,
        status_code: int,
        code: str,
        category: str,
        message: str,
        retryable: bool = False,
    ) -> HTTPException:
        include = {"code", "category", "message"}
        if include_retryable:
            include.add("retryable")
        return HTTPException(
            status_code=status_code,
            detail=RuntimeDatabaseErrorDetail(
                code=code,
                category=category,
                message=message,
                retryable=retryable,
            ).model_dump(include=include),
        )

    def _map_value_error(exc: ValueError) -> HTTPException:
        message = str(exc)
        if message == "目标会话不存在":
            return _runtime_database_http_error(
                status_code=404,
                code="session_not_found",
                category="session",
                message=message,
            )
        if message.startswith("不支持的数据库句柄"):
            return _runtime_database_http_error(
                status_code=400,
                code="invalid_handle",
                category="request",
                message=message,
            )
        if message == "数据库连接器句柄缺少 connector_id":
            return _runtime_database_http_error(
                status_code=400,
                code="invalid_handle",
                category="request",
                message=message,
            )
        if message == "非法表名":
            return _runtime_database_http_error(
                status_code=400,
                code="invalid_table_name",
                category="request",
                message=message,
            )
        return _runtime_database_http_error(
            status_code=400,
            code="invalid_request",
            category="request",
            message=message,
        )

    def _map_connector_access_error(exc: Exception) -> HTTPException:
        message = str(exc)

        if isinstance(exc, DatabaseConnectorAttachmentMissingError):
            return _runtime_database_http_error(
                status_code=403,
                code="session_connector_not_attached",
                category="session",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorNotFoundError):
            return _runtime_database_http_error(
                status_code=404,
                code="connector_not_found",
                category="session",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorCapabilityDeniedError):
            return _runtime_database_http_error(
                status_code=403,
                code="capability_denied",
                category="platform",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorGrantDeniedError):
            return _runtime_database_http_error(
                status_code=403,
                code="platform_grant_denied",
                category="platform",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorApprovalTimeoutError):
            return _runtime_database_http_error(
                status_code=409,
                code="approval_timeout",
                category="approval",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorApprovalRejectedError):
            return _runtime_database_http_error(
                status_code=403,
                code="approval_rejected",
                category="approval",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorApprovalRequiredError):
            return _runtime_database_http_error(
                status_code=403,
                code="approval_required",
                category="approval",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorRemotePermissionError):
            return _runtime_database_http_error(
                status_code=403,
                code="remote_permission_denied",
                category="remote",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorRemoteExecutionError):
            return _runtime_database_http_error(
                status_code=502,
                code="remote_execution_error",
                category="remote",
                message=message,
            )
        if isinstance(exc, DatabaseConnectorPlatformRejectionError):
            if message == "数据库连接器不存在":
                return _runtime_database_http_error(
                    status_code=404,
                    code="connector_not_found",
                    category="session",
                    message=message,
                )
            if message == "会话未挂载该数据库连接器":
                return _runtime_database_http_error(
                    status_code=403,
                    code="session_connector_not_attached",
                    category="session",
                    message=message,
                )
            if "未获授权执行动作" in message:
                return _runtime_database_http_error(
                    status_code=403,
                    code="platform_grant_denied",
                    category="platform",
                    message=message,
                )
            if "能力上限不支持动作" in message:
                return _runtime_database_http_error(
                    status_code=403,
                    code="capability_denied",
                    category="platform",
                    message=message,
                )
            return _runtime_database_http_error(
                status_code=403,
                code="platform_rejected",
                category="platform",
                message=message,
            )

        return _runtime_database_http_error(
            status_code=502,
            code="runtime_error",
            category="runtime",
            message=message,
            retryable=True,
        )

    return _runtime_database_http_error, _map_value_error, _map_connector_access_error
