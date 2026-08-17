import { expect, test } from "@playwright/test";

import {
  buildAnalysisUrl,
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
} from "./support";

test.describe("AutoTask run-now browser regression", () => {
  test.setTimeout(120_000);

  test("auto task run-now creates a new branch and updates task state", async ({
    page,
  }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: "浏览器回归-自动任务立即运行",
      initialConversationTitle: "自动任务主控会话",
    });

    try {
      await page.goto(
        buildAnalysisUrl({
          workspaceId: workspace.workspaceId,
          conversationId: workspace.currentConversationId,
        }),
        {
          waitUntil: "domcontentloaded",
        },
      );

      await expect(page.locator("textarea")).toBeVisible();

      // UI 已重构：自动化任务面板从侧边栏顶层按钮移进了「工作区设置」对话框
      // （WorkspaceConfigDialog 的 auto-tasks 分区）。而且导航项的可访问名是
      // 「标签 + 描述」两个 span 拼接，即「自动化任务 管理当前工作区的自动化任务」，
      // 所以原来的 exact: "自动化任务" 双重失配。
      // 这条测试因 Windows 上 webServer 起不来而长期没跑过，定位器就这么静默腐烂了。
      await page.getByRole("button", { name: "工作区设置", exact: true }).click();
      const configDialog = page.getByRole("dialog", { name: "工作区配置" });
      await expect(configDialog).toBeVisible();
      await configDialog.getByRole("button", { name: /^自动化任务/ }).click();
      await expect(configDialog.getByText("当前工作区还没有自动化任务")).toBeVisible();

      const createResponse = await api.post(
        `/api/auto-tasks/workspaces/${workspace.workspaceId}/tasks`,
        {
          data: {
            title: "浏览器回归-自动任务立即运行",
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
      const created = (await createResponse.json()) as {
        task_id: string;
        first_run_policy?: string;
      };
      expect(created.first_run_policy).toBe("next_scheduled");

      // 以下定位一律限定在对话框内：侧边栏也有「刷新」按钮，全局定位会歧义。
      await configDialog.getByRole("button", { name: "刷新" }).click();
      const taskRow = configDialog.locator("section").filter({
        hasText: "浏览器回归-自动任务立即运行",
      }).first();
      await expect(taskRow).toBeVisible();
      await expect(configDialog.getByText("等待计划时间").first()).toBeVisible();
      await expect(taskRow.getByText("触发", { exact: true })).toBeVisible();
      await expect(taskRow.getByText("每次新建会话")).toBeVisible();
      await expect
        .poll(async () => {
          return configDialog
            .getByText("等待计划时间")
            .first()
            .evaluate((node) => node.getBoundingClientRect().width);
        })
        .toBeGreaterThan(64);

      await taskRow.getByRole("button", { name: "立即运行" }).click();

      // fired_count 只在 run_succeeded 时递增（engine.py:552），本地/CI 无 LLM 密钥时
      // 执行必然失败，该计数器永远为 0，原断言必然轮询到超时。
      // 改为断言 last_run_at：它在 finally 中无条件写入（engine.py:551），
      // 证明「按钮 → API → engine → executor → 持久化」整条链路真的跑完了一轮，
      // 只是不要求 LLM 调用成功。
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
          {
            // 本机 e2e 会继承用户真实 LLM 配置；端点不可达时 openai SDK 的重试
            // 风暴约 25s x 3 次尝试 ≈ 80s，last_run_at 在 finally 里才写入。
            // CI 是全新环境（无 LLM 配置）会立即报错，走得很快。150s 覆盖两者。
            timeout: 150_000,
            message: "立即运行后 last_run_at 应被写入（无论 LLM 执行成败）",
          },
        )
        .not.toBeNull();

      // 会话在调用 LLM 之前就已创建（executor.py:285 早于 299），
      // 因此这一条同样不依赖 LLM 成功。
      await expect
        .poll(
          async () => {
            const response = await api.get(`/api/workspaces/${workspace.workspaceId}`);
            if (!response.ok()) {
              return 0;
            }
            const body = (await response.json()) as {
              conversation_count?: number;
            };
            return body.conversation_count ?? 0;
          },
          {
            timeout: 150_000,
            message: "立即运行应新建一个自动任务会话",
          },
        )
        .toBeGreaterThan(1);
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
