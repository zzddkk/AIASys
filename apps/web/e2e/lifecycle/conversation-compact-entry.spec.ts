import { expect, test } from "@playwright/test";

import {
  addSessionMessage,
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
} from "./support";
import {
  CHUNK_1,
  installMockStreamingResponse,
} from "./streaming-render.mocks";

test.describe("Conversation compact entry browser regression", () => {
  test("input area exposes compact entry and triggers the compact action while idle", async ({
    page,
  }) => {
    const api = page.request;
    const { userId } = await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: "浏览器回归-输入区压缩入口",
      initialConversationTitle: "输入区压缩会话",
      mode: "analysis",
    });
    let compactCalls = 0;

    try {
      await addSessionMessage(
        api,
        userId,
        workspace.currentConversationId,
        "这是一条用于 compact 回归的历史消息。",
        "assistant",
      );

      await page.route("**/api/sessions/**/compact", async (route) => {
        compactCalls += 1;
        // 延迟要给「重开 popover + 断言」留足窗口：按钮在途文案是「压缩中」，
        // 请求一结束就恢复成「压缩上下文」，窗口太短断言必漂。
        await page.waitForTimeout(1500);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ success: true }),
        });
      });

      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      // compact 入口折叠在会话头部的「上下文与预算」下拉里（TokenUsageBar
      // variant="dropdown"），先点开 Popover 才能看到压缩按钮。
      const contextTrigger = page.getByRole("button", { name: "上下文与预算" });
      await expect(page.locator("textarea")).toBeVisible();
      await contextTrigger.click();
      const compactButton = page.getByRole("button", {
        name: "压缩上下文",
        exact: true,
      });
      await expect(compactButton).toBeVisible();
      await expect(compactButton).toBeEnabled();

      await compactButton.click();
      // 点击后 Popover 立即关闭（setPopoverOpen(false)），在途期间 DOM 里没有
      // 「压缩上下文」按钮；重新点开才能看到「压缩中」禁用态。已用源码插桩验证：
      // 在途时该按钮真实渲染且 disabled=true。
      await contextTrigger.click();
      const compactingButton = page.getByRole("button", {
        name: "压缩中",
        exact: true,
      });
      await expect(compactingButton).toBeVisible();
      await expect(compactingButton).toBeDisabled();
      await expect
        .poll(() => compactCalls, { timeout: 5_000 })
        .toBe(1);
      await expect(page.getByText("对话上下文已压缩")).toBeVisible({
        timeout: 10_000,
      });
      // 压缩完成后 popover 已关闭，重新点开断言回到可用态
      await contextTrigger.click();
      await expect(compactButton).toBeEnabled();
      await expect(compactButton).toContainText("压缩");
    } finally {
      await page.unroute("**/api/sessions/**/compact");
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });

  test("input area keeps compact entry disabled while the current branch is running", async ({
    page,
  }) => {
    const api = page.request;
    const { userId } = await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: "浏览器回归-运行中压缩禁用",
      initialConversationTitle: "运行中压缩禁用会话",
      mode: "analysis",
    });

    try {
      await addSessionMessage(
        api,
        userId,
        workspace.currentConversationId,
        "这是一条用于 running 态 compact 禁用验证的历史消息。",
        "assistant",
      );
      await installMockStreamingResponse(page);

      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      const input = page.locator("textarea");
      const contextTrigger = page.getByRole("button", { name: "上下文与预算" });
      await expect(input).toBeVisible();
      await contextTrigger.click();
      const compactButton = page.getByRole("button", {
        name: "压缩上下文",
        exact: true,
      });
      await expect(compactButton).toBeVisible();
      await expect(compactButton).toBeEnabled();

      await input.fill("请模拟一段会让当前会话进入 running 态的流式回复");
      await input.press("Enter");

      await expect(page.getByText(CHUNK_1, { exact: true })).toBeVisible();
      // 运行中按钮文案变为「运行中」，「压缩上下文」定位器此时匹配不到任何元素。
      // popover 可能仍开或已关，确保打开后断言禁用。
      const runningButton = page.getByRole("button", {
        name: "运行中",
        exact: true,
      });
      if (!(await runningButton.isVisible())) {
        await contextTrigger.click();
      }
      await expect(runningButton).toBeVisible();
      await expect(runningButton).toBeDisabled();

      await expect
        .poll(async () => await input.isEnabled(), { timeout: 10_000 })
        .toBe(true);
      // 流式结束时页面会重渲染（会话状态/用量刷新），popover 可能被重置关掉。
      // 用 toPass 包住「确保打开 + 断言」，让瞬态重渲染被吸收掉而不是碰运气。
      await expect(async () => {
        if (!(await compactButton.isVisible())) {
          await contextTrigger.click();
        }
        await expect(compactButton).toBeVisible({ timeout: 1_000 });
        await expect(compactButton).toBeEnabled({ timeout: 1_000 });
      }).toPass({ timeout: 15_000 });
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
