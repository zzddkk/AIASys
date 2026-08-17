import { expect, test } from "@playwright/test";

import { createWorkspace, deleteWorkspace, registerLifecycleUser } from "./support";

/**
 * 删除工作区必须走应用内对话框，不能退化成浏览器原生 confirm。
 *
 * 这个用例原先在 e2e/ 根层（workspace-delete-dialog.smoke.spec.ts），而
 * playwright.lifecycle.config.ts 的 testDir 是 ./e2e/lifecycle——根层不在任何配置的
 * 扫描范围内，所以它写好之后从未被执行过一次。仓库里「有这个测试」的印象一直留着。
 *
 * 搬过来时改掉了两处让它无法在套件里稳定运行的写法：
 *
 * 1. 原来硬编码 http://127.0.0.1:13100/analysis。套件的 baseURL 走
 *    PLAYWRIGHT_BASE_URL（缺省 13000），端口对不上就只能连到一个不存在的服务。
 *    改成相对路径，由配置决定连哪儿。
 *
 * 2. 原来直接取页面上第一个「更多操作」，隐含假设「运行时恰好已有工作区」。
 *    空库时这个断言会等满 30 秒再失败，而失败原因看起来像 UI 坏了，其实是没数据。
 *    改成先用 API 建一个，前置状态自己造。
 *
 * 断言的是「原生 dialog 一次都没出现过」而不是「界面上有个弹窗」：后者在实现退化成
 * window.confirm 时同样可能为真（页面上别处也有弹窗），前者才真正锁住这条契约。
 * 最后点「取消」而不是「删除」——这个用例只验证交互形态，不该顺手删掉真实数据。
 */
test.describe("Workspace delete dialog", () => {
  test("删除工作区用应用内对话框，不触发浏览器原生 confirm", async ({ page }) => {
    const nativeDialogs: string[] = [];
    page.on("dialog", async (dialog) => {
      nativeDialogs.push(`${dialog.type()}:${dialog.message()}`);
      await dialog.dismiss();
    });

    await registerLifecycleUser(page.request);

    // 标题带随机后缀，避免与历史遗留数据同名（本套件共用单机默认用户，库里可能有旧数据）。
    const workspace = await createWorkspace(page.request, {
      title: `删除对话框冒烟-${Math.random().toString(36).slice(2, 10)}`,
    });

    try {
      await page.goto("/analysis", { waitUntil: "domcontentloaded" });

      const moreActions = page.getByRole("button", { name: "更多操作" }).first();
      await expect(moreActions).toBeVisible();
      await moreActions.click();

      const deleteEntry = page.getByText("删除工作区", { exact: true }).last();
      await expect(deleteEntry).toBeVisible();
      await deleteEntry.click();

      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expect(
        dialog.getByText(/该工作区下的所有会话和工作区文件都会被删除/),
      ).toBeVisible();

      const confirmButton = dialog.getByRole("button", {
        name: "删除工作区",
        exact: true,
      });
      const cancelButton = dialog.getByRole("button", { name: "取消", exact: true });
      await expect(confirmButton).toBeVisible();
      await expect(cancelButton).toBeVisible();

      // 核心断言：整个交互过程中没有出现任何浏览器原生弹窗。
      expect(nativeDialogs).toEqual([]);

      // 取消后对话框应关闭，且仍然没有原生弹窗。
      await cancelButton.click();
      await expect(dialog).toBeHidden();
      expect(nativeDialogs).toEqual([]);
    } finally {
      await deleteWorkspace(page.request, workspace.workspaceId);
    }
  });
});
