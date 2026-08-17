import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";

// e2e/manual/ 下那批脚本的专用配置。
//
// 为什么需要单独一份：playwright.lifecycle.config.ts 的 testDir 是 ./e2e/lifecycle，
// 而 playwright 会用 testDir 过滤命令行传入的路径参数。这批脚本此前放在 e2e/ 根层，
// 同样落在 testDir 之外——结果是它们既不会被套件自动跑到，手工指定文件也跑不起来
// （报 no tests found），只能靠临时改配置。有了这份配置，它们才有一个稳定入口。
//
// 跟 lifecycle 配置的差别只在三处，其余刻意保持一致，避免两套环境语义漂移：
//   1. testDir 指向 ./e2e/manual；
//   2. outputDir / 报告目录分开，不与回归套件的产物混在一起；
//   3. 不设 forbidOnly 之类的门禁——这批脚本本来就是给人跑的，不进 CI。
//
// 注意这批脚本零断言、以截图为输出，跑通不等于界面正确：结论要人看图才能得出。
// 详见 e2e/manual/README.md。
const devServerCommand =
  process.platform === "win32" ? "bash ./dev.sh" : "./dev.sh";

export default defineConfig({
  testDir: "./e2e/manual",
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: {
    timeout: 15_000,
  },
  outputDir: "./test-results/manual",
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "./playwright-report/manual" }],
  ],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: devServerCommand,
    cwd: repoRoot,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 240_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
