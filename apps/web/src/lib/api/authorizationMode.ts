/**
 * 会话授权模式（authorization_mode）的前端 API 与元数据。
 *
 * 设计文档：AIASys-product-design/交互设计/permission-mode-management.md
 * 后端：POST /api/sessions/{userId}/{sessionId}/authorization-mode（next_run_only），
 * 当前值随 GET /api/sessions/status/{sessionId} 下发。
 */
import { apiRequest } from "@/lib/api/httpClient";
import { getCurrentUserId } from "@/config/api";

export type AuthorizationMode = "manual" | "smart" | "auto" | "full_auto";

/** 后端默认档：session metadata 未设置时按 full_auto 处理（与 mixins/session.py 一致） */
export const DEFAULT_AUTHORIZATION_MODE: AuthorizationMode = "full_auto";

export interface AuthorizationModeMeta {
  id: AuthorizationMode;
  label: string;
  description: string;
  /** 是否需要风险确认门（harness RiskConfirmation 式） */
  requiresRiskConfirmation: boolean;
}

export const AUTHORIZATION_MODES: readonly AuthorizationModeMeta[] = [
  {
    id: "manual",
    label: "手动确认",
    description: "每个写操作和执行命令都需要你确认",
    requiresRiskConfirmation: false,
  },
  {
    id: "smart",
    label: "智能",
    description: "安全操作自动放行，风险操作询问",
    requiresRiskConfirmation: false,
  },
  {
    id: "auto",
    label: "自动",
    description: "文件读写自动放行，执行命令仍会询问",
    requiresRiskConfirmation: false,
  },
  {
    id: "full_auto",
    label: "全权",
    description: "所有操作自动执行，不再询问",
    requiresRiskConfirmation: true,
  },
] as const;

export function authorizationModeMeta(
  mode: string | null | undefined,
): AuthorizationModeMeta {
  const found = AUTHORIZATION_MODES.find((m) => m.id === mode);
  return (
    found ??
    AUTHORIZATION_MODES.find((m) => m.id === DEFAULT_AUTHORIZATION_MODE)!
  );
}

/** 切换目标档位是否需要先过风险确认门 */
export function needsRiskConfirmation(target: AuthorizationMode): boolean {
  return authorizationModeMeta(target).requiresRiskConfirmation;
}

interface SessionStatusResponse {
  authorization_mode?: string | null;
}

export async function getSessionAuthorizationMode(
  sessionId: string,
): Promise<AuthorizationMode> {
  const data = await apiRequest<SessionStatusResponse>(
    `/api/sessions/status/${sessionId}`,
  );
  return authorizationModeMeta(data.authorization_mode).id;
}

export async function updateSessionAuthorizationMode(
  sessionId: string,
  mode: AuthorizationMode,
): Promise<void> {
  await apiRequest(`/api/sessions/${getCurrentUserId()}/${sessionId}/authorization-mode`, {
    method: "POST",
    body: { authorization_mode: mode },
  });
}
