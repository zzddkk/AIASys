"""对话归档（隐藏）语义的看守测试。

设计依据：归档 ≠ 删除。归档只置 exclude_from_user_history，默认列表隐藏、
数据保留、可恢复；删除是物理移到 .trash。

看守点：
1. 归档后默认 list_conversations 不返回，include_hidden=True 才返回；
2. summary.archived 字段正确透出；
3. 取消归档后恢复显示；
4. 不存在的 conversation 返回 False，不抛；
5. session metadata 同步写入（重建会话不丢标记）。
"""

from __future__ import annotations

from pathlib import Path

from app.services.session import SessionManager
from app.services.workspace_registry import WorkspaceRegistryService


def _build_service(tmp_path: Path) -> WorkspaceRegistryService:
    return WorkspaceRegistryService(tmp_path, session_manager=SessionManager(tmp_path))


def _make_workspace_with_conversation(tmp_path: Path):
    service = _build_service(tmp_path)
    user_id = "local_default"
    ws = service.create_workspace(user_id=user_id, title="归档测试工作区")
    conv = service.create_conversation(
        user_id=user_id,
        workspace_id=ws.workspace_id,
        title="待归档对话",
    )
    return service, user_id, ws.workspace_id, conv.conversation_id, conv.session_id


class TestConversationArchive:
    def test_archive_hides_from_default_list_but_keeps_data(self, tmp_path):
        service, user_id, ws_id, cid, sid = _make_workspace_with_conversation(tmp_path)

        def active_ids():
            return [c.conversation_id for c in service.list_conversations(user_id, ws_id)]

        # 归档前目标对话可见
        assert cid in active_ids()

        ok = service.set_conversation_archived(
            user_id=user_id, workspace_id=ws_id, conversation_id=cid, archived=True
        )
        assert ok is True

        # 默认列表隐藏目标对话（工作区初始会话仍在）
        assert cid not in active_ids()
        # include_hidden 时目标对话可见，且 archived=True
        hidden = service.list_conversations(user_id, ws_id, include_hidden_conversations=True)
        target = next(c for c in hidden if c.conversation_id == cid)
        assert target.archived is True

        # 数据未删：仍能取到单条
        detail = service.get_conversation(user_id=user_id, workspace_id=ws_id, conversation_id=cid)
        assert detail.title == "待归档对话"

    def test_unarchive_restores_visibility(self, tmp_path):
        service, user_id, ws_id, cid, sid = _make_workspace_with_conversation(tmp_path)
        service.set_conversation_archived(
            user_id=user_id, workspace_id=ws_id, conversation_id=cid, archived=True
        )
        assert cid not in [c.conversation_id for c in service.list_conversations(user_id, ws_id)]

        service.set_conversation_archived(
            user_id=user_id, workspace_id=ws_id, conversation_id=cid, archived=False
        )
        visible = service.list_conversations(user_id, ws_id)
        target = next(c for c in visible if c.conversation_id == cid)
        assert target.archived is False

    def test_archive_writes_session_metadata(self, tmp_path):
        """归档标记要写进 session metadata，否则重建会话时标记丢失。"""
        service, user_id, ws_id, cid, sid = _make_workspace_with_conversation(tmp_path)
        service.set_conversation_archived(
            user_id=user_id, workspace_id=ws_id, conversation_id=cid, archived=True
        )
        meta = service.session_manager.get_session(sid, user_id)
        assert meta is not None
        assert meta.exclude_from_user_history is True

    def test_archive_nonexistent_returns_false(self, tmp_path):
        service = _build_service(tmp_path)
        ws = service.create_workspace(user_id="local_default", title="空工作区")
        assert (
            service.set_conversation_archived(
                user_id="local_default",
                workspace_id=ws.workspace_id,
                conversation_id="does-not-exist",
                archived=True,
            )
            is False
        )

    def test_probe_archive_without_metadata_write_loses_flag_on_recreate(self, tmp_path):
        """探针：只改 conversations.json 不写 session metadata，重建会话读 metadata 时标记丢失。

        正确实现必须双写：payload（列表过滤即时生效）+ metadata（recreate 路径读它）。
        """
        service, user_id, ws_id, cid, sid = _make_workspace_with_conversation(tmp_path)

        # 缺陷行为：只改 conversations.json payload，不写 metadata
        payloads = service._read_conversation_payloads(user_id, ws_id)
        for p in payloads:
            if str(p.get("conversation_id")) == cid:
                p["exclude_from_user_history"] = True
        service._write_conversation_payloads(user_id, ws_id, payloads)

        # payload 层面归档了，默认列表也确实隐藏（因为 _is_hidden 先查 payload）
        assert cid not in [c.conversation_id for c in service.list_conversations(user_id, ws_id)]

        # 但 session metadata 没写——重建会话（recreate 路径）读 metadata.exclude_from_user_history，
        # 标记会复活。这证明只写 payload 不够。
        meta = service.session_manager.get_session(sid, user_id)
        assert meta is not None
        assert meta.exclude_from_user_history is False  # 缺陷版本：metadata 未同步

        # 正确实现双写：metadata 也为 True
        service.set_conversation_archived(
            user_id=user_id, workspace_id=ws_id, conversation_id=cid, archived=True
        )
        meta = service.session_manager.get_session(sid, user_id)
        assert meta.exclude_from_user_history is True
