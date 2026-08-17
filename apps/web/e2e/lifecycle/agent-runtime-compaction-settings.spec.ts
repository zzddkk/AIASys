import { expect, test } from "@playwright/test";

import { createWorkspace, deleteWorkspace, registerLifecycleUser } from "./support";

test.describe("Agent runtime compaction settings", () => {
  test("branch settings can save runtime compaction overrides and reload them", async ({
    page,
  }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `自动压缩策略回归-${Date.now()}`,
      mode: "analysis",
    });

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      // 入口已从输入框工具栏收敛到 DockHeader 的「会话设置」下拉
      // （2026-08-14 会话配置入口收敛，见迭代记录/2026-08-14-会话配置入口收敛.md）
      await page.getByRole("button", { name: "会话设置" }).click();
      await page.getByRole("menuitem", { name: "会话配置" }).click();
      const sessionDialog = page.getByRole("dialog").filter({
        hasText: "当前会话配置",
      });
      await expect(sessionDialog).toBeVisible();

      const reservedInput = sessionDialog.getByTestId(
        "agent-runtime-reserved-context-size",
      );
      const ratioInput = sessionDialog.getByTestId(
        "agent-runtime-compaction-trigger-ratio",
      );
      const saveButton = sessionDialog.getByTestId("agent-runtime-save");

      await expect(reservedInput).toHaveValue("50000");
      await expect(ratioInput).toHaveValue("0.85");

      await reservedInput.fill("32000");
      await ratioInput.fill("0.67");
      await saveButton.scrollIntoViewIfNeeded();
      const saveRequest = page.waitForResponse((response) => {
        return (
          response.url().includes("/api/agent-config/analysis/runtime") &&
          response.request().method() === "PUT"
        );
      });
      await saveButton.evaluate((node: HTMLElement) => node.click());
      const saveResponse = await saveRequest;
      expect(saveResponse.ok()).toBeTruthy();

      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/agent-config/analysis/editor?session_id=${workspace.currentConversationId}`,
          );
          const payload = (await response.json()) as {
            reserved_context_size: number;
            compaction_trigger_ratio: number;
            runtime_source: string;
            has_local_runtime_override: boolean;
          };
          return JSON.stringify(payload);
        })
        .toContain('"reserved_context_size":32000');

      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/agent-config/analysis/editor?session_id=${workspace.currentConversationId}`,
          );
          const payload = (await response.json()) as {
            reserved_context_size: number;
            compaction_trigger_ratio: number;
            runtime_source: string;
            has_local_runtime_override: boolean;
          };
          return [
            payload.compaction_trigger_ratio,
            payload.runtime_source,
            payload.has_local_runtime_override,
          ].join("|");
        })
        .toBe("0.67|session_override|true");

      await page.reload({ waitUntil: "domcontentloaded" });

      // 对话框的打开状态同步在 URL（openAgentConfigDialog ->
      // replaceWorkspaceOverlay("agent_config")，useWorkspaceOverlayState.ts），
      // reload 后 syncRouteOverlay 会自动重开对话框。旧版在这里再点一次
      // 旧入口（input-tool-config 输入框按钮），点击会被已打开的对话框遮罩拦截到超时。
      const reloadedDialog = page.getByRole("dialog").filter({
        hasText: "当前会话配置",
      });
      await expect(reloadedDialog).toBeVisible({ timeout: 15_000 });
      await expect(
        reloadedDialog.getByTestId("agent-runtime-reserved-context-size"),
      ).toHaveValue("32000");
      await expect(
        reloadedDialog.getByTestId("agent-runtime-compaction-trigger-ratio"),
      ).toHaveValue("0.67");
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
