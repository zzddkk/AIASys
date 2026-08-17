import { expect, test, type Page } from "@playwright/test";

import {
  buildAnalysisUrl,
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
} from "./support";

async function waitForBackend(page: Page) {
  await expect
    .poll(
      async () => {
        // 后端健康检查地址与 readiness.setup.ts 保持同一来源：默认 13001
        // （dev.sh 缺省后端端口），端口自动切换时由 run_lifecycle_playwright.sh
        // export PLAYWRIGHT_BACKEND_HEALTH_URL 覆盖。曾写死 13002（某次调试时的
        // 移位端口），干净端口环境下必然超时。
        const healthUrl =
          process.env.PLAYWRIGHT_BACKEND_HEALTH_URL ??
          "http://127.0.0.1:13001/health";
        const response = await page.request
          .get(healthUrl, { timeout: 1_000 })
          .catch(() => null);
        return response?.ok() ?? false;
      },
      { timeout: 60_000 },
    )
    .toBe(true);
}

test.describe("Sandbox strategy workspace entry", () => {
  test("opens workspace sandbox settings from the chat input badge", async ({
    page,
  }, testInfo) => {
    const api = page.request;
    await waitForBackend(page);
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-沙盒策略入口-${Date.now()}`,
      initialConversationTitle: "沙盒策略入口会话",
    });

    try {
      await page.goto(
        buildAnalysisUrl({
          workspaceId: workspace.workspaceId,
          conversationId: workspace.currentConversationId,
        }),
        { waitUntil: "domcontentloaded" },
      );

      await expect(page.locator("textarea")).toBeVisible();

      // badge 现在用 aria-label（InputArea.tsx:741），点击打开的是主画布的
      // 执行环境 tab（ExecutionResourcesPanel），不再是独立弹窗。
      const runtimeBadge = page.getByRole("button", { name: /^运行环境：/ });
      await expect(runtimeBadge).toBeVisible();
      await runtimeBadge.click();

      await expect(page.getByText("当前执行模式")).toBeVisible();
      await expect(page.getByText("资源管理")).toBeVisible();

      // Docker 区块仍内嵌 ContainerResourcesPanel，登记表单链路不变。
      // /Docker 沙盒/ 会同时命中面板的分段 tab 和「资源管理」里的导航卡片，
      // 限定在资源管理 section 内点卡片。
      await page
        .locator("section", { hasText: "资源管理" })
        .getByRole("button", { name: /Docker 沙盒/ })
        .click();
      const containerDialog = page.getByTestId("container-resources-panel");
      await expect(containerDialog).toBeVisible();
      await expect(containerDialog.getByRole("button", { name: "登记 Docker 沙盒" })).toBeVisible();
      await containerDialog.getByRole("button", { name: "登记 Docker 沙盒" }).first().click();
      await expect(containerDialog.getByTestId("docker-sandbox-register-form")).toBeVisible();
      await expect(containerDialog.getByRole("button", { name: "登记已有容器" })).toBeVisible();
      await expect(containerDialog.getByRole("button", { name: "按镜像创建容器" })).toBeVisible();
      await expect(containerDialog.getByLabel("容器 ID 或名称")).toBeVisible();

      await page.screenshot({
        path: testInfo.outputPath("runtime-environment-entry.png"),
        fullPage: true,
      });
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
