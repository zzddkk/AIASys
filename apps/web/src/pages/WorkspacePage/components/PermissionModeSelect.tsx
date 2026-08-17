/**
 * PermissionModeSelect - 权限档位 chip
 *
 * 参照 deepseek-harness 的 PermissionSelect（ui-conversation/skeleton/PermissionSelect.tsx）：
 * 药丸形 chip（图标 + 档位名 + chevron）放在输入框工具栏，点击弹出四档下拉；
 * 切到「全权」先过风险确认门（勾选「我已了解风险」才能启用）。
 *
 * 数据自包含：按 sessionId 拉取当前档位，切换走 POST，
 * 成功后更新本地状态（档位只能由这里改，本地状态即权威）。
 */
import { useCallback, useEffect, useState } from "react";
import { Check, ChevronDown, ShieldAlert, ShieldCheck } from "lucide-react";

import {
  AUTHORIZATION_MODES,
  authorizationModeMeta,
  getSessionAuthorizationMode,
  needsRiskConfirmation,
  updateSessionAuthorizationMode,
  type AuthorizationMode,
} from "@/lib/api/authorizationMode";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface PermissionModeSelectProps {
  sessionId?: string;
  /** 切换失败时的错误提示回调（409 运行中冲突等） */
  onError?: (message: string) => void;
}

export function PermissionModeSelect({
  sessionId,
  onError,
}: PermissionModeSelectProps) {
  const [mode, setMode] = useState<AuthorizationMode>("full_auto");
  const [pendingRiskTarget, setPendingRiskTarget] =
    useState<AuthorizationMode | null>(null);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    getSessionAuthorizationMode(sessionId)
      .then((current) => {
        if (!cancelled) setMode(current);
      })
      .catch(() => {
        // 拉取失败保持默认档展示，不打扰用户；下次切换会话重试
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const applyMode = useCallback(
    async (target: AuthorizationMode) => {
      if (!sessionId || switching) return;
      setSwitching(true);
      try {
        await updateSessionAuthorizationMode(sessionId, target);
        setMode(target);
      } catch (error) {
        onError?.(
          error instanceof Error ? error.message : "切换权限档位失败",
        );
      } finally {
        setSwitching(false);
      }
    },
    [sessionId, switching, onError],
  );

  const handleSelect = useCallback(
    (target: AuthorizationMode) => {
      if (target === mode) return;
      if (needsRiskConfirmation(target)) {
        setRiskAcknowledged(false);
        setPendingRiskTarget(target);
        return;
      }
      void applyMode(target);
    },
    [mode, applyMode],
  );

  const meta = authorizationModeMeta(mode);
  const Icon = meta.requiresRiskConfirmation ? ShieldAlert : ShieldCheck;

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`权限模式，当前：${meta.label}`}
            className="flex h-7 items-center gap-1.5 rounded-full border border-border bg-background px-2.5 text-micro font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Icon size={12} className="flex-shrink-0" />
            <span>{meta.label}</span>
            <ChevronDown size={11} className="flex-shrink-0 opacity-60" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64">
          {AUTHORIZATION_MODES.map((item) => (
            <DropdownMenuItem
              key={item.id}
              onSelect={() => handleSelect(item.id)}
              className="flex items-start gap-2 py-2"
            >
              <span className="mt-0.5 w-4 flex-shrink-0">
                {item.id === mode ? <Check size={13} /> : null}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-xs font-medium">{item.label}</span>
                <span className="text-nano leading-4 text-muted-foreground">
                  {item.description}
                </span>
              </span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog
        open={pendingRiskTarget !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRiskTarget(null);
        }}
      >
        <DialogContent size="xs">
          <DialogHeader>
            <DialogTitle>确认启用全权模式？</DialogTitle>
            <DialogDescription>
              启用全权模式后，Agent 将不再询问，直接执行文件修改与命令。
              仅建议在你信任当前任务时使用。
            </DialogDescription>
          </DialogHeader>
          <label className="flex cursor-pointer items-center gap-2 text-xs text-foreground">
            <input
              type="checkbox"
              checked={riskAcknowledged}
              onChange={(e) => setRiskAcknowledged(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            我已了解风险，并愿意继续
          </label>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPendingRiskTarget(null)}
            >
              取消
            </Button>
            <Button
              size="sm"
              disabled={!riskAcknowledged}
              onClick={() => {
                const target = pendingRiskTarget;
                setPendingRiskTarget(null);
                if (target) void applyMode(target);
              }}
            >
              启用全权模式
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default PermissionModeSelect;
