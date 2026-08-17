import { expect, test } from "@playwright/test";

/**
 * 就绪门：lifecycle 全部用例的前置依赖。
 *
 * 为什么需要它——2026-08-11 实测到的竞态：
 * playwright 的 webServer 只探测一个 url，配置里给的是前端 13000。vite 启动快，
 * 后端 uvicorn 要慢约 3 秒。于是 playwright 认为「服务已就绪」并立刻开跑，
 * 第一个用例的 registerLifecycleUser 打到 13002 直接 ECONNREFUSED，612ms 失败：
 *
 *   [WebServer] http proxy error: /api/auth/session
 *   [WebServer] Error: connect ECONNREFUSED 127.0.0.1:13002
 *
 * 表现是「冷启动必失败，已有服务在跑时才通过」，属于测试基建自身不可靠，
 * 会把真实缺陷淹没在随机失败里。这里显式等两个端口都能服务再放行。
 */

// 默认值必须是后端的真实默认端口 13001（scripts/dev/cli.sh 的 BACKEND_PORT 缺省值）。
// 这里曾写死 13002——那是某次调试时 13001 被占、后端自动移位后的端口，端口状态一干净
// 它就等满超时（2026-08-13 实测复现）。端口切换场景由 run_lifecycle_playwright.sh
// 读取 .tmp/dev-ports.env 后 export PLAYWRIGHT_BACKEND_HEALTH_URL 覆盖。
const BACKEND_HEALTH_URL =
  process.env.PLAYWRIGHT_BACKEND_HEALTH_URL || "http://127.0.0.1:13001/health";
const READY_TIMEOUT_MS = Number(process.env.PLAYWRIGHT_READY_TIMEOUT_MS || 180_000);

test.describe("lifecycle 就绪门", () => {
  test.setTimeout(READY_TIMEOUT_MS + 30_000);

  test("后端与前端均可服务后才放行 lifecycle 用例", async ({ page, baseURL }) => {
    // 后端：直连 /health，它不在 /api 前缀下，不经 vite 代理。
    await expect
      .poll(
        async () => {
          try {
            const response = await page.request.get(BACKEND_HEALTH_URL, {
              timeout: 5_000,
            });
            return response.status();
          } catch {
            return 0;
          }
        },
        {
          timeout: READY_TIMEOUT_MS,
          intervals: [500, 1_000, 2_000],
          message: `后端未在 ${READY_TIMEOUT_MS}ms 内就绪：${BACKEND_HEALTH_URL}`,
        },
      )
      .toBe(200);

    // 前端：能真正拿到文档，而不只是端口被占。
    await expect
      .poll(
        async () => {
          try {
            const response = await page.request.get(baseURL ?? "http://127.0.0.1:13000", {
              timeout: 5_000,
            });
            return response.status();
          } catch {
            return 0;
          }
        },
        {
          timeout: 60_000,
          intervals: [500, 1_000, 2_000],
          message: "前端未就绪",
        },
      )
      .toBe(200);

    // 会话接口可用（走 vite 代理到后端），这一条正是原竞态失败的那个调用。
    await expect
      .poll(
        async () => {
          try {
            const response = await page.request.get("/api/auth/session", {
              timeout: 5_000,
            });
            // 未登录时可能是 401/200，只要不是连接错误即视为链路已通。
            return response.status() > 0 ? 1 : 0;
          } catch {
            return 0;
          }
        },
        {
          timeout: 60_000,
          intervals: [500, 1_000, 2_000],
          message: "/api/auth/session 代理链路未就绪（vite → 后端）",
        },
      )
      .toBe(1);
  });
});
