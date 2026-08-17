import { expect, test } from "@playwright/test";

import {
  addSessionMessage,
  createWorkspace,
  deleteWorkspace,
  registerLifecycleUser,
} from "../lifecycle/support";

// 已退役的工作区级专家策略 e2e，不是可运行的回归测试。
//
// 退役原因（2026-08-12 实测）：这两个用例验证的是「工作区级专家策略 UI」——
// 侧边栏「专家协作节点」视图里的 workspace-expert-policy-summary /
// workspace-expert-policy-panel / workspace-expert-role-toggle-* 面板。
// 该面板组件 WorkspaceCollaborationPolicyPanel.tsx 仍在仓库里，但已无任何文件
// import 它（死代码）；v0.4.2-beta.1（PR #49）把入口换成了全局「协作专家管理」
// 对话框（CollaborationRolesSettingsDialog），且对话框的 workspaceId prop 被有意
// 弃用（解构为 _workspaceId，内部硬编码 scope="global"）。也就是说工作区级策略
// 的 UI 被上游有意下架，不是 spec 腐烂。
//
// 行为本身仍有覆盖：后端 tests/test_expert_policy_routes.py 有 7 条工作区策略
// 路由测试（含可见性继承、不可选角色拒绝）。可见性开关的 UI 链路改由
// lifecycle 侧的 global 版用例覆盖（同一文件内
// "global visibility switch updates role selectability"）。
//
// 若将来工作区级策略 UI 回归，把下面两个用例按新 UI 重写后搬回 lifecycle。

