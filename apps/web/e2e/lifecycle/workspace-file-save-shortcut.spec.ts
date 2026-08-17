import { expect, test } from "@playwright/test";

import {
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
  openWorkspaceFilesPanel
} from "./support";

test.describe("文件保存快捷键", () => {
  test.setTimeout(240_000);

  test("在代码编辑器中 Ctrl+S 保存文件", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });

    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-保存快捷键-${Date.now()}`,
      mode: "analysis",
      initialConversationTitle: "保存快捷键会话",
    });

    const codeFileName = "save-shortcut-test.py";
    const originalContent = "print('hello')\n";
    const updatedContent = "print('world')\n";

    try {
      const createFile = await api.post(
        `/api/workspaces/${workspace.workspaceId}/files/create`,
        {
          data: {
            path: codeFileName,
            content: originalContent,
            overwrite: true,
          },
        },
      );
      expect(createFile.ok()).toBeTruthy();

      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(page.locator("textarea")).toBeVisible();
      const panel = await openWorkspaceFilesPanel(page);
      await panel.getByPlaceholder("搜索文件或目录...").fill(codeFileName);
      await expect(panel.getByText(codeFileName, { exact: true })).toBeVisible();

      await panel
        .getByRole("button", {
          name: `打开 ${codeFileName} 的文件操作菜单`,
          exact: true,
        })
        .click();
      await page.getByRole("menuitem", { name: "在主画布打开" }).click();

      await expect(page.getByRole("heading", { name: codeFileName })).toBeVisible();

      // 进入编辑模式
      await page
        .getByRole("button", {
          name: `打开 ${codeFileName} 的文件操作菜单`,
          exact: true,
        })
        .click();
      await page.getByRole("menuitem", { name: "编辑文件" }).click();

      const editableEditor = page.locator(".cm-content[contenteditable='true']").first();
      await expect(editableEditor).toBeVisible();

      // 修改内容
      await editableEditor.click();
      await page.keyboard.press("Control+a");
      await page.keyboard.type(updatedContent);

      // 确认未保存状态
      await expect(page.getByText("未保存").first()).toBeVisible(); // 多处渲染，取其一

      // 按下 Ctrl+S 保存
      await page.keyboard.press("Control+s");

      // 等待保存完成，状态变为已保存
      await expect(page.getByText("已保存").first()).toBeVisible();

      // 通过 API 验证文件内容已持久化
      const contentResponse = await api.get(
        `/api/workspaces/${workspace.workspaceId}/files/content/${encodeURIComponent(codeFileName)}`,
      );
      expect(contentResponse.ok()).toBeTruthy();
      const contentBody = (await contentResponse.json()) as { content: string };
      expect(contentBody.content).toBe(updatedContent);
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });

  test("在 Markdown 预览源编辑模式下 Ctrl+S 保存文件", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });

    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-Markdown保存快捷键-${Date.now()}`,
      mode: "analysis",
      initialConversationTitle: "Markdown保存快捷键会话",
    });

    const markdownFileName = "save-shortcut-test.md";
    const originalContent = "# Hello\n\nOriginal content.\n";
    const updatedContent = "# Hello\n\nUpdated content.\n";

    try {
      const createFile = await api.post(
        `/api/workspaces/${workspace.workspaceId}/files/create`,
        {
          data: {
            path: markdownFileName,
            content: originalContent,
            overwrite: true,
          },
        },
      );
      expect(createFile.ok()).toBeTruthy();

      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(page.locator("textarea")).toBeVisible();
      const panel = await openWorkspaceFilesPanel(page);
      await panel.getByPlaceholder("搜索文件或目录...").fill(markdownFileName);
      await expect(panel.getByText(markdownFileName, { exact: true })).toBeVisible();

      await panel
        .getByRole("button", {
          name: `打开 ${markdownFileName} 的文件操作菜单`,
          exact: true,
        })
        .click();
      await page.getByRole("menuitem", { name: "在主画布打开" }).click();

      await expect(page.getByRole("heading", { name: markdownFileName })).toBeVisible();

      // 切换到源编辑模式
      const editButton = page.getByRole("button", { name: "编辑" }).first();
      if (await editButton.count() > 0) {
        await editButton.click();
      }

      // 等待编辑器出现并修改内容
      const editor = page.locator(".cm-content[contenteditable='true']").first();
      await expect(editor).toBeVisible();

      await editor.click();
      await page.keyboard.press("Control+a");
      await page.keyboard.type(updatedContent);

      // 确认未保存状态
      await expect(page.getByText("未保存").first()).toBeVisible(); // 多处渲染，取其一

      // 按下 Ctrl+S 保存
      await page.keyboard.press("Control+s");

      // 等待保存完成
      await expect(page.getByText("已保存").first()).toBeVisible();

      // 验证文件内容已持久化
      const contentResponse = await api.get(
        `/api/workspaces/${workspace.workspaceId}/files/content/${encodeURIComponent(markdownFileName)}`,
      );
      expect(contentResponse.ok()).toBeTruthy();
      const contentBody = (await contentResponse.json()) as { content: string };
      expect(contentBody.content).toBe(updatedContent);
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
