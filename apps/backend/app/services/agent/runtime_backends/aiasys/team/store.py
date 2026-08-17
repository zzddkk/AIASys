"""Multi-agent team collaboration store (mission state layer + resource lease table).

# 移植自 step-code src/agent/team/store.ts（MIT, Copyright (c) 2026 stepfun-ai）
# 变更：
# - 去掉 git worktree / git diff / git merge 等 git 依赖（AIASys 不做 worktree 隔离）
# - 状态落盘改为 asyncio 友好的原子写（tempfile + os.replace）
# - scope 比对使用 os.path.realpath() + 路径分量前缀，避免符号链接与裸字符串误匹配
# - merge 门的 git 相关门（门③ tip 校验、门⑤ diff 范围校验）改为产出物指纹比对
# - 第三步新增：资源租约表（仅 notebook / kernel 独占，其余靠 scope 声明互斥）
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class TeamMission:
    """Mission 六态状态机中的任务实体。"""

    id: str
    title: str
    kind: str  # 'build' | 'survey'
    scope: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    status: str = "planned"
    owner: str | None = None
    reviewed_commit: str | None = None
    lease: dict[str, Any] | None = None  # 资源租约声明（规划期填写）
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # 运行时持有的资源键列表（spawn 时填充，teardown 时清空）
    # 持久化到 state.json 以支持审计与进程重启后恢复占用关系
    resource_lease_keys: list[str] = field(default_factory=list)


@dataclass
class TeamState:
    """Team 全局状态（唯一事实源）。"""

    version: int = 1
    base: str = ""
    repo_root: str = ""
    closed_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    missions: list[TeamMission] = field(default_factory=list)
    # 运行时资源租约表：{normalized_lease_key -> {mission_id, resource_type, ...}}
    # 持久化以支持进程重启后恢复占用关系
    resource_leases: dict[str, Any] = field(default_factory=dict)


class TeamError(Exception):
    """团队操作错误（状态迁移失败、门控不通过等）。"""


# ---------------------------------------------------------------------------
# State machine（移植自 step-code store.ts 的状态迁移规则）
# ---------------------------------------------------------------------------

# 合法迁移表（含 step-code 原注释的阈值理据）
# planned → active：正常启动
# planned → blocked：依赖不满足
# planned → paused：人为暂停
# active → completed：worker 完成
# active → blocked：执行失败
# active → paused：人为暂停
# blocked → active：重试（respawn）
# paused → active：恢复
# completed → active：返工（rework，审阅打回）
# completed → merged：收编
# blocked → merged：依赖完成后直接收编（罕见但允许）
# merged 终态，不再出
# active/completed/paused/blocked → planned 不允许（不能回退）
VALID_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active", "blocked", "paused"},
    "active": {"completed", "blocked", "paused"},
    "blocked": {"active", "merged"},
    "paused": {"active"},
    "completed": {"active", "merged"},
    "merged": set(),  # 终态
}


def _assert_valid_transition(mission: TeamMission, new_status: str) -> None:
    """抛出明确异常，绝不依赖 LLM 自觉遵守。"""
    allowed = VALID_TRANSITIONS.get(mission.status, set())
    if new_status not in allowed:
        raise TeamError(
            f"任务 {mission.id} 当前状态是 {mission.status}，不允许迁移到 {new_status}。"
            f" 合法目标：{sorted(allowed) or '(无，终态)'}"
        )


# ---------------------------------------------------------------------------
# Scope helpers（移植自 step-code scopeMatches / scopesOverlap）
# ---------------------------------------------------------------------------


def _normalize_scope(raw: str) -> str:
    """归一为目录前缀：`src/data/**` / `src/data/*` → `src/data/`。"""
    return raw.replace("\\", "/").rstrip("/")


def _path_components(p: str) -> tuple[str, ...]:
    """把路径拆成分量（兼容 POSIX/Windows 分隔符）。"""
    return tuple(part for part in p.replace("\\", "/").split("/") if part)


def _scope_covers_path(scope: str, file_path: str) -> bool:
    """目录语义的前缀匹配。"""
    scope_norm = _normalize_scope(scope)
    file_norm = _normalize_scope(file_path)
    if scope_norm == file_norm:
        return True
    if not scope_norm.endswith("/"):
        scope_norm += "/"
    return file_norm.startswith(scope_norm) or file_norm.startswith(scope_norm.replace("//", "/"))


def _scopes_overlap(a: str, b: str) -> bool:
    """两个 scope 是否冲突（目录语义下互为前缀即重叠）。"""
    try:
        a_real = os.path.realpath(a) if os.path.exists(a) else os.path.normpath(a)
    except (OSError, ValueError):
        a_real = os.path.normpath(a)
    try:
        b_real = os.path.realpath(b) if os.path.exists(b) else os.path.normpath(b)
    except (OSError, ValueError):
        b_real = os.path.normpath(b)

    a_norm = a_real.replace("\\", "/").rstrip("/")
    b_norm = b_real.replace("\\", "/").rstrip("/")
    if a_norm == b_norm:
        return True
    a_parts = tuple(part for part in a_norm.split("/") if part)
    b_parts = tuple(part for part in b_norm.split("/") if part)
    min_len = min(len(a_parts), len(b_parts))
    if min_len == 0:
        return False
    return a_parts[:min_len] == b_parts[:min_len]


def _resolve_real_path(path: str) -> str:
    """规范化路径（解析符号链接 + 绝对路径）。"""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


# ---------------------------------------------------------------------------
# Resource Lease Helpers
# ---------------------------------------------------------------------------

_RESOURCE_LEASE_MAP: dict[str, dict[str, Any]] = {
    # 工具名 → {resource_type, resource_id_arg, exclusive, require_lease}
    # 判断依据（写进注释，防止实现漂移）：
    #
    # notebook / kernel（exclusive）：
    #   IPython 内核命名空间是会话级共享的，两个 worker 同时在一个内核里跑代码
    #   会互相覆盖变量，这是 AIASys 特有的冲突热点，git worktree 之类的文件级
    #   隔离完全管不住。设计文档 3.6 明确这是唯一需要独占租约的资源类型。
    #
    # dataset（非独占，但删除类要求持有租约）：
    #   insert 语义（CreateDataTable / InsertDataTableRecords）天然并发安全，无需独占。
    #   删除操作（DeleteDataTableRecord）不可逆，且会让其它 mission 正在读的资源消失，
    #   因此要求操作前该资源已在本 mission 的租约内登记（require_lease=True），
    #   但不阻止多个 mission 同时持有同一数据表的租约（非独占）。
    #
    # knowledge_graph（非独占，但删除/实体操作要求持有租约）：
    #   insert 语义（CreateKnowledgeGraph / CreateGraphEntity / CreateGraphRelation）
    #   天然并发安全。删除操作（DeleteKnowledgeGraph / DeleteGraphEntity）不可逆，
    #   同理由 require_lease=True。
    #   注意：同一资源类型有两种参数名——CreateKnowledgeGraph 用 graph_id，
    #   CreateGraphEntity/DeleteGraphEntity 用 base_id（两者语义相同，都是图谱 ID）。
    #   本次不动工具参数名，映射表分别注册即可。
    #
    # knowledge_base（非独占，但删除类要求持有租约）：
    #   文档入库（CreateKnowledgeBase / UploadDocuments）是 insert 语义。
    #   删除（DeleteDocumentsFromKnowledgeBase / DeleteKnowledgeBase）不可逆，
    #   同理由 require_lease=True。
    #
    # env_id（非独占）：
    #   os.environ 修改是进程级的，hermes.py 有 __exit__ 还原机制，
    #   并发下竞态待后续修复 hermes.py，此处仅登记不独占。
    #
    # canvas（无资源租约，靠路径守卫）：
    #   文件操作，write_allow_root 路径守卫已足够。
    "EditNotebookFile": {
        "resource_type": "notebook",
        "resource_id_arg": "notebook_path",
        "exclusive": True,
        "require_lease": True,
    },
    "CreateSessionNotebook": {
        "resource_type": "notebook",
        "resource_id_arg": "notebook_path",
        "exclusive": True,
        "require_lease": True,
    },
    "RunNotebook": {
        "resource_type": "notebook",
        "resource_id_arg": "notebook_path",
        "exclusive": True,
        "require_lease": True,
    },
    "WriteCanvas": {
        "resource_type": "canvas",
        "resource_id_arg": "canvas_path",
        "exclusive": False,
        "require_lease": False,
    },
    "CreateDataTable": {
        "resource_type": "dataset",
        "resource_id_arg": "table_id",
        "exclusive": False,
        "require_lease": False,
    },
    "DeleteDataTableRecord": {
        "resource_type": "dataset",
        "resource_id_arg": "table_path",
        "exclusive": False,
        "require_lease": True,  # 删除不可逆，要求持有租约
    },
    "InsertDataTableRecords": {
        "resource_type": "dataset",
        "resource_id_arg": "table_path",
        "exclusive": False,
        "require_lease": False,
    },
    "UpdateDataTableRecord": {
        "resource_type": "dataset",
        "resource_id_arg": "table_path",
        "exclusive": False,
        "require_lease": False,
    },
    "CreateKnowledgeGraph": {
        "resource_type": "knowledge_graph",
        "resource_id_arg": "graph_id",
        "exclusive": False,
        "require_lease": False,
    },
    "DeleteKnowledgeGraph": {
        "resource_type": "knowledge_graph",
        "resource_id_arg": "graph_id",
        "exclusive": False,
        "require_lease": True,  # 删除不可逆，要求持有租约
    },
    "CreateGraphEntity": {
        "resource_type": "knowledge_graph",
        "resource_id_arg": "base_id",
        "exclusive": False,
        "require_lease": False,
    },
    "DeleteGraphEntity": {
        "resource_type": "knowledge_graph",
        "resource_id_arg": "base_id",
        "exclusive": False,
        "require_lease": True,  # 删除不可逆，要求持有租约
    },
    "CreateGraphRelation": {
        "resource_type": "knowledge_graph",
        "resource_id_arg": "base_id",
        "exclusive": False,
        "require_lease": False,
    },
    "CreateKnowledgeBase": {
        "resource_type": "knowledge_base",
        "resource_id_arg": "name",
        "exclusive": False,
        "require_lease": False,
    },
    "DeleteDocumentsFromKnowledgeBase": {
        "resource_type": "knowledge_base",
        "resource_id_arg": "knowledge_base_id",
        "exclusive": False,
        "require_lease": True,  # 删除不可逆，要求持有租约
    },
    "DeleteKnowledgeBase": {
        "resource_type": "knowledge_base",
        "resource_id_arg": "knowledge_base_id",
        "exclusive": False,
        "require_lease": True,  # 删除不可逆，要求持有租约
    },
    "DeleteEnvVar": {
        "resource_type": "env_id",
        "resource_id_arg": "name",
        "exclusive": False,
        "require_lease": False,
    },
}


def _normalize_lease_key(resource_type: str, resource_id: str) -> str:
    """生成规范化的租约键：`{resource_type}:{normalized_resource_id}`。"""
    return f"{resource_type}:{_normalize_scope(str(resource_id))}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# TeamStore
# ---------------------------------------------------------------------------


class TeamStore:
    """Mission 状态层：六态状态机 + 依赖硬化门控 + scope 互斥检查 + 资源租约表 + 原子写落盘。"""

    def __init__(self, state_dir: str) -> None:
        self._state_dir = Path(state_dir).resolve()
        self._state_file = self._state_dir / "state.json"
        # 并发安全：asyncio.Lock 保护 load-modify-save 循环。
        self._lock = asyncio.Lock()
        # 运行时资源租约表（内存中）：{normalized_lease_key -> {mission_id, ...}}
        # 与 state.json 中的 resource_leases 字段保持同步（写操作通过 _save 持久化）。
        self._runtime_lease_table: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Persistence（原子写 + 并发安全）
    # ------------------------------------------------------------------

    async def _load(self) -> TeamState:
        """从磁盘加载状态，同时恢复运行时租约表。"""
        try:
            raw = await asyncio.to_thread(self._state_file.read_text, encoding="utf-8")
        except FileNotFoundError as exc:
            raise TeamError("team 尚未初始化——先运行 team_init。") from exc
        data: dict[str, Any] = json.loads(raw)
        # 恢复运行时租约表，并同步到 TeamState（让 state.resource_leases 反映内存表）
        self._runtime_lease_table = {k: v for k, v in data.pop("resource_leases", {}).items()}
        data["resource_leases"] = dict(self._runtime_lease_table)
        # 反序列化：把 dict 转回 dataclass
        if "missions" in data and isinstance(data["missions"], list):
            data["missions"] = [
                TeamMission(**m) if isinstance(m, dict) else m for m in data["missions"]
            ]
        return TeamState(**data)

    async def _save(self, state: TeamState) -> None:
        """原子写：先写临时文件，再 os.replace。"""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._state_dir), suffix=".tmp", prefix=".state-"
        )
        try:
            # 将运行时租约表嵌入 state 以便持久化
            state.resource_leases = dict(self._runtime_lease_table)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=_dataclass_default)
            os.replace(tmp_path, str(self._state_file))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def load(self) -> TeamState:
        """公开接口：加载状态（持有锁）。"""
        async with self._lock:
            return await self._load()

    async def save(self, state: TeamState) -> None:
        """公开接口：保存状态（持有锁）。"""
        async with self._lock:
            await self._save(state)

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    async def init(self, repo_root: str, base: str = "") -> TeamState:
        """初始化团队状态（幂等：已初始化则保留全部状态）。"""
        async with self._lock:
            if self._state_file.exists():
                state = await self._load()
                if state.closed_at is not None:
                    state.closed_at = None
                    await self._save(state)
                return state

            state = TeamState(
                base=base or "main",
                repo_root=_resolve_real_path(repo_root),
            )
            await self._save(state)
            return state

    # ------------------------------------------------------------------
    # Plan（登记任务 + scope 互斥 + deps 校验）
    # ------------------------------------------------------------------

    async def plan(self, missions: list[TeamMission]) -> list[TeamMission]:
        """登记一批任务。"""
        async with self._lock:
            state = await self._load()
            existing_ids = {m.id for m in state.missions}

            new_missions: list[TeamMission] = []
            for idx, m in enumerate(missions):
                m.id = f"M{len(state.missions) + idx + 1}"

                for dep in m.deps:
                    if dep not in existing_ids and dep not in {n.id for n in new_missions}:
                        raise TeamError(f"任务 {m.id} 依赖了不存在的任务「{dep}」。")

                if m.kind == "build":
                    candidates = [
                        (other.id, other.scope, other.lease)
                        for other in state.missions
                        if other.kind == "build" and other.status != "merged"
                    ] + [(n.id, n.scope, n.lease) for n in new_missions if n.kind == "build"]

                    # scope 互斥（文件路径）
                    for other_id, other_scope, _other_lease in candidates:
                        for s1 in m.scope:
                            for s2 in other_scope:
                                if _scopes_overlap(s1, s2):
                                    raise TeamError(
                                        f"任务 {m.id} 的 scope「{s1}」与 {other_id} 的「{s2}」重叠——"
                                        f"build 类任务的 scope 必须两两不互斥，请重新划分。"
                                    )

                    # 资源 ID 互斥（lease 中声明的非文件资源）
                    # 与 scope 互斥同构：同一资源标识同时出现在两个 build 任务的 lease 中即冲突。
                    # 这防止「文件 scope 不重叠但操作同一 graph_id/table_id」的漏洞。
                    _LEASE_RESOURCE_KEYS = {
                        "datasets",
                        "connections",
                        "env_id",
                        "kernel",
                        "memory",
                        "notebook",
                    }
                    for other_id, _other_scope, other_lease in candidates:
                        if not m.lease or not other_lease:
                            continue
                        for res_type, res_ids in m.lease.items():
                            if res_type == "files" or res_type not in _LEASE_RESOURCE_KEYS:
                                continue
                            if not isinstance(res_ids, list):
                                continue
                            other_res_ids = other_lease.get(res_type, [])
                            if not isinstance(other_res_ids, list):
                                continue
                            for rid in res_ids:
                                if rid and str(rid).strip() in [
                                    str(r).strip() for r in other_res_ids if r
                                ]:
                                    raise TeamError(
                                        f"任务 {m.id} 的 lease 中资源「{res_type}:{rid}」"
                                        f"与 {other_id} 的同一资源冲突——"
                                        f"build 类任务的 lease 资源标识必须两两不互斥，请重新划分。"
                                    )

                new_missions.append(m)

            state.missions.extend(new_missions)
            await self._save(state)
            return new_missions

    # ------------------------------------------------------------------
    # Status transition（状态机）
    # ------------------------------------------------------------------

    async def set_status(self, mission_id: str, new_status: str) -> TeamMission:
        """迁移任务到新状态（系统强制门控）。"""
        async with self._lock:
            state = await self._load()
            mission = next((m for m in state.missions if m.id == mission_id), None)
            if mission is None:
                raise TeamError(f"任务 {mission_id} 不存在。")

            _assert_valid_transition(mission, new_status)

            if new_status == "active":
                # 建索引再查，不要在推导式里对同一个 dep 调两次 next()：那样既是
                # O(2n) 重复遍历，也让 `is None` 短路保护的对象与随后访问 .status
                # 的对象成为两次独立调用的结果，mypy 无法确认非空（union-attr）。
                by_id = {m.id: m for m in state.missions}
                unmerged = [
                    dep for dep in mission.deps if dep not in by_id or by_id[dep].status != "merged"
                ]
                if unmerged:
                    raise TeamError(
                        f"任务 {mission.id} 的依赖 {unmerged} 尚未全部 merged——"
                        f"依赖未满足前不能启动（系统门，非 prompt 约束）。"
                    )

            mission.status = new_status
            await self._save(state)
            return mission

    # ------------------------------------------------------------------
    # Merge（收编检查：六道门）
    # ------------------------------------------------------------------

    async def merge(
        self,
        mission_id: str,
        reviewed_commit: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """收编任务（六道门检查）。"""
        async with self._lock:
            state = await self._load()
            mission = next((m for m in state.missions if m.id == mission_id), None)
            if mission is None:
                raise TeamError(f"任务 {mission_id} 不存在。")

            if mission.status != "completed":
                raise TeamError(
                    f"任务 {mission.id} 状态是 {mission.status}，只有 completed 才能合并。"
                )

            # 同 set_status 的门：建索引再查，避免双次 next() 与 union-attr。
            by_id = {m.id: m for m in state.missions}
            unmerged = [
                dep for dep in mission.deps if dep not in by_id or by_id[dep].status != "merged"
            ]
            if unmerged:
                raise TeamError(f"门④：依赖 {unmerged} 尚未合并。")

            if reviewed_commit is None:
                raise TeamError("门①：未传入 reviewed_commit，请先审阅。")

            if artifacts is not None and len(artifacts) == 0:
                raise TeamError(
                    f"门〇：任务 {mission.id} 没有任何产出物落点。"
                    " 可能原因：① worker 忘了保存产出；② worker 提交到了错误的位置。"
                )

            if artifacts is not None:
                current_fp = _compute_artifacts_fingerprint(artifacts)
                if current_fp != reviewed_commit:
                    raise TeamError(
                        f"门③：产出物指纹已变更（审阅时 {reviewed_commit[:12]}，现在 {current_fp[:12]}）——请重新审阅。"
                    )

            if artifacts is not None and mission.lease is not None:
                allowed_files = _normalize_lease_files(mission.lease)
                for art in artifacts:
                    art_path = art.get("path", "")
                    if not _is_path_allowed(art_path, allowed_files):
                        raise TeamError(
                            f"门⑤：产出物 {art_path} 超出任务 lease 范围 {allowed_files}。"
                        )

            _assert_valid_transition(mission, "merged")
            mission.status = "merged"
            mission.reviewed_commit = reviewed_commit
            await self._save(state)

            conflicts_with = _find_scope_conflicts(state.missions, mission, artifacts)
            return {"conflictsWith": conflicts_with, "kept": False}

    # ------------------------------------------------------------------
    # Inbox（文件信箱）
    # ------------------------------------------------------------------

    async def inbox(self, name: str, limit: int = 20) -> list[dict[str, str]]:
        """读取团队信箱，newest-first。"""
        inbox_dir = self._state_dir / "comms" / "inbox"
        messages: list[dict[str, str]] = []
        try:
            entries = sorted(inbox_dir.iterdir(), reverse=True)
        except FileNotFoundError:
            return messages

        import re as _re

        _FM_RE = _re.compile(r"^---\n([\s\S]*?)\n---\n?([\s\S]*)$")

        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            if len(messages) >= limit:
                break
            try:
                raw = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm_match = _FM_RE.match(raw)
            if not fm_match:
                continue
            meta_block = fm_match.group(1)
            body = fm_match.group(2).strip()
            meta: dict[str, str] = {}
            for line in meta_block.split("\n"):
                kv = _re.match(r"^(\w+):\s*(.*)$", line)
                if kv:
                    meta[kv.group(1)] = kv.group(2)

            to_field = meta.get("to", "")
            if name != "team" and to_field != name and to_field != "all":
                continue

            messages.append(
                {
                    "message_id": meta.get("message_id", ""),
                    "from": meta.get("from", "?"),
                    "to": to_field,
                    "subject": meta.get("subject", ""),
                    "sent_at": meta.get("sent_at", ""),
                    "body": body,
                    "file": entry.name,
                }
            )
        return messages

    # ------------------------------------------------------------------
    # Teardown（收尾关闭 + 释放所有资源租约）
    # ------------------------------------------------------------------

    async def teardown(self, force: bool = False) -> dict[str, list[str]]:
        """收尾：标记关闭 + 清理工作间 + 释放所有资源租约。

        在同一个 lock 内完成释放租约与标记 closed_at，消除两次 load 之间的竞态窗口。
        """
        async with self._lock:
            # 一次性 load，避免两次 load 之间的竞态
            state = await self._load()

            # 释放所有 mission 的活跃租约
            for mission in state.missions:
                self._release_all_mission_leases_in_state(state, mission.id)

            # 标记关闭（防止中途出错后 resume 复活）
            state.closed_at = _iso_now()
            state.resource_leases = {}
            self._runtime_lease_table.clear()
            await self._save(state)

            removed: list[str] = []
            kept: list[str] = []
            return {"removed": removed, "kept": kept}

    # ------------------------------------------------------------------
    # Resource Lease Table（运行时资源租约）
    # ------------------------------------------------------------------
    #
    # 设计依据（设计文档 3.6）：
    # 资源租约收窄为「仅 notebook / kernel 需要独占租约」。
    # 理由：IPython 内核命名空间是会话级共享的，两个 worker 同时在一个内核
    # 里跑代码会互相覆盖变量，这是 AIASys 特有的冲突热点。
    #
    # 其余资源（数据表 insert 语义、知识图谱 insert 语义、环境变量进程级恢复）
    # 靠 scope 声明互斥即可，不做重量级独占锁。

    async def acquire_resource_lease(
        self,
        mission_id: str,
        resource_type: str,
        resource_id: str,
        lease_key: str,
        exclusive: bool = True,
    ) -> None:
        """申请资源租约（运行时执行期调用）。

        Args:
            mission_id: 申请资源的 mission id。
            resource_type: 资源类型（notebook / dataset 等）。
            resource_id: 资源业务 ID。
            lease_key: 规范化的租约键（用于工具层快速比对）。
            exclusive: 是否独占。True 时资源被其他 mission 持有则拒绝；
               False 时仅登记不检测冲突（非独占资源靠 scope 声明互斥）。

        Raises:
            TeamError: exclusive=True 且资源被别的 mission 持有时抛出。
        """
        async with self._lock:
            state = await self._load()
            norm_key = _normalize_lease_key(resource_type, resource_id)
            existing = self._runtime_lease_table.get(norm_key)
            if exclusive and existing is not None and existing.get("mission_id") != mission_id:
                holder = existing.get("mission_id", "?")
                raise TeamError(
                    f"资源租约冲突：资源「{resource_type}:{resource_id}」"
                    f"当前被 mission「{holder}」持有，"
                    f"mission「{mission_id}」无法申请。"
                )
            entry = {
                "mission_id": mission_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "lease_key": lease_key,
                "acquired_at": _iso_now(),
                "exclusive": exclusive,
            }
            self._runtime_lease_table[norm_key] = entry
            state.resource_leases[norm_key] = entry
            await self._save(state)

    async def release_resource_lease(
        self,
        mission_id: str,
        resource_type: str,
        resource_id: str,
    ) -> None:
        """释放资源租约。"""
        async with self._lock:
            state = await self._load()
            norm_key = _normalize_lease_key(resource_type, resource_id)
            existing = self._runtime_lease_table.get(norm_key)
            if existing is not None and existing.get("mission_id") == mission_id:
                del self._runtime_lease_table[norm_key]
                state.resource_leases.pop(norm_key, None)
                await self._save(state)

    async def release_all_mission_leases(self, mission_id: str) -> None:
        """释放某 mission 持有的全部资源租约（teardown / 异常退出时调用）。"""
        async with self._lock:
            state = await self._load()
            self._release_all_mission_leases_in_state(state, mission_id)
            await self._save(state)

    def _release_all_mission_leases_in_state(self, state: "TeamState", mission_id: str) -> None:
        """在给定 state 对象上删除属于 mission_id 的全部租约（不 save）。"""
        keys_to_delete = [
            key
            for key, entry in self._runtime_lease_table.items()
            if entry.get("mission_id") == mission_id
        ]
        for key in keys_to_delete:
            del self._runtime_lease_table[key]
            state.resource_leases.pop(key, None)

    def get_lease_holder(self, resource_type: str, resource_id: str) -> str | None:
        """查询资源当前持有者 mission_id，无人持有时返回 None。

        只读取内存中的运行时表，进程重启后需先 load()。
        """
        norm_key = _normalize_lease_key(resource_type, resource_id)
        # _runtime_lease_table 是 dict[str, Any]，取两层都会退化成 Any，直接返回
        # 等于把「声明 str | None」变成空头承诺。逐层收窄，顺带防住表结构异常
        # （load() 从磁盘还原，字段类型不由本进程保证）。
        entry = self._runtime_lease_table.get(norm_key)
        if not isinstance(entry, dict):
            return None
        holder = entry.get("mission_id")
        return holder if isinstance(holder, str) else None

    async def resolve_mission_resource_leases(self, mission: TeamMission) -> list[str]:
        """将 mission 的 lease 声明解析为运行时租约键列表，并校验冲突。

        对声明为独占的资源（notebook / kernel）做运行时冲突检测。

        Returns:
            本 mission 成功申请的 lease_key 列表。

        Raises:
            TeamError: 独占资源被其他 mission 持有时抛出。
        """
        lease_keys: list[str] = []
        if not mission.lease:
            return lease_keys

        for resource_type, resource_ids in mission.lease.items():
            if not isinstance(resource_ids, list):
                continue
            for resource_id in resource_ids:
                resource_id = str(resource_id).strip()
                if not resource_id:
                    continue
                lease_key = _normalize_lease_key(resource_type, resource_id)
                # 从 _RESOURCE_LEASE_MAP 查该资源类型的 exclusive 标记
                # 找不到映射时默认 True（安全保守）
                is_exclusive = True
                for _tool_cfg in _RESOURCE_LEASE_MAP.values():
                    if _tool_cfg.get("resource_type") == resource_type:
                        is_exclusive = _tool_cfg.get("exclusive", True)
                        break
                try:
                    await self.acquire_resource_lease(
                        mission_id=mission.id,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        lease_key=lease_key,
                        exclusive=is_exclusive,
                    )
                    lease_keys.append(lease_key)
                except TeamError:
                    raise
        return lease_keys

    async def get_mission_lease_keys(self, mission_id: str) -> list[str]:
        """获取某 mission 当前持有的全部 lease_key。"""
        async with self._lock:
            state = await self._load()
            return [
                entry["lease_key"]
                for entry in state.resource_leases.values()
                if entry.get("mission_id") == mission_id
            ]


# ---------------------------------------------------------------------------
# Workspace Memory Sharding
# ---------------------------------------------------------------------------
# 设计依据：workspace_memory.md 全文覆盖写，多 agent 并行写会互相覆盖。
# 方案：每个 mission 写自己的分片文件，合并由主控执行。


def get_workspace_memory_shard_path(workspace_memory_dir: Path, mission_id: str) -> Path:
    """返回 mission 专属的 workspace_memory 分片文件路径。

    分片文件落在 {workspace_memory_dir}/shards/{mission_id}.md。
    主文件为 {workspace_memory_dir}/workspace_memory.md。

    Worker 只能写自己的分片，不能写其他 mission 的分片，也不能写主文件。
    """
    shards_dir = workspace_memory_dir / "shards"
    return shards_dir / f"{mission_id}.md"


def get_workspace_memory_main_path(workspace_memory_dir: Path) -> Path:
    """返回 workspace_memory 主文件路径。"""
    return workspace_memory_dir / "workspace_memory.md"


def merge_workspace_memory_shards(
    workspace_memory_dir: Path,
    mission_ids: list[str] | None = None,
) -> str:
    """合并 mission 分片到主文件（仅主控调用）。

    按分片文件最后修改时间排序，依次追加到主文件。
    跳过空分片。

    Args:
        workspace_memory_dir: workspace_memory 目录（包含主文件 + shards/）。
        mission_ids: 要合并的 mission id 列表；None 表示合并全部。

    Returns:
        合并后的主文件完整内容。
    """
    main_path = get_workspace_memory_main_path(workspace_memory_dir)
    shards_dir = workspace_memory_dir / "shards"

    # 读取现有主文件内容（如果存在）
    existing_content = ""
    if main_path.exists():
        existing_content = main_path.read_text(encoding="utf-8")

    # 收集分片内容
    shard_files: list[tuple[float, str]] = []  # (mtime, content)
    if shards_dir.exists():
        entries = sorted(shards_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            if mission_ids is not None and entry.stem not in mission_ids:
                continue
            try:
                content = entry.read_text(encoding="utf-8").strip()
                if content:
                    shard_files.append((entry.stat().st_mtime, content))
            except (OSError, UnicodeDecodeError):
                continue

    # 合并：主文件内容 + 各分片内容（分片间加分隔线）
    parts = [existing_content.rstrip()] if existing_content.strip() else []
    for _, content in shard_files:
        parts.append(f"\n\n<!-- shard start -->\n{content}\n<!-- shard end -->")

    merged = "\n".join(parts).strip() + "\n"
    return merged


# ---------------------------------------------------------------------------
# Per-Agent Write Allow Root 守卫（第二步 + 第三步扩展）
# ---------------------------------------------------------------------------

# 写工具名 → 参数中表示目标文件路径的参数名（文件类工具）
_WRITE_PATH_ARG: dict[str, str] = {
    "WriteFile": "path",
    "StrReplaceFile": "path",
    "CreateFile": "path",
    "EditNotebookFile": "notebook_path",
    # Shell：命令级路径无法可靠提取，守卫激活时整工具拒绝（见下方逻辑）
    "Shell": "__command__",
}

# 写工具名 → 参数中表示目标资源 ID 的参数名（非文件类资源工具）
# 从 _RESOURCE_LEASE_MAP 自动派生，保证两处映射一致。
_RESOURCE_LEASE_ARG: dict[str, str] = {
    tool_name: cfg["resource_id_arg"]
    for tool_name, cfg in _RESOURCE_LEASE_MAP.items()
    if cfg.get("resource_id_arg")
}

# 要求持有租约的工具名 → 参数中表示目标资源 ID 的参数名
# 删除类工具：必须该资源已在本 mission 的租约内才能操作（require_lease=True）
# 「要求持有租约」≠「独占租约」：前者是「你得先声明这个资源归你管」，
# 后者是「同时只能一个人持有」。设计文档已定租约收窄为仅 notebook/kernel 独占，
# 删除类工具仅要求声明归属，不阻止多个 mission 同时声明同一资源。
_REQUIRE_LEASE_ARG: dict[str, str] = {
    tool_name: cfg["resource_id_arg"]
    for tool_name, cfg in _RESOURCE_LEASE_MAP.items()
    if cfg.get("require_lease") and cfg.get("resource_id_arg")
}


def _resolve_path_for_guard(raw: str) -> str:
    """将原始路径字符串规范化为绝对路径。"""
    try:
        return os.path.realpath(raw)
    except (OSError, ValueError):
        return os.path.abspath(raw)


def _check_path_allowed(target: str, allow_roots: list[str], tool_name: str = "") -> str | None:
    """检查路径是否在允许的根列表内。返回 None 表示放行。"""
    resolved = _resolve_path_for_guard(target)
    for root in allow_roots:
        root_norm = _normalize_scope(root)
        target_norm = _normalize_scope(resolved)
        if root_norm == target_norm:
            return None
        root_slash = root_norm if root_norm.endswith("/") else root_norm + "/"
        if target_norm.startswith(root_slash):
            return None
    tool_label = f"工具「{tool_name}」" if tool_name else ""
    return (
        f"写范围守卫硬拒绝：{tool_label}试图写入「{resolved}」，"
        f"但当前 agent 仅允许写入以下范围：{allow_roots}。"
    )


def _check_lease_allowed(lease_key: str, allowed_leases: list[str]) -> str | None:
    """检查资源租约键是否在允许列表中。返回 None 表示放行。"""
    for allowed in allowed_leases:
        allowed_norm = _normalize_scope(allowed)
        lease_norm = _normalize_scope(lease_key)
        if allowed_norm == lease_norm:
            return None
        allowed_slash = allowed_norm if allowed_norm.endswith("/") else allowed_norm + "/"
        if lease_norm.startswith(allowed_slash):
            return None
    return (
        f"资源租约守卫拒绝：工具试图操作资源「{lease_key}」，"
        f"但当前 mission 的租约列表为：{allowed_leases}。"
    )


def check_write_guard(
    write_allow_root: list[str] | None,
    tool_name: str,
    arguments: dict[str, Any],
    resource_lease_keys: list[str] | None = None,
) -> str | None:
    """检查写操作是否被允许（第二步：路径守卫 + 第三步：资源租约守卫）。

    执行两层检查，任一不通过即拒绝：

    1. **路径守卫**（file-based 工具）：目标路径必须在 write_allow_root 内。
    2. **资源租约守卫**（non-file 工具）：资源的 lease_key 必须在
       resource_lease_keys 内。

    Args:
        write_allow_root: 允许写入的绝对路径前缀列表；None 表示不限制。
        tool_name: 被调用的工具名称。
        arguments: 工具参数（已解析为 dict）。
        resource_lease_keys: 本 mission 持有的资源租约键列表；None 表示不限制。

    Returns:
        错误消息字符串表示被拒绝；None 表示允许。
    """
    # 无任何限制配置 → 直接放行
    # write_allow_root=None 表示非 team 场景（不限制路径）
    # resource_lease_keys=None 表示非 team 场景（不限制资源）
    # 两者都是 None 时才真正无限制放行
    if write_allow_root is None and resource_lease_keys is None:
        return None

    # --- 要求持有租约的检查（删除类工具）---
    # 删除不可逆，必须该资源已在本 mission 的租约内才能操作。
    # 「要求持有租约」≠「独占租约」：前者是声明归属，后者是阻止多人同时持有。
    # resource_lease_keys=None 表示非 team 场景（不限制），空列表 [] 表示
    # team 场景但 mission 未声明该资源（应拒绝）。
    require_lease_arg = _REQUIRE_LEASE_ARG.get(tool_name)
    if require_lease_arg is not None and resource_lease_keys is not None:
        raw_resource_id = arguments.get(require_lease_arg)
        if raw_resource_id and str(raw_resource_id).strip():
            lease_cfg = _RESOURCE_LEASE_MAP.get(tool_name, {})
            resource_type = lease_cfg.get("resource_type", "")
            lease_key_full = _normalize_lease_key(resource_type, str(raw_resource_id).strip())
            lease_denial = _check_lease_allowed(lease_key_full, resource_lease_keys)
            if lease_denial:
                return (
                    f"资源租约守卫拒绝（删除类工具要求持有租约）：工具「{tool_name}」"
                    f"试图操作资源「{lease_key_full}」，但当前 mission 的租约列表为："
                    f"{resource_lease_keys}。请先在 mission.lease 中声明该资源。"
                )
        else:
            return f"资源租约守卫：工具「{tool_name}」缺少资源标识参数「{require_lease_arg}」。"

    # --- 第二步扩展：资源租约检查（非文件资源，非删除类）---
    # 只检查那些「需要租约但不需要强制声明」的工具。
    # 实际上：delete 类已在 require_lease 检查中处理；
    # 非 delete 类（CreateDataTable 等）不检查 lease，直接放行。
    # 保留此块结构以备后续扩展。

    # --- 第一步：文件路径检查（保留原有逻辑）---
    # write_allow_root=None 表示不限制路径（非 team 场景），跳过路径检查
    if write_allow_root is not None:
        path_arg = _WRITE_PATH_ARG.get(tool_name)
        if path_arg is None:
            # 工具既不在路径映射也不在资源租约映射 → 未注册工具，保守拒绝
            if tool_name not in _RESOURCE_LEASE_ARG:
                return (
                    f"写范围守卫：工具「{tool_name}」的写路径参数未注册，"
                    f"无法校验写入范围。请向 _WRITE_PATH_ARG 注册该工具。"
                )
            # 工具在资源租约映射中（如 CreateDataTable），跳过路径检查
            # 资源租约检查在上方已完成
            return None

        # Shell 工具：无法可靠提取命令级目标路径
        if path_arg == "__command__":
            return (
                "写范围守卫：Shell 工具的命令级路径无法应用范围守卫（已知缺口）。"
                "请使用专用的文件工具（WriteFile / StrReplaceFile / CreateFile）"
                "在允许范围内执行写操作，或让主控代理执行跨范围写。"
            )

        raw_path = arguments.get(path_arg)
        if not raw_path or not str(raw_path).strip():
            return f"写范围守卫：工具「{tool_name}」缺少目标路径参数「{path_arg}」。"

        path_denial = _check_path_allowed(str(raw_path).strip(), write_allow_root, tool_name)
        if path_denial:
            return path_denial

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dataclass_default(obj: Any) -> Any:
    """JSON 序列化 dataclass。"""
    if hasattr(obj, "__dataclass_fields__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _normalize_lease_files(lease: dict[str, Any]) -> list[str]:
    """从 lease 中提取并规范化允许的文件路径前缀。"""
    raw = lease.get("files", [])
    if isinstance(raw, list):
        return [_normalize_scope(str(p)) for p in raw]
    return []


def _is_path_allowed(path: str, allowed_prefixes: list[str]) -> bool:
    """检查 path 是否落在允许的前缀列表中。"""
    path_norm = _normalize_scope(path)
    for prefix in allowed_prefixes:
        if _scope_covers_path(prefix, path_norm):
            return True
    return False


def _compute_artifacts_fingerprint(artifacts: list[dict[str, Any]]) -> str:
    """计算产出物清单的指纹。"""
    sorted_arts = sorted(artifacts, key=lambda a: a.get("path", ""))
    parts = []
    for art in sorted_arts:
        parts.append(f"{art.get('path', '')}:{art.get('size', '')}:{art.get('hash', '')}")
    return "|".join(parts)


def _find_scope_conflicts(
    missions: list[TeamMission],
    merged: TeamMission,
    artifacts: list[dict[str, Any]] | None,
) -> list[str]:
    """找出 scope 与本次产出物重叠的其他未 merged build 任务。"""
    if merged.kind != "build" or artifacts is None:
        return []
    changed_paths = [a.get("path", "") for a in artifacts]
    return [
        m.id
        for m in missions
        if m.kind == "build"
        and m.status != "merged"
        and m.id != merged.id
        and any(_scope_covers_path(s, p) for s in merged.scope for p in changed_paths if p)
    ]
