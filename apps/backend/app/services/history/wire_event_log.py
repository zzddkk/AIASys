"""会话 wire 事件日志（v2）：append-only 单一事实源。

设计文档：AIASys-product-design/交互设计/session-event-log-design.md。

A1 阶段定位：与既有持久化机制（history.json 覆写、display sidecar）
并行双写，读路径仍走旧机制；双写一致性由测试看守，A2 才切读路径。

写盘纪律（照搬 step-code wire.jsonl）：
- 只追加，物理上永不改写；
- 读取时容忍最后一行截断（进程可能死在写一半），坏行跳过；
- 任何写失败只告警不抛出，绝不影响对话主流程。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

from app.services.session.constants import SESSION_DIR_NAME
from app.utils.path_utils import as_system_path

logger = logging.getLogger(__name__)

WIRE_LOG_FORMAT_VERSION = 2

# message.append 事件载荷允许携带的消息字段白名单。
# 不在列的字段（如 tool_calls 的执行中间态、usage 记录）不进事件流。
_MESSAGE_EVENT_FIELDS = (
    "id",
    "role",
    "content",
    "display_content",
    "reasoning_content",
    "reasoning_signature",
    "reasoning_redacted_data",
    "tool_calls",
    "tool_call_id",
    "compaction_stats",
    "origin",
    "turn_n",
)


def wire_log_path(session_dir: Path, session_id: str) -> Path:
    """返回指定会话的 wire 事件日志路径。"""
    return Path(session_dir) / SESSION_DIR_NAME / session_id / "wire.jsonl"


def append_wire_event(
    session_dir: Path,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """追加一条事件到 wire 日志。失败只告警，不抛出。"""
    try:
        wire_file = wire_log_path(session_dir, session_id)
        wire_file.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(event)
        payload.setdefault("timestamp", time.time())
        with open(as_system_path(str(wire_file)), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(
            "写入 wire 事件失败（继续执行）: session=%s event_type=%s error=%s",
            session_id,
            event.get("type"),
            exc,
        )


def build_message_append_event(message: dict[str, Any]) -> dict[str, Any]:
    """把一条内部消息构造成 message.append 事件（字段白名单过滤）。"""
    payload = {
        key: message[key]
        for key in _MESSAGE_EVENT_FIELDS
        if key in message and message[key] is not None
    }
    return {"type": "message.append", "message": payload}


def append_message_event(
    session_dir: Path,
    session_id: str,
    message: dict[str, Any],
) -> None:
    """双写入口：一条消息进入会话历史时调用。"""
    append_wire_event(session_dir, session_id, build_message_append_event(message))


def read_wire_events(wire_file: Path) -> list[dict[str, Any]]:
    """读取 wire 日志全部事件。容忍坏行与末行截断，文件不存在返回空。"""
    path = Path(as_system_path(str(wire_file)))
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except Exception as exc:
        logger.warning("读取 wire 事件失败: path=%s error=%s", wire_file, exc)
    return events


def project_messages_from_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """显示/上下文投影（A1 版）：从事件流重建消息序列。

    只处理 message.append；turn/token_usage 等审计事件不参与消息序列。
    后续切片在此扩展 apply_compaction / truncate_to 的重放语义。
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "message.append":
            continue
        message = event.get("message")
        if isinstance(message, dict):
            messages.append(dict(message))
    return messages
