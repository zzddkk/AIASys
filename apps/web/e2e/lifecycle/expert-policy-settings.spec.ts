import { expect, test } from "@playwright/test";

import { createWorkspace, deleteWorkspace } from "./support";

test.describe("Expert policy settings", () => {
  test("sidebar collaboration experts entry opens settings dialog without route navigation", async ({
    page,
  }) => {
    const api = page.request;
    const workspace = await createWorkspace(api, {
      title: `协作专家弹窗回归-${Date.now()}`,
      mode: "analysis",
    });
    const initialGlobalPolicyResponse = await api.get("/api/experts/global/policy");
    const initialGlobalPolicy = (await initialGlobalPolicyResponse.json()) as {
      available_roles: Array<{
        role_id: string;
        catalog_visible: boolean;
        host_selectable: boolean;
        default_enabled: boolean;
      }>;
    };
    const initialReviewer = initialGlobalPolicy.available_roles.find(
      (role) => role.role_id === "reviewer",
    );
    const reviewerWasInstalled = Boolean(initialReviewer);
    if (!reviewerWasInstalled) {
      const installReviewerResponse = await api.post(
        "/api/experts/global/reviewer/enable",
        { data: { role_id: "reviewer" } },
      );
      expect(installReviewerResponse.ok()).toBeTruthy();
    }

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );

      await expect
        .poll(() => new URL(page.url()).searchParams.get("workspace_id"))
        .toBe(workspace.workspaceId);

      // 协作专家入口：点击侧边栏齿轮直接打开全局控制面板 -> 能力管理 -> 新建专家
      await page.getByTestId("sidebar-workspace-tools-menu-trigger").click();
      await expect(page.getByTestId("global-settings-dialog")).toBeVisible();
      await page.getByTestId("global-settings-nav-capabilities").click();
      await page.getByTestId("capability-panel-new-expert").click();

      await expect(
        page.getByTestId("collaboration-roles-settings-dialog"),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "协作专家管理", exact: true }),
      ).toBeVisible();
    } finally {
      if (reviewerWasInstalled) {
        const restoreReviewerResponse = await api.post(
          "/api/experts/global/reviewer/enable",
          { data: { role_id: "reviewer" } },
        );
        if (restoreReviewerResponse.ok() && initialReviewer) {
          await api.put("/api/experts/global/reviewer/visibility", {
            data: {
              catalog_visible: initialReviewer.catalog_visible,
              host_selectable: initialReviewer.host_selectable,
              default_enabled: initialReviewer.default_enabled,
            },
          });
        }
      } else {
        await api.delete("/api/experts/global/reviewer");
      }
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });

  test("global visibility switch updates role selectability via settings dialog", async ({
    page,
  }) => {
    // 入口链路较长（goto + 三级菜单 + 对话框渲染），默认 60s 不够。
    test.setTimeout(120_000);
    const api = page.request;
    const workspace = await createWorkspace(api, {
      title: `协作专家可见性回归-${Date.now()}`,
      mode: "analysis",
    });

    // 全局策略是用户级状态：先记录初始值，结束后恢复，避免污染其他用例。
    const initialPolicyResponse = await api.get("/api/experts/global/policy");
    const initialPolicy = (await initialPolicyResponse.json()) as {
      available_roles: Array<{
        role_id: string;
        catalog_visible: boolean;
        host_selectable: boolean;
        default_enabled: boolean;
      }>;
    };
    const initialCoder = initialPolicy.available_roles.find(
      (role) => role.role_id === "coder",
    );
    const coderWasInstalled = Boolean(initialCoder);
    if (!coderWasInstalled) {
      const enableResponse = await api.post("/api/experts/global/coder/enable", {
        data: { role_id: "coder" },
      });
      expect(enableResponse.ok()).toBeTruthy();
    } else if (initialCoder && !initialCoder.host_selectable) {
      const restore = await api.put("/api/experts/global/coder/visibility", {
        data: {
          catalog_visible: initialCoder.catalog_visible,
          host_selectable: true,
          default_enabled: initialCoder.default_enabled,
        },
      });
      expect(restore.ok()).toBeTruthy();
    }

    try {
      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        {
          waitUntil: "domcontentloaded",
        },
      );
      await expect
        .poll(() => new URL(page.url()).searchParams.get("workspace_id"))
        .toBe(workspace.workspaceId);

      // 入口与 :6 相同：侧边栏齿轮 -> 全局设置 -> 能力管理 -> 新建专家。
      await page.getByTestId("sidebar-workspace-tools-menu-trigger").click();
      await expect(page.getByTestId("global-settings-dialog")).toBeVisible();
      await page.getByTestId("global-settings-nav-capabilities").click();
      await page.getByTestId("capability-panel-new-expert").click();
      await expect(
        page.getByTestId("collaboration-roles-settings-dialog"),
      ).toBeVisible();

      // 默认 tab「我的协作专家」里每个角色卡片带可见性开关
      // （RoleListItem -> RoleVisibilityPopover）。
      const trigger = page.getByTestId("role-visibility-trigger-coder");
      await expect(trigger).toBeVisible();
      await trigger.click();

      const popover = page.getByTestId("role-visibility-popover-coder");
      await expect(popover).toBeVisible();
      const hostSelectable = popover.getByTestId(
        "role-visibility-host-selectable-coder",
      );
      // 直接点 Switch 本体。旧面板时代的 xpath=.. 点的是父容器，
      // 在 RoleVisibilityPopover 里父容器只是个 div，点了不动开关。
      await expect(hostSelectable).toBeChecked();
      await hostSelectable.locator("xpath=..").click();
      await expect(hostSelectable).not.toBeChecked();
      // 确认 popover 仍开着再点保存，避免点击落到下层角色行触发详情弹窗。
      await expect(popover).toBeVisible();

      const saveRequest = page.waitForResponse((response) => {
        return (
          response.url().includes("/api/experts/global/coder/visibility") &&
          response.request().method() === "PUT"
        );
      });
      await popover.getByTestId("role-visibility-save-coder").click();
      const saveResponse = await saveRequest;
      expect(saveResponse.ok()).toBeTruthy();

      // UI -> API 闭环：全局策略里 coder 不再可被主控选用。
      await expect
        .poll(async () => {
          const response = await api.get("/api/experts/global/policy");
          const payload = (await response.json()) as {
            available_roles: Array<{
              role_id: string;
              host_selectable: boolean;
            }>;
          };
          const coder = payload.available_roles.find(
            (role) => role.role_id === "coder",
          );
          return coder?.host_selectable;
        })
        .toBe(false);
    } finally {
      if (coderWasInstalled && initialCoder) {
        await api.put("/api/experts/global/coder/visibility", {
          data: {
            catalog_visible: initialCoder.catalog_visible,
            host_selectable: initialCoder.host_selectable,
            default_enabled: initialCoder.default_enabled,
          },
        });
      } else {
        await api.delete("/api/experts/global/coder");
      }
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });

});
