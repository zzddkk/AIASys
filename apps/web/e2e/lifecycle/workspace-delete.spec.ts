import { expect, test } from "@playwright/test";

import { createWorkspace, deleteWorkspace, registerLifecycleUser } from "./support";

/**
 * 工作区删除必须真的成功——这是测试隔离的地基，也是一个长期被吞掉的 Windows 缺陷。
 *
 * 背景（2026-08-11 实测）：跑过自动任务的工作区，删除时后端抛 500：
 *
 *   PermissionError: [WinError 5] 拒绝访问。:
 *     'C:\\Users\\ke\\AIASys\\workspaces\\local_default\\<sid>'
 *     -> 'C:\\Users\\ke\\AIASys\\workspaces\\.trash\\local_default\\<sid>-xxxxxxxx'
 *
 * 根因是 session/core.py 的 detach_session_for_deletion 用 shutil.move 把会话目录
 * 重命名进 .trash。Windows 语义下，只要目录内还有任何未释放的文件句柄（或有进程以它
 * 为 cwd），os.rename 就报 WinError 5；Linux 允许重命名带打开句柄的目录，所以这个缺陷
 * 只在 Windows 现场出现——而主力开发正是 Windows。
 *
 * 为什么一直没被发现：e2e 的清理助手 deleteWorkspace 只 console.warn 不失败，
 * 删除失败从不导致任何测试变红。后果是残留工作区在单机默认用户下无限堆积，
 * 侧边栏塞满历史工作区，进而让所有「空态」断言变得不可靠。
 *
 * 所以这条用例的职责就是：把那声被吞掉的 warn 变成一次真正的红。
 */
test.describe("工作区删除", () => {
  test.setTimeout(120_000);

  test("跑过自动任务的工作区可以被删除（Windows 句柄占用回归）", async ({ page }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-工作区删除-${Date.now()}`,
      initialConversationTitle: "删除回归主控会话",
    });

    let deleted = false;
    try {
      // 制造出「目录内有活动过的会话」这个前提：自动任务会新建会话并真的去执行，
      // 从而在会话目录里留下句柄。不要求 LLM 成功——无密钥时执行失败，
      // 但会话目录与其中的文件已经产生，这正是触发 WinError 5 的条件。
      const createResponse = await api.post(
        `/api/auto-tasks/workspaces/${workspace.workspaceId}/tasks`,
        {
          data: {
            title: "删除回归-自动任务",
            prompt: "请简短回复 done。",
            trigger_type: "interval",
            trigger_value: "3600",
            first_run_policy: "next_scheduled",
            sandbox_mode: "local",
            mode: "analysis",
            overlap_policy: "skip",
            session_strategy: "new_each_time",
          },
        },
      );
      expect(createResponse.ok()).toBeTruthy();
      const created = (await createResponse.json()) as { task_id: string };

      const runResponse = await api.post(
        `/api/auto-tasks/workspaces/${workspace.workspaceId}/tasks/${created.task_id}/run`,
      );
      expect(
        runResponse.ok(),
        `立即运行接口应可用，实际 status=${runResponse.status()}`,
      ).toBeTruthy();

      // 等这一轮执行落库（last_run_at 无条件写入，与 LLM 成败无关）。
      await expect
        .poll(
          async () => {
            const response = await api.get(
              `/api/auto-tasks/workspaces/${workspace.workspaceId}/tasks`,
            );
            if (!response.ok()) {
              return null;
            }
            const body = (await response.json()) as {
              tasks?: Array<{ task_id: string; last_run_at?: string | null }>;
            };
            return (
              body.tasks?.find((task) => task.task_id === created.task_id)
                ?.last_run_at ?? null
            );
          },
          { timeout: 45_000, message: "自动任务应已执行过一轮" },
        )
        .not.toBeNull();

      // 核心断言：删除必须返回成功，不允许 500。
      const deleteResponse = await api.delete(
        `/api/workspaces/${workspace.workspaceId}`,
        { timeout: 30_000 },
      );
      const body = deleteResponse.ok() ? "" : await deleteResponse.text();
      expect(
        deleteResponse.ok(),
        `删除工作区应成功，实际 status=${deleteResponse.status()} body=${body.slice(0, 400)}`,
      ).toBeTruthy();
      deleted = true;

      // 删除后必须从列表里真的消失，而不只是接口返回 200。
      await expect
        .poll(
          async () => {
            const response = await api.get("/api/workspaces");
            if (!response.ok()) {
              return true;
            }
            const payload = (await response.json()) as
              | { workspaces?: Array<{ workspace_id: string }> }
              | Array<{ workspace_id: string }>;
            const list = Array.isArray(payload) ? payload : payload.workspaces ?? [];
            return list.some((item) => item.workspace_id === workspace.workspaceId);
          },
          { timeout: 15_000, message: "已删除的工作区不应再出现在列表里" },
        )
        .toBe(false);
    } finally {
      if (!deleted) {
        await deleteWorkspace(api, workspace.workspaceId);
      }
    }
  });
});
