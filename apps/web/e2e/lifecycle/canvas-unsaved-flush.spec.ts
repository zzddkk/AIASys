import { expect, test, type APIRequestContext } from "@playwright/test";
import {
  createWorkspace,
  deleteWorkspace,
  openWorkspaceFilesPanel,
  registerLifecycleUser,
} from "./support";
/**
 * 画布编辑后立刻切走，改动不能丢。
 *
 * 为什么单独一份 spec：画布的保存是 300ms debounce，scheduleSave 只起一个 setTimeout。
 * 编辑完马上切到别的文件、关掉画布、或者离开页面，组件一卸载定时器就跟着消失，磁盘停在
 * 上一次落盘的内容，界面上没有任何报错——保存状态本来就还是 dirty，看不出区别。
 * 修法是在卸载 / 切换画布文件 / pagehide 时把 pending 冲掉（useCanvasHandlers 里的
 * flushPending）。
 *
 * 这份 spec 的价值全在「不等待」这一点上。canvas-preview-ctrl-wheel 那条主用例里也编辑
 * 了 subpath，但它在编辑后 poll 磁盘直到落盘完成才继续操作，debounce 早已跑完，卸载时
 * 根本没有 pending 可丢——那条用例摘掉 flushPending 照样通过，验证过。所以覆盖这个场景
 * 必须有一条「编辑完立刻切走、中间不插任何等待」的用例，不能靠主用例顺带。
 *
 * 改这份 spec 时不要在 fill 与切走之间插入 poll、waitForTimeout 或任何对磁盘的断言，
 * 那会把它退化成主用例的重复。
 */
