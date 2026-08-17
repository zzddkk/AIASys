import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Archive,
  History,
  Loader2,
  MoreHorizontal,
  Package,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  type WorkspaceSnapshot,
  type ApplySnapshotResponse,
  applyWorkspaceSnapshot,
  createWorkspaceSnapshot,
  deleteWorkspaceSnapshot,
  listWorkspaceSnapshots,
} from "@/lib/api/workspaceSnapshots";

interface SnapshotsPanelProps {
  workspaceId: string | null;
}


function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function SnapshotsPanel({ workspaceId }: SnapshotsPanelProps) {
  const [snapshots, setSnapshots] = useState<WorkspaceSnapshot[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [selectedSnapshot, setSelectedSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [applyConfirmOpen, setApplyConfirmOpen] = useState(false);
  const [applyMode, setApplyMode] = useState<"soft" | "hard">("soft");
  const [isApplying, setIsApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<ApplySnapshotResponse | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const requestIdRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadSnapshots = useCallback(async () => {
    if (!workspaceId) return;
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setIsLoading(true);
    setError(null);
    try {
      const response = await listWorkspaceSnapshots(workspaceId, { limit: 50 });
      if (requestIdRef.current !== requestId) return;
      setSnapshots(response.snapshots);
    } catch (err) {
      if (requestIdRef.current !== requestId) return;
      setSnapshots([]);
      setError(err instanceof Error ? err.message : "加载版本列表失败");
    } finally {
      if (requestIdRef.current === requestId) {
        setIsLoading(false);
      }
    }
  }, [workspaceId]);

  useEffect(() => {
    void loadSnapshots();
  }, [loadSnapshots]);

  const handleCreate = useCallback(async () => {
    if (!workspaceId || !title.trim()) return;
    setIsCreating(true);
    try {
      await createWorkspaceSnapshot(workspaceId, {
        title: title.trim(),
        description: description.trim() || null,
      });
      setCreateOpen(false);
      setTitle("");
      setDescription("");
      await loadSnapshots();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建版本失败");
    } finally {
      setIsCreating(false);
    }
  }, [workspaceId, title, description, loadSnapshots]);

  const handleApply = useCallback(async () => {
    if (!workspaceId || !selectedSnapshot) return;
    setIsApplying(true);
    try {
      const result = await applyWorkspaceSnapshot(workspaceId, selectedSnapshot.id, {
        mode: applyMode,
      });
      if (mountedRef.current) {
        setApplyResult(result);
      }
      await loadSnapshots();
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : "切换版本失败");
      }
    } finally {
      if (mountedRef.current) {
        setIsApplying(false);
      }
      setApplyConfirmOpen(false);
    }
  }, [workspaceId, selectedSnapshot, applyMode, loadSnapshots]);

  const handleDelete = useCallback(async () => {
    if (!workspaceId || !selectedSnapshot) return;
    setIsDeleting(true);
    try {
      await deleteWorkspaceSnapshot(workspaceId, selectedSnapshot.id);
      if (mountedRef.current) {
        setDeleteConfirmOpen(false);
      }
      await loadSnapshots();
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : "删除版本失败");
      }
    } finally {
      if (mountedRef.current) {
        setIsDeleting(false);
      }
    }
  }, [workspaceId, selectedSnapshot, loadSnapshots]);

  const openApply = useCallback(
    (snapshot: WorkspaceSnapshot, mode: "soft" | "hard") => {
      setSelectedSnapshot(snapshot);
      setApplyMode(mode);
      setApplyConfirmOpen(true);
    },
    [],
  );

  const openDelete = useCallback((snapshot: WorkspaceSnapshot) => {
    setSelectedSnapshot(snapshot);
    setDeleteConfirmOpen(true);
  }, []);

  const manualSnapshots = useMemo(
    () => snapshots.filter((s) => s.source === "manual"),
    [snapshots],
  );
  const autoSnapshots = useMemo(
    () => snapshots.filter((s) => s.source !== "manual"),
    [snapshots],
  );

  const canLoad = Boolean(workspaceId);

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex h-11 items-center justify-between border-b border-border px-3">
        <span className="text-xs font-medium text-muted-foreground">
          {isLoading ? "加载中" : `${snapshots.length} 个版本`}
        </span>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={() => void loadSnapshots()}
            disabled={!canLoad || isLoading}
            aria-label="刷新版本列表"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isLoading && "animate-spin")} />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={() => setCreateOpen(true)}
            disabled={!canLoad}
            aria-label="保存当前版本"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        {!canLoad ? (
          <div className="flex h-32 items-center justify-center px-6 text-sm text-muted-foreground">
            请先打开一个工作区。
          </div>
        ) : error ? (
          <div className="px-3 py-4 text-xs text-error">{error}</div>
        ) : snapshots.length === 0 && !isLoading ? (
          <div className="flex h-48 flex-col items-center justify-center gap-3 px-6 text-center text-xs text-muted-foreground">
            <Archive className="h-8 w-8 opacity-40" />
            <p>暂无保存的版本。</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setCreateOpen(true)}
            >
              <Save className="mr-1 h-3.5 w-3.5" />
              保存当前版本
            </Button>
          </div>
        ) : (
          <div className="p-2">
            {manualSnapshots.length > 0 && (
              <div className="mb-3">
                <div className="px-2 py-1.5 text-micro font-medium text-muted-foreground">
                  手动版本
                </div>
                {manualSnapshots.map((snapshot) => (
                  <SnapshotListItem
                    key={snapshot.id}
                    snapshot={snapshot}
                    onApplySoft={() => openApply(snapshot, "soft")}
                    onApplyHard={() => openApply(snapshot, "hard")}
                    onDelete={() => openDelete(snapshot)}
                  />
                ))}
              </div>
            )}
            {autoSnapshots.length > 0 && (
              <div>
                <div className="px-2 py-1.5 text-micro font-medium text-muted-foreground">
                  自动版本
                </div>
                {autoSnapshots.map((snapshot) => (
                  <SnapshotListItem
                    key={snapshot.id}
                    snapshot={snapshot}
                    onApplySoft={() => openApply(snapshot, "soft")}
                    onApplyHard={() => openApply(snapshot, "hard")}
                    onDelete={() => openDelete(snapshot)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </ScrollArea>

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent size="sm">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <Save className="h-4 w-4" />
              保存当前版本
            </DialogTitle>
            <DialogDescription className="text-xs">
              保存后可以随时切换回这个状态。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 py-2">
            <div>
              <label className="mb-1 block text-xs font-medium">版本名称</label>
              <Input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="例如：运行分析前"
                disabled={isCreating}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium">备注（可选）</label>
              <Textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="简要说明这个版本的内容…"
                rows={3}
                disabled={isCreating}
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setCreateOpen(false)}
              disabled={isCreating}
            >
              取消
            </Button>
            <Button
              type="button"
              onClick={() => void handleCreate()}
              disabled={!title.trim() || isCreating}
            >
              {isCreating && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Apply confirm */}
      <AlertDialog open={applyConfirmOpen} onOpenChange={setApplyConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              {applyMode === "hard" && (
                <AlertTriangle className="h-4 w-4 text-destructive" />
              )}
              {applyMode === "soft" ? "恢复文件" : "完全恢复"}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-xs">
              {applyMode === "soft"
                ? "将把快照中存在的文件恢复到该版本状态。快照之后新增的文件不会被删除。当前状态会先自动备份。"
                : "将完全对齐到该版本状态：快照中不存在的文件会被删除，存在的文件会被恢复。当前状态会先自动备份。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void handleApply()}
              disabled={isApplying}
              className={
                applyMode === "hard"
                  ? "bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  : ""
              }
            >
              {isApplying && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              {applyMode === "soft" ? "确认恢复" : "确认完全恢复"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Apply result */}
      {applyResult && (
        <Dialog
          open={Boolean(applyResult)}
          onOpenChange={(open) => {
            if (!open) setApplyResult(null);
          }}
        >
          <DialogContent size="sm">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 text-base">
                <Package className="h-4 w-4" />
                版本切换完成
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-1 py-2 text-xs">
              <p>
                已恢复 <span className="font-medium">{applyResult.restored_files.length}</span> 个文件
              </p>
              {applyResult.deleted_files.length > 0 && (
                <p>
                  已删除 <span className="font-medium text-error">{applyResult.deleted_files.length}</span> 个文件
                </p>
              )}
              {applyResult.skipped_files.length > 0 && (
                <p>
                  跳过 <span className="font-medium">{applyResult.skipped_files.length}</span> 个文件（历史内容缺失）
                </p>
              )}
              <p className="pt-2 text-muted-foreground">
                当前状态已备份为：{applyResult.backup_snapshot_id}
              </p>
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => setApplyResult(null)}>
                确定
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {/* Delete confirm */}
      <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除版本</AlertDialogTitle>
            <AlertDialogDescription className="text-xs">
              删除后无法恢复，但不会影响文件历史内容。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void handleDelete()}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

interface SnapshotListItemProps {
  snapshot: WorkspaceSnapshot;
  onApplySoft: () => void;
  onApplyHard: () => void;
  onDelete: () => void;
}

function SnapshotListItem({
  snapshot,
  onApplySoft,
  onApplyHard,
  onDelete,
}: SnapshotListItemProps) {
  return (
    <div
      className={cn(
        "mb-1 flex items-center justify-between gap-2 rounded-md px-2.5 py-2",
        "text-foreground hover:bg-muted",
      )}
    >
      <span className="truncate text-xs font-medium">{snapshot.title}</span>
      <span className="text-micro text-muted-foreground">
        {formatTime(snapshot.created_at)} · {snapshot.file_count} 文件
      </span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon-sm">
            <MoreHorizontal className="h-3.5 w-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="text-xs">
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuItem onClick={() => onApplySoft()}>
                <RotateCcw className="mr-2 h-3.5 w-3.5" />
                恢复文件
              </DropdownMenuItem>
            </TooltipTrigger>
            <TooltipContent>
              仅恢复快照中的文件，保留之后新增的内容
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuItem onClick={() => onApplyHard()}>
                <History className="mr-2 h-3.5 w-3.5" />
                完全恢复
              </DropdownMenuItem>
            </TooltipTrigger>
            <TooltipContent>
              完全恢复到该版本，会删除快照后新增的文件
            </TooltipContent>
          </Tooltip>
          <DropdownMenuItem onClick={onDelete} className="text-error focus:text-error">
            <Trash2 className="mr-2 h-3.5 w-3.5" />
            删除
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
