import { test } from "@playwright/test";

/**
 * Ctrl+` 是否切到终端侧边栏页签（人工看图确认，本文件不做断言）。
 *
 * 2026-08-11 修了三处让它从来跑不起来的问题：
 *
 * 1. `const ARTIFACTS_DIR = path.resolve(__dirname, ...)` 在 ESM 下直接抛
 *    `ReferenceError: __dirname is not defined in ES module scope`。这个错误发生在
 *    模块加载期，后果比单个文件失败大得多：playwright 收集测试时一并失败，整个
 *    目录变成 `0 tests in 0 files`——同目录另外 9 个脚本一起收不到。这正是这批脚本
 *    「只能靠临时改配置跑」的真实原因，改配置也救不了。
 *
 * 2. 那个路径算出来是 `apps/design-draft/archive/artifacts`，而仓库里没有这个目录
 *    （design-draft 是 suite 级的平级仓 AIASys-design-draft）。脚本会 mkdirSync
 *    把它造出来，在 apps/ 下留一个垃圾目录。现在改用 `testInfo.outputPath()`，
 *    截图落在配置的 outputDir（test-results/manual/）里，不再写别处，也不需要 fs。
 *
 * 3. 硬编码 http://127.0.0.1:13000，端口一变就连到空气上。改成相对路径走 baseURL。
 *
 * 另外删掉了未使用的 `expect` 导入——它给人一种「这里有断言」的错觉，实际没有。
 * 本文件是人工审查脚本，跑通不代表功能正确，结论要看截图。
 */
test("verify Ctrl+Backquote switches to terminal sidebar tab", async ({ page }, testInfo) => {
  await page.goto("/analysis");
  await page.waitForTimeout(2000);

  // 初始状态（工作区首页）
  await page.screenshot({
    path: testInfo.outputPath("terminal-tab-before.png"),
    fullPage: false,
  });

  // 尝试进入某个工作区。这里的工作区名是本机开发数据，别处跑很可能匹配不到——
  // count 为 0 时跳过，脚本仍会产出后两张截图，方便看「没有工作区时按键的表现」。
  const workspaceButton = page.locator('button:has-text("新任务测试")').first();
  if ((await workspaceButton.count()) > 0) {
    await workspaceButton.click();
    await page.waitForTimeout(1500);
  }

  await page.screenshot({
    path: testInfo.outputPath("terminal-tab-workspace.png"),
    fullPage: false,
  });

  await page.keyboard.press("Control+Backquote");
  await page.waitForTimeout(1000);

  await page.screenshot({
    path: testInfo.outputPath("terminal-tab-after.png"),
    fullPage: false,
  });

  // 按键后页签数量。人看这个数字判断有没有误开新标签页；
  // 要把它变成自动化判据，得先确定期望值，然后写成 expect 并搬去 e2e/lifecycle/。
  const tabCount = await page.locator("[role='tab']").count();
  console.log(`Tab count after Ctrl+\`: ${tabCount}`);
});
