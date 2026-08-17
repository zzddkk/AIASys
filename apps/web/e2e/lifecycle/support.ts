import { mkdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";

import {
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

export const DEFAULT_ENV_ID = "python-data-analysis";

export interface LifecycleUserMeta {
  userId: string;
  email: string;
  password: string;
}

export interface RuntimeEnvironmentSummary {
  id: string;
  name: string;
}

export interface WorkspaceMeta {
  workspaceId: string;
  currentConversationId: string;
  currentSessionId: string;
}

// 工作区根目录必须与运行中的后端一致，不能猜。
//
// 原先写的是 path.resolve(process.cwd(), "../backend/data/workspaces")，
// 而后端 app/core/config.py 的实际解析是：
//   WORKSPACE_DIR = AIASYS_RUNTIME_WORKSPACES_DIR 环境变量，否则 ~/AIASys/workspaces
// 两者并不相同（2026-08-11 实测：dev 服务器实际用 C:\Users\ke\AIASys\workspaces，
// 而 apps/backend/data/workspaces 里堆着 76MB、12 个用户目录的历史残留）。
//
// 猜错的后果是静默的：seedWorkspaceFile 把文件写进一个后端根本不读的目录，
// 于是「文件已存在」的前提不成立，测试失败的表象却是断言不符，很难往路径上想。
// 所以这里镜像后端的解析顺序，并在解析结果不存在时立刻给出可读的失败原因。
const BACKEND_WORKSPACES_ROOT = path.resolve(
  process.env.AIASYS_RUNTIME_WORKSPACES_DIR ||
    path.join(os.homedir(), "AIASys", "workspaces"),
);

let workspacesRootChecked = false;

function assertWorkspacesRootExists(): void {
  if (workspacesRootChecked) {
    return;
  }
  workspacesRootChecked = true;
  if (!existsSync(BACKEND_WORKSPACES_ROOT)) {
    throw new Error(
      [
        `工作区根目录不存在：${BACKEND_WORKSPACES_ROOT}`,
        "它必须与运行中的后端一致（后端取 AIASYS_RUNTIME_WORKSPACES_DIR，缺省 ~/AIASys/workspaces）。",
        "若后端用了别的目录，请把同一个值通过 AIASYS_RUNTIME_WORKSPACES_DIR 传给测试进程。",
      ].join("\n"),
    );
  }
}

export function getWorkspaceRoot(userId: string, workspaceId: string): string {
  assertWorkspacesRootExists();
  return path.join(BACKEND_WORKSPACES_ROOT, userId, workspaceId);
}

/**
 * 拼出工作区内某个相对路径的绝对路径，分隔符与后端返回的一致。
 *
 * 不要写 `${getWorkspaceRoot(...)}/${relativePath}`：getWorkspaceRoot 走 path.join，
 * 在 Windows 上产出反斜杠，再用 `/` 拼接就得到
 *   C:\Users\ke\AIASys\workspaces\local_default\<id>/browser-regression/x
 * 这种混用形态，而后端 resources/tree 的 absolute_path 是纯反斜杠的
 *   C:\Users\ke\AIASys\workspaces\local_default\<id>\browser-regression\x
 * 于是「复制绝对路径」这类断言在 Windows 上必然失配（2026-08-11 实测）。
 */
export function getWorkspaceAbsolutePath(
  userId: string,
  workspaceId: string,
  relativePath: string,
): string {
  const segments = relativePath
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean);
  return path.join(getWorkspaceRoot(userId, workspaceId), ...segments);
}

export async function registerLifecycleUser(
  api: APIRequestContext,
): Promise<LifecycleUserMeta> {
  const response = await api.get("/api/auth/session");

  const body = (await response.json()) as {
    user?: { id?: string; email?: string };
    detail?: string;
    error?: string;
  };

  if (!response.ok() || !body.user?.id) {
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : body.detail
          ? JSON.stringify(body.detail)
          : body.error || response.status();
    throw new Error(
      `Failed to register lifecycle user: ${detail}`,
    );
  }

  const meta: LifecycleUserMeta = {
    userId: body.user.id,
    email: body.user.email || "local_default@localhost",
    password: "",
  };

  return meta;
}

export async function listEnvironments(
  api: APIRequestContext,
): Promise<RuntimeEnvironmentSummary[]> {
  const response = await api.get("/api/runtime-envs");
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as RuntimeEnvironmentSummary[];
}

export async function createSession(
  api: APIRequestContext,
  options: {
    title: string;
    envId?: string;
    sandboxMode?: "docker" | "local";
  },
): Promise<string> {
  const sessionId = randomUUID();
  const response = await api.post("/api/sessions/create", {
    data: {
      session_id: sessionId,
      title: options.title,
      env_id: options.envId || DEFAULT_ENV_ID,
      sandbox_mode: options.sandboxMode || "local",
    },
  });

  expect(response.ok()).toBeTruthy();
  return sessionId;
}

export async function createWorkspace(
  api: APIRequestContext,
  options: {
    title: string;
    workspaceId?: string;
    mode?: "analysis" | "research";
    initialConversationId?: string;
    initialConversationTitle?: string;
  },
): Promise<WorkspaceMeta> {
  const workspaceId = options.workspaceId || randomUUID();
  const initialConversationId = options.initialConversationId || randomUUID();
  const response = await api.post("/api/workspaces", {
    data: {
      workspace_id: workspaceId,
      title: options.title,
      mode: options.mode || "analysis",
      initial_conversation_id: initialConversationId,
      initial_conversation_title: options.initialConversationTitle || "新会话",
    },
  });

  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as {
    workspace_id: string;
    current_conversation?: {
      conversation_id?: string;
      session_id?: string;
    };
  };

  return {
    workspaceId: body.workspace_id,
    currentConversationId:
      body.current_conversation?.conversation_id || initialConversationId,
    currentSessionId:
      body.current_conversation?.session_id || initialConversationId,
  };
}

export async function createWorkspaceConversation(
  api: APIRequestContext,
  workspaceId: string,
  options: {
    title: string;
    conversationId?: string;
    mode?: "analysis" | "research";
    branchedFromConversationId?: string;
  },
): Promise<string> {
  const conversationId = options.conversationId || randomUUID();
  const response = await api.post(`/api/workspaces/${workspaceId}/conversations`, {
    data: {
      conversation_id: conversationId,
      title: options.title,
      mode: options.mode || "analysis",
      branched_from_conversation_id: options.branchedFromConversationId,
    },
  });

  expect(response.ok()).toBeTruthy();
  return conversationId;
}

export async function addSessionMessage(
  api: APIRequestContext,
  userId: string,
  sessionId: string,
  content: string,
  role: "user" | "assistant" = "user",
): Promise<void> {
  const response = await api.post(`/api/sessions/${userId}/${sessionId}/messages`, {
    data: {
      role,
      content,
    },
  });

  expect(response.ok()).toBeTruthy();
}

export async function deleteSession(
  api: APIRequestContext,
  userId: string,
  sessionId: string | null | undefined,
): Promise<void> {
  if (!sessionId) {
    return;
  }

  try {
    const deletePromise = api
      .delete(`/api/sessions/${userId}/${sessionId}`, {
        timeout: 5_000,
      })
      .catch((error) => {
        console.warn(`[Lifecycle] 删除会话失败或超时: ${sessionId}`, error);
        return null;
      });

    const response = await Promise.race<
      Awaited<ReturnType<typeof api.delete>> | null
    >([
      deletePromise,
      new Promise<null>((resolve) => {
        setTimeout(() => resolve(null), 5_000);
      }),
    ]);

    if (!response) {
      console.warn(`[Lifecycle] 删除会话超时，跳过清理: ${sessionId}`);
      return;
    }

    if (!response.ok() && response.status() !== 404) {
      console.warn(
        `[Lifecycle] 删除会话失败: ${sessionId} status=${response.status()}`,
      );
    }
  } catch (error) {
    console.warn(`[Lifecycle] 删除会话失败或超时: ${sessionId}`, error);
  }
}

export async function deleteWorkspace(
  api: APIRequestContext,
  workspaceId: string | null | undefined,
): Promise<void> {
  if (!workspaceId) {
    return;
  }

  try {
    const response = await api.delete(`/api/workspaces/${workspaceId}`, {
      timeout: 10_000,
    });

    if (!response.ok() && response.status() !== 404) {
      console.warn(
        `[Lifecycle] 删除工作区失败: ${workspaceId} status=${response.status()}`,
      );
    }
  } catch (error) {
    console.warn(`[Lifecycle] 删除工作区失败或超时: ${workspaceId}`, error);
  }
}

export async function seedWorkspaceFile(options: {
  userId: string;
  workspaceId: string;
  filePath: string;
  content: string;
}): Promise<void> {
  const targetPath = path.join(
    BACKEND_WORKSPACES_ROOT,
    options.userId,
    options.workspaceId,
    "workspace",
    options.filePath,
  );
  await mkdir(path.dirname(targetPath), { recursive: true });
  await writeFile(targetPath, options.content, "utf-8");
}

export async function setWorkspaceCurrentConversation(options: {
  userId: string;
  workspaceId: string;
  conversationId: string;
}): Promise<void> {
  const metaPath = path.join(
    BACKEND_WORKSPACES_ROOT,
    options.userId,
    options.workspaceId,
    ".aiasys/workspace",
    "workspace.json",
  );
  const payload = JSON.parse(await readFile(metaPath, "utf-8")) as Record<string, unknown>;
  payload.current_conversation_id = options.conversationId;
  payload.updated_at = new Date().toISOString();
  await writeFile(metaPath, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
}

export async function createPendingAskUser(
  api: APIRequestContext,
  sessionId: string,
  overrides?: Partial<{
    title: string;
    message: string;
    timeout: number;
  }>,
): Promise<string> {
  const response = await api.post("/api/ask-user/dev/create-pending", {
    data: {
      session_id: sessionId,
      title: overrides?.title || "键盘保护测试",
      message: overrides?.message || "请确认 AskUser 打开时页面级快捷键不会误触发。",
      timeout: overrides?.timeout || 120,
    },
  });

  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { request_id: string };
  return body.request_id;
}

export async function resolveAskUser(
  api: APIRequestContext,
  requestId: string,
  approved = false,
): Promise<void> {
  const response = await api.post("/api/ask-user/resolve", {
    data: {
      request_id: requestId,
      approved,
    },
  });

  if (!response.ok() && response.status() !== 404) {
    throw new Error(`Failed to resolve AskUser request ${requestId}: ${response.status()}`);
  }
}

export function extractSessionIdFromUrl(url: string): string | null {
  const analysisUrl = new URL(url, "http://localhost");
  const sessionIdFromQuery = analysisUrl.searchParams.get("session_id");
  if (sessionIdFromQuery) {
    return sessionIdFromQuery;
  }
  const match = analysisUrl.pathname.match(/\/analysis\/([^/?#]+)/);
  return match?.[1] || null;
}

export function extractWorkspaceIdFromUrl(url: string): string | null {
  const analysisUrl = new URL(url, "http://localhost");
  return analysisUrl.searchParams.get("workspace_id");
}

export function extractConversationIdFromUrl(url: string): string | null {
  const analysisUrl = new URL(url, "http://localhost");
  return (
    analysisUrl.searchParams.get("conversation_id") ||
    analysisUrl.searchParams.get("session_id")
  );
}

export function buildAnalysisUrl(options: {
  workspaceId?: string | null;
  conversationId?: string | null;
  sessionId?: string | null;
}): string {
  const search = new URLSearchParams();
  if (options.workspaceId) {
    search.set("workspace_id", options.workspaceId);
  }
  if (options.conversationId) {
    search.set("conversation_id", options.conversationId);
  }
  if (options.sessionId) {
    search.set("session_id", options.sessionId);
  }
  const query = search.toString();
  return query ? `/analysis?${query}` : "/analysis";
}

/**
 * 打开并返回当前工作区的资产面板。
 *
 * 关键前提：这个面板在 /analysis 页面默认就是渲染的，不需要点任何按钮。所以先判
 * panel 是否已可见、可见就直接返回——下面那段切换逻辑在正常路径下不会执行。
 *
 * 之所以还留着切换分支：面板可能因为上一步操作切到了全局资源视图。但它必须放在
 * panel 预检之后，不能反过来先判切换按钮。曾有三份 spec 缺这层预检，直接去判
 * button[aria-label='文件']，按钮不在就走 getByRole("button", { name: "资产" })
 * 兜底，而「资产」这个可访问名早已不存在，于是在它上面等满超时，报错指向兜底定位器，
 * 完全看不出真实原因是「面板本来就开着，压根不用点」。
 *
 * 这类 if (visible) A else B 的兜底写法要慎用：A 失效时它不会报错，而是走一条同样
 * 失效的 B，把「定位器过时」的失败伪装成「等待超时」。
 *
 * 活动栏按钮的可访问名来自 activityBarUtils.tsx 的 `label`，经 ActivityBar.tsx 的
 * `aria-label={item.label}` 渲染，现值是「当前工作区」；「文件」是旧名。两个都试。
 *
 * 为什么必须收敛到本函数：2026-08-11 全量跑出 21 条失败，其中一大片的直接原因就是
 * 7 份 spec 各自内联了一份只认旧名「文件」的版本，面板压根没打开，后续断言级联失败。
 *
 * tree-surface 断言收在这里是安全的：它在 WorkspaceAssetPanel.tsx:1953 无条件渲染，
 * 只要 panel 在就在，不存在某些场景没有的情况。
 */
export async function openWorkspaceFilesPanel(page: Page) {
  await expect(page.locator("textarea")).toBeVisible();
  const panel = page.locator('[data-testid="workspace-artifacts-panel"]');
  if (!(await panel.isVisible())) {
    const fileTab = page
      .locator("button[aria-label='当前工作区'], button[aria-label='文件']")
      .first();
    if ((await fileTab.count()) > 0 && (await fileTab.isVisible())) {
      await fileTab.click();
    } else {
      await page
        .getByRole("button", { name: "当前工作区", exact: true })
        .or(page.getByRole("button", { name: "文件", exact: true }))
        .first()
        .click();
    }
  }
  await expect(panel).toBeVisible();
  await expect(
    panel.getByTestId("workspace-artifacts-tree-surface"),
  ).toBeVisible();
  return panel;
}

/**
 * 打开并返回全局资源面板。
 *
 * 两个可访问名都要试。这段写法取自 canvas-preview-ctrl-wheel.spec.ts 的本地版本，
 * 它是各份拷贝里唯一跟上了改名的：按钮的可访问名现在是「全局工作区」，「全局资源」
 * 是旧名。只写其中一个就会在另一种渲染路径下等满超时。
 *
 * 提取公共函数时不能随手挑一份拷贝当基准——各份的新旧程度不同，挑错就是用退化的
 * 版本覆盖已修好的版本。本函数第一版只写了「全局资源」，实测直接把 canvas 的一条
 * 通过用例改成了 180s 超时。要取各份的并集。
 */
export async function openGlobalResourcesPanel(page: Page) {
  await expect(page.locator("textarea")).toBeVisible();
  const panel = page.locator(
    '[data-testid="workspace-global-resources-panel"]',
  );
  if (!(await panel.isVisible())) {
    const globalTab = page
      .locator("button[aria-label='全局工作区'], button[aria-label='全局资源']")
      .first();
    if ((await globalTab.count()) > 0 && (await globalTab.isVisible())) {
      await globalTab.click();
    } else {
      await page
        .getByRole("button", { name: "全局工作区", exact: true })
        .or(page.getByRole("button", { name: "全局资源", exact: true }))
        .first()
        .click();
    }
  }
  await expect(panel).toBeVisible();
  await expect(
    panel.getByTestId("workspace-global-resources-tree-surface"),
  ).toBeVisible();
  return panel;
}
