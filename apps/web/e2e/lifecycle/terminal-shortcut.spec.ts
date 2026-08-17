import { expect, test } from "@playwright/test";

import {
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
} from "./support";

/**
 * Ctrl+` 打开终端的自动化判据。
 *
 * 由 e2e/manual/verify-terminal-tab.spec.ts（纯截图人工审查脚本）转换而来。
 * 原脚本注释里写明了转换路径：确定期望值 → 写成 expect → 搬进 lifecycle。
 * 期望值：按键后终端在主画布出现（xterm 挂载点可见）。
 *
 * 调查备注：转换时发现快捷键的接收端疑似断链——MainContent 把按键路由到
 * requestSidebarTab("terminal")，但终端 UI 已迁到主画布 Tab
 * （WorkspaceTabBar 的「新建终端」），侧边栏没有 terminal 面板的消费者。
 * 若本测试失败，即为该断链的实证。
 */
test.describe("Terminal shortcut", () => {
  test("Ctrl+` opens a terminal in the workspace surface", async ({ page }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-终端快捷键-${Date.now()}`,
      mode: "analysis",
      initialConversationTitle: "终端会话",
    });

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(page.locator("textarea").first()).toBeVisible();

      await page.keyboard.press("Control+Backquote");

      // 终端 spawn/attach 需要走后端 websocket，给足余量
      await expect(page.locator(".xterm").first()).toBeVisible({ timeout: 20_000 });
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
