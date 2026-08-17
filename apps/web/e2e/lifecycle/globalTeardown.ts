/**
 * lifecycle e2e 的 globalTeardown：直接 npx（CI）路径下强杀 dev 服务器。
 *
 * 背景：playwright 的 webServer 只 spawn `bash ./dev.sh`，vite/uvicorn 是它的
 * 子孙进程；测试全部结束后 playwright 等待 webServer 退出，而 Windows 上
 * 子孙进程不随 bash 退出，于是 runner 空转——2026-08-13 CI 实测两次：
 * 58 条用例全部跑完后挂起约 55 分钟，直到 job 超时强杀才打出总结。
 * 脚本路径（run_lifecycle_playwright.sh）由外层脚本负责收尾，不受影响。
 *
 * globalTeardown 先于 playwright 的 webServer 关停逻辑执行，在这里按端口
 * 杀掉服务进程，webServer 等待随即解除。
 *
 * 只在 CI 环境执行：本地手动起 dev 服务器时 webServer 走复用，
 * 按端口杀会误伤用户自己的进程。
 */
import { execSync } from "node:child_process";

const LIFECYCLE_PORTS = [13000, 13001];

export default async function lifecycleGlobalTeardown(): Promise<void> {
  if (!process.env.CI || process.platform !== "win32") {
    return;
  }
  for (const port of LIFECYCLE_PORTS) {
    let pids: string[] = [];
    try {
      const out = execSync(
        `powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"`,
        { encoding: "utf-8" },
      );
      pids = out.split(/\s+/).filter(Boolean);
    } catch {
      continue; // 端口无监听
    }
    for (const pid of pids) {
      try {
        process.kill(Number(pid));
      } catch {
        // 进程已退出
      }
    }
  }
}