const CANVAS_FLUSH_TIMEOUT = 120_000;
async function readCanvasSubpath(
  api: APIRequestContext,
  workspaceId: string,
  fileName: string,
  nodeId: string,
): Promise<string | undefined> {
  const response = await api.get(
    `/api/workspaces/${workspaceId}/files/content/${fileName}`,
  );
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const parsed = JSON.parse(String(body.content));
  return parsed.nodes.find((node: { id: string }) => node.id === nodeId)
    ?.subpath;
}
test.describe("Canvas unsaved flush", () => {
  test.setTimeout(CANVAS_FLUSH_TIMEOUT);
  test("editing then leaving immediately still persists the change", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-画布未保存冲写-${Date.now()}`,
      mode: "analysis",
      initialConversationTitle: "画布冲写会话",
    });
    const canvasFileName = `canvas-flush-${Date.now()}.canvas`;
    const linkedFileName = `canvas-flush-target-${Date.now()}.md`;
    const initialSubpath = "#初始位置";
    const editedSubpath = "#冲写后的位置";
    try {
      // 用 files/create 接口建文件，不要用 seedWorkspaceFile 直接往磁盘写。
      // 实测直接写磁盘之后 files/content 读不到（response.ok() 为 false）：后端那条读路径
      // 依赖工作区文件的登记，绕过接口写进去的文件不在其中。
      const createLinkedResponse = await api.post(
        `/api/workspaces/${workspace.workspaceId}/files/create`,
        {
          data: {
            path: linkedFileName,
            content: "# Flush Target\n\n切走时的目标文件。\n",
            overwrite: false,
          },
        },
      );
      expect(createLinkedResponse.ok()).toBeTruthy();
      const createCanvasResponse = await api.post(
        `/api/workspaces/${workspace.workspaceId}/files/create`,
        {
          data: {
            path: canvasFileName,
            content: JSON.stringify(
              {
                nodes: [
                  {
                    id: "node-file",
                    type: "file",
                    x: 80,
                    y: 80,
                    width: 320,
                    height: 180,
                    file: linkedFileName,
                    subpath: initialSubpath,
                  },
                ],
                edges: [],
              },
              null,
              2,
            ),
            overwrite: false,
          },
        },
      );
      expect(createCanvasResponse.ok()).toBeTruthy();
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(page.locator("textarea")).toBeVisible();
      const panel = await openWorkspaceFilesPanel(page);
      await panel.getByPlaceholder("搜索文件或目录...").fill(canvasFileName);
      if (
        !(await panel.getByText(canvasFileName, { exact: true }).isVisible())
      ) {
        await panel.getByRole("button", { name: "刷新" }).click();
      }
      await expect(
        panel.getByText(canvasFileName, { exact: true }),
      ).toBeVisible();
      await panel
        .getByRole("button", {
          name: `打开 ${canvasFileName} 的文件操作菜单`,
          exact: true,
        })
        .click();
      await page.getByRole("menuitem", { name: "在主画布打开" }).click();
      await expect(page.locator("[data-canvas-viewport]")).toBeVisible();
      await expect(
        page.getByText(initialSubpath, { exact: true }),
      ).toBeVisible();
      // 落盘基线：确认此刻磁盘还是初始值。少了这步，后面即使断言通过也无法排除
      // 「文件本来就写着目标值」这种假绿。
      expect(
        await readCanvasSubpath(
          api,
          workspace.workspaceId,
          canvasFileName,
          "node-file",
        ),
      ).toBe(initialSubpath);
      await page.locator('[data-canvas-node-id="node-file"]').click();
      const propertiesPanel = page.getByTestId("canvas-properties-panel");
      await expect(propertiesPanel).toBeVisible();
      const subpathInput = propertiesPanel.getByLabel("内部位置");
      await expect(subpathInput).toHaveValue(initialSubpath);

      const canvasWrites: string[] = [];
      page.on("request", (request) => {
        if (request.method() !== "PUT" || !request.url().includes("/canvas")) {
          return;
        }
        const body = request.postData() || "";
        const matched = body.match(/"subpath":\s*"([^"]*)"/);
        canvasWrites.push(matched ? matched[1] : "(无 subpath)");
      });

      // 冻结页面时钟，把「改动还没落盘」这个前提做成确定的。
      //
      // 不冻结时这条用例是 flaky 的：实测同一份代码连跑两次，一次红一次绿。fill 与
      // click 之间到底有没有跨过 300ms 的 debounce，取决于机器当时的负载——跨过了就
      // 由定时器正常落盘，测的就不再是切走时的补写，用例转绿；没跨过才真正验证 flush。
      // 这种测试进 CI 只会训练人忽略红灯，比没有更糟。
      //
      // install 放在页面与面板都就绪之后，避免冻结影响加载阶段依赖 setTimeout 的逻辑。
      // 后面的点击不受影响：Playwright 的 actionability 检查跑在驱动侧，不看页面时钟；
      // flush 补发的 PUT 走 fetch，也不依赖时钟推进。
      await page.clock.install();

      // 关键动作：改完立刻切走，中间不许有任何等待或磁盘断言。
      await subpathInput.fill(editedSubpath);
      await page
        .locator('[data-canvas-node-id="node-file"]')
        .getByRole("button", { name: "打开文件" })
        .click();
      await expect(
        page.getByRole("heading", { name: linkedFileName }),
      ).toBeVisible();

      // 这里刻意不推进时钟。
      //
      // 推进会把还活着的 debounce 定时器一并唤醒，改动照样落盘，断言就变成必然成立。
      // 时钟停住之后，定时器不会触发，落盘只能来自别的路径。
      //
      // 需要说清一件事：本用例并不能证明「卸载时的 flush 补写」这条路径是必需的。摘掉
      // useCanvasHandlers 里的 flushPending 之后，时钟冻结版本仍然通过——说明还有第三条
      // 把改动写下去的路径没被定位（怀疑在切换预览时由父组件触发，未确认）。所以这条用例
      // 的定位是行为回归：编辑后立刻切走，改动最终必须出现在磁盘上，不管由哪个机制兜住。
      // 不要在注释或提交信息里把它当成 flush 的回归测试。
      //
      // poll 不受影响：它走 Playwright 的 request context 读磁盘，重试节奏也由驱动侧控制，
      // 都不看页面时钟。
      await expect
        .poll(
          () =>
            readCanvasSubpath(
              api,
              workspace.workspaceId,
              canvasFileName,
              "node-file",
            ),
          {
            message:
              "编辑后立刻切走，未落盘的改动应由卸载时的 flush 补写，而不是静默丢弃。" +
              `实际发出的 canvas 写请求依次为: ${JSON.stringify(canvasWrites)}`,
          },
        )
        .toBe(editedSubpath);
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