test.describe("Expert policy workspace scope (retired)", () => {
  test("workspace experts catalog and workspace collaboration policy stay connected", async ({
    page,
  }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `协作专家回归-${Date.now()}`,
      mode: "analysis",
    });

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/workspaces/${workspace.workspaceId}/experts`,
          );
          const payload = (await response.json()) as {
            roles: Array<{ role_id: string }>;
          };
          return payload.roles.map((role) => role.role_id).join(",");
        })
        .toContain("data_analyst");

      await expect
        .poll(() => {
          return new URL(page.url()).searchParams.get("session_id");
        })
        .toBe(workspace.currentConversationId);

      const initialPolicyResponse = await api.put(
        `/api/workspaces/${workspace.workspaceId}/experts/policy`,
        {
          data: {
            enabled_role_ids: ["data_analyst", "researcher"],
          },
        },
      );
      expect(initialPolicyResponse.ok()).toBeTruthy();

      await page.getByRole("button", { name: "专家协作节点", exact: true }).click();

      const policySummary = page.getByTestId("workspace-expert-policy-summary");
      await expect(policySummary).toBeVisible();
      await expect(policySummary).toHaveAttribute(
        "data-workspace-id",
        workspace.workspaceId,
      );
      await policySummary.getByTestId("open-workspace-collaboration-settings").click();

      const policySurface = page.getByTestId("workspace-expert-policy-panel");
      await expect(
        policySurface,
      ).toBeVisible();
      await expect(policySurface).toHaveAttribute(
        "data-workspace-id",
        workspace.workspaceId,
      );

      await expect(
        page.getByRole("button", { name: "工作区协作配置", exact: true }),
      ).toBeVisible();

      await policySurface.getByRole("tab", { name: "协作专家" }).click();
      const researcherToggle = policySurface.getByTestId(
        "workspace-expert-role-toggle-researcher",
      );
      await researcherToggle.click();
      await expect(researcherToggle).toHaveAttribute("data-state", "unchecked");
      await policySurface.getByRole("tab", { name: "工具权限" }).click();
      const dataAnalystNotebookOutputTool = policySurface.getByTestId(
        "workspace-expert-role-tool-toggle-data_analyst-ReadNotebookOutputsTool",
      );
      await dataAnalystNotebookOutputTool.click({ force: true });
      await expect(dataAnalystNotebookOutputTool).toHaveAttribute("data-state", "unchecked");

      const saveRequest = page.waitForResponse((response) => {
        return (
          response
            .url()
            .includes(`/api/workspaces/${workspace.workspaceId}/experts/policy`) &&
          response.request().method() === "PUT"
        );
      });
      await policySurface.getByTestId("workspace-expert-policy-save").click();
      const saveResponse = await saveRequest;
      expect(saveResponse.ok()).toBeTruthy();

      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/workspaces/${workspace.workspaceId}/experts/policy`,
          );
          const payload = (await response.json()) as {
            policy_mode: string;
            configured_enabled_role_ids: string[] | null;
            configured_role_tool_ids: Record<string, string[]> | null;
          };
          return JSON.stringify(payload);
        })
        .toContain('"policy_mode":"workspace"');
      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/workspaces/${workspace.workspaceId}/experts/policy`,
          );
          const payload = (await response.json()) as {
            configured_enabled_role_ids: string[] | null;
          };
          return JSON.stringify(payload.configured_enabled_role_ids || []);
        })
        .not.toContain("researcher");
      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/workspaces/${workspace.workspaceId}/experts/policy`,
          );
          const payload = (await response.json()) as {
            configured_role_tool_ids: Record<string, string[]> | null;
          };
          return JSON.stringify(payload.configured_role_tool_ids || {});
        })
        .not.toContain("ReadNotebookOutputsTool");

      await page.reload({ waitUntil: "domcontentloaded" });
      await page.getByRole("button", { name: "专家协作节点", exact: true }).click();
      await page
        .getByTestId("workspace-expert-policy-summary")
        .getByTestId("open-workspace-collaboration-settings")
        .click();

      const reloadedPolicySurface = page.getByTestId("workspace-expert-policy-panel");
      await expect(
        reloadedPolicySurface,
      ).toBeVisible();
      await reloadedPolicySurface.getByRole("tab", { name: "协作专家" }).click();
      await expect(
        reloadedPolicySurface
          .getByTestId("workspace-expert-role-toggle-researcher"),
      ).toHaveAttribute("data-state", "unchecked");
      await reloadedPolicySurface.getByRole("tab", { name: "工具权限" }).click();
      await expect(
        reloadedPolicySurface
          .getByTestId("workspace-expert-role-tool-toggle-data_analyst-ReadNotebookOutputsTool"),
      ).toHaveAttribute("data-state", "unchecked");

      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", { name: "协作节点", exact: true }),
      ).toBeVisible();
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
  test("workspace visibility switch hides non selectable roles from workspace policy", async ({
    page,
  }) => {
    const api = page.request;
    await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `协作专家可见性回归-${Date.now()}`,
      mode: "analysis",
    });

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      const installResponse = await api.post(
        `/api/workspaces/${workspace.workspaceId}/experts/coder/enable`,
        {
          data: { role_id: "coder" },
        },
      );
      expect(installResponse.ok()).toBeTruthy();
      const installReviewerResponse = await api.post(
        `/api/workspaces/${workspace.workspaceId}/experts/reviewer/enable`,
        {
          data: { role_id: "reviewer" },
        },
      );
      expect(installReviewerResponse.ok()).toBeTruthy();

      await page.getByRole("button", { name: "专家协作节点", exact: true }).click();
      await expect(page.getByTestId("role-visibility-trigger-coder")).toBeVisible();
      await page.getByTestId("role-visibility-trigger-coder").click();

      const popover = page.getByTestId("role-visibility-popover-coder");
      await expect(popover).toBeVisible();
      const hostSelectable = popover.getByTestId(
        "role-visibility-host-selectable-coder",
      );
      await expect(hostSelectable).toBeChecked();
      await hostSelectable.locator("xpath=..").click();
      await expect(hostSelectable).not.toBeChecked();

      const saveRequest = page.waitForResponse((response) => {
        return (
          response.url().includes(
            `/api/workspaces/${workspace.workspaceId}/experts/coder/visibility`,
          ) && response.request().method() === "PUT"
        );
      });
      await popover.getByTestId("role-visibility-save-coder").click();
      const saveResponse = await saveRequest;
      expect(saveResponse.ok()).toBeTruthy();

      await expect
        .poll(async () => {
          const response = await api.get(
            `/api/workspaces/${workspace.workspaceId}/experts`,
          );
          const payload = (await response.json()) as {
            roles: Array<{
              role_id: string;
              host_selectable: boolean;
              default_enabled: boolean;
            }>;
          };
          const coder = payload.roles.find((role) => role.role_id === "coder");
          return `${coder?.host_selectable}:${coder?.default_enabled}`;
        })
        .toBe("false:false");

      await page
        .getByTestId("workspace-expert-policy-summary")
        .getByTestId("open-workspace-collaboration-settings")
        .click();

      const policySurface = page.getByTestId("workspace-expert-policy-panel");
      await expect(policySurface).toBeVisible();
      await policySurface.getByRole("tab", { name: "协作专家" }).click();
      await expect(
        policySurface.getByTestId("workspace-expert-role-toggle-coder"),
      ).toHaveCount(0);
      await expect(
        policySurface.getByTestId("workspace-expert-role-toggle-reviewer"),
      ).toBeVisible();

      const rejected = await api.put(
        `/api/workspaces/${workspace.workspaceId}/experts/policy`,
        {
          data: {
            enabled_role_ids: ["coder"],
          },
        },
      );
      expect(rejected.status()).toBe(400);
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
