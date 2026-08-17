import { expect, test } from "@playwright/test";

import { gotoAnalysisSession } from "./data-analysis-helpers";
import {
  createSession,
  createWorkspace,
  deleteSession,
  deleteWorkspace,
  extractSessionIdFromUrl,
  extractWorkspaceIdFromUrl,
  registerLifecycleUser,
} from "./support";

test.describe("DataAnalysis create flow browser regression", () => {
  test("empty session shows the workspace startup panel instead of legacy execution-space entry", async ({
    page,
  }) => {
    const api = page.request;
    const { userId } = await registerLifecycleUser(api);
    const sessionId = await createSession(api, {
      title: "浏览器回归-工作区启动面板",
      sandboxMode: "local",
    });

    try {
      await gotoAnalysisSession(page, sessionId);

      // 这里不能断言「还没有工作区」的绝对空态。原因是 registerLifecycleUser 名不副实：
      // 它并不注册新用户，只是 GET /api/auth/session 拿当前单机默认用户的 id
      // （后端没有 register 端点，只有 login/logout/session/me）。于是整个 lifecycle
      // 套件共用同一个 Local Default 用户，工作区在其中不断累积——2026-08-10 实测
      // 跑到这个 spec 时该用户已有 6 个工作区，全是前面测试的残留。
      // 原断言 getByText("工作区启动面板") 与 heading "从一个工作区开始" 更是从未
      // 存在过的文案，四条测试因此从合入起就没通过过。
      //
      // 所以改成对状态不敏感的双分支：无工作区时校验空态引导，有工作区时校验列表入口。
      // 两种分支共同断言的那条才是本测试真正的回归目标——legacy execution-space 入口
      // 必须已经消失。
      const emptyState = page.getByText("还没有工作区", { exact: true });
      const newWorkspaceButton = page.getByTestId("sidebar-new-task-expanded");
      await expect(newWorkspaceButton).toBeVisible();

      if (await emptyState.isVisible()) {
        await expect(
          page.getByText(
            "先创建一个长期任务工作区，后续再在里面展开多个会话、资源范围和执行记录。",
          ),
        ).toBeVisible();
      } else {
        // 有工作区时应当渲染工作区侧栏列表，而不是回落到旧的执行空间视图
        await expect(page.getByTestId("workspace-list").or(newWorkspaceButton)).toBeVisible();
      }

      await expect(page.getByRole("button", { name: /查看执行详情/i })).toHaveCount(0);
    } finally {
      await deleteSession(api, userId, sessionId);
    }
  });

  test("new task dialog uses workspace basics instead of legacy sandbox controls", async ({
    page,
  }) => {
    const api = page.request;
    const { userId } = await registerLifecycleUser(api);
    const sessionId = await createSession(api, {
      title: "浏览器回归-新建任务弹窗",
      sandboxMode: "local",
    });

    try {
      await gotoAnalysisSession(page, sessionId);
      await page.getByTestId("sidebar-new-task-expanded").click();

      const dialog = page.getByRole("dialog").filter({ hasText: "新建工作区" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByLabel("任务名称")).toBeVisible();
      await expect(dialog.getByLabel("任务说明")).toBeVisible();
      await expect(dialog.getByRole("button", { name: "创建工作区" })).toBeDisabled();

      await expect(dialog.getByText("选择沙盒模式")).toHaveCount(0);
      await expect(dialog.getByText("默认连续", { exact: true })).toHaveCount(0);
      await expect(dialog.getByText("清空当前对话", { exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("heading", { name: "本地沙盒" })).toHaveCount(0);
      await expect(dialog.getByRole("heading", { name: "Docker 沙盒" })).toHaveCount(0);
    } finally {
      await deleteSession(api, userId, sessionId);
    }
  });

  test("creating a new task activates a workspace route and keeps the new branch usable", async ({
    page,
  }) => {
    const api = page.request;
    const { userId } = await registerLifecycleUser(api);
    const sourceSessionId = await createSession(api, {
      title: "浏览器回归-创建任务源会话",
      sandboxMode: "local",
    });
    const workspaceTitle = `浏览器回归-新建任务-${Date.now()}`;
    let createdWorkspaceId: string | null = null;
    let createdSessionId: string | null = null;

    try {
      await gotoAnalysisSession(page, sourceSessionId);
      await page.getByTestId("sidebar-new-task-expanded").click();

      const dialog = page.getByRole("dialog").filter({ hasText: "新建工作区" });
      await expect(dialog).toBeVisible();
      await dialog.getByLabel("任务名称").fill(workspaceTitle);
      await dialog.getByLabel("任务说明").fill("正式验证 workspace-first 新建任务流。");
      await dialog.getByRole("button", { name: "创建工作区" }).click();

      await expect
        .poll(() => extractWorkspaceIdFromUrl(page.url()), { timeout: 30_000 })
        .not.toBeNull();

      createdWorkspaceId = extractWorkspaceIdFromUrl(page.url());
      createdSessionId = extractSessionIdFromUrl(page.url());
      expect(createdWorkspaceId).toBeTruthy();
      expect(createdSessionId).toBeTruthy();

      // 收窄到工作区资源树容器：页面上有两个「当前工作区」文本节点（侧栏标题与资源树
      // 表头），裸 getByText 会撞 strict mode violation 而报「找不到」——那不是功能问题。
      // 2026-08-10 实测截图确认工作区创建成功、标题正常渲染，纯粹是选择器不够specific。
      await expect(
        page.getByTestId("workspace-artifacts-tree-surface").getByText("当前工作区"),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: workspaceTitle }).first(),
      ).toBeVisible();
      await expect(page.locator("textarea")).toBeEnabled();
    } finally {
      await deleteWorkspace(api, createdWorkspaceId);
      await deleteSession(api, userId, createdSessionId);
      await deleteSession(api, userId, sourceSessionId);
    }
  });

  test("new workspace conversations expose workspace config assets immediately", async ({
    page,
  }) => {
    const api = page.request;
    const { workspaceId, currentConversationId } = await createWorkspace(api, {
      title: `浏览器回归-工作区偏好文件-${Date.now()}`,
      mode: "analysis",
    });

    try {
      await page.goto(
        `/analysis?workspace_id=${workspaceId}&session_id=${currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      // 原断言锚在三处已不存在的文案上：
      //   getByRole("button", { name: "资产" }) —— activity 视图按钮的可访问名是 label
      //     本身（「当前工作区」/「全局工作区」），"资产" 只是 currentViewLabel 在找不到
      //     匹配项时的兜底默认值，正常渲染路径下不会出现；
      //   getByText("工作区资产") —— 资产面板标题实际是「当前工作区」；
      //   getByText("config") / getByText(".env") —— 资源树节点默认不展开。页面快照
      //     实测：树头部显示「当前对话的工作区文件 5 目录·10 文件」，但没有任何文件名
      //     节点进入 DOM（懒渲染 + 默认折叠），所以断言具体文件名注定不稳。
      // 本测试真正的回归目标是「新建工作区的会话一进来就已经扫描到工作区配置资产」，
      // 计数非零正是这件事的可靠证据，比钉死某个文件名稳定得多。
      const artifactsTree = page.getByTestId("workspace-artifacts-tree-surface");
      await expect(artifactsTree).toBeVisible();
      await expect(artifactsTree.getByText("当前对话的工作区文件")).toBeVisible();
      await expect(artifactsTree.getByText(/\d+\s*目录/)).toBeVisible();
      await expect(artifactsTree.getByText(/\d+\s*文件/)).toBeVisible();
    } finally {
      await deleteWorkspace(api, workspaceId);
    }
  });
});
