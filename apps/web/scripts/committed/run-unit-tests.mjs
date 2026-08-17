/**
 * 单元测试 runner（跨平台）。
 *
 * 起因：package.json 原先写 `node --experimental-strip-types src/utils/__tests__/*.test.mjs`，
 * 依赖 shell 展开 glob。这在 Linux/macOS 的 bash 下可行，**在 Windows 下不展开**，
 * node 会把字面量 `src/utils/__tests__/*.test.mjs` 当文件名，报 MODULE_NOT_FOUND。
 * 后果是 CI（ubuntu-latest）绿、Windows 本地跑不了——「CI 绿但本地坏」的不对称。
 *
 * 这里改为在 Node 内部自己发现文件，不经过 shell，因此三端行为一致。
 *
 * 另注：`__tests__` 下的测试是**自执行断言脚本**（import 即跑断言，不用 node:test 的
 * describe/it），所以 runner 只需 await import()，抛异常即视为失败。也因此不能用
 * `node --test <dir>`——那个模式期望文件内有 test() 声明。
 * 新写的测试请用 vitest（.test.ts / .test.tsx，见 vitest.config.ts），本 runner 只
 * 负责这几个存量 .mjs 自执行脚本。
 *
 * ── 扫描范围：整个 src/ 递归，不要再改回单一目录 ──────────────────────
 * 2026-08-09 发现：原先固定扫 src/utils/__tests__/，而仓库里实际有 4 个 .test.mjs，
 * 另外两个在 src/components/layout/WorkspaceSidebar/__tests__/ 与 src/hooks/__tests__/，
 * 合计 21 个断言从未被执行过（手动跑，两个都是通过的——所以不是有人写坏了没管，
 * 而是压根没人跑过它们）。范围来自最初那条 glob，后来加测试的人没同步改。
 * 递归扫 src/ 之后，新增测试文件放哪个目录都会被自动发现，不需要再维护路径清单。
 */

import { readdir } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 路径基准说明：本文件位于 apps/web/scripts/committed/，需要上溯两级才到 apps/web。
// （放在 committed/ 是因为 apps/web/.gitignore 忽略 scripts/*.mjs，只有 committed/
// 下的脚本能入版本控制——CI 要调用它，不入版本控制等于 CI 里没有这个文件。）
const SRC_DIR = path.resolve(import.meta.dirname, "..", "..", "src");
const PATTERN = /\.test\.mjs$/;
const SKIP_DIRS = new Set(["node_modules", "dist", ".vite", "__snapshots__"]);

/** 递归收集 src/ 下所有 *.test.mjs，返回绝对路径数组。 */
async function collect(dir) {
  const found = [];
  const entries = await readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      found.push(...(await collect(full)));
    } else if (entry.isFile() && PATTERN.test(entry.name)) {
      found.push(full);
    }
  }
  return found;
}

const files = (await collect(SRC_DIR)).sort();

// 空结果视为失败而非通过。「没发现测试」和「测试都通过了」是两件事，
// 前者说明发现逻辑坏了（改了目录、改了后缀名），静默返回 0 会变成假绿。
if (files.length === 0) {
  console.error(`[test:unit] 在 ${SRC_DIR} 下递归未发现任何匹配 ${PATTERN} 的测试文件`);
  console.error("[test:unit] 这大概率是发现逻辑坏了，而不是真的没有测试");
  process.exit(1);
}

let failed = 0;
for (const full of files) {
  const rel = path.relative(SRC_DIR, full).replace(/\\/g, "/");
  try {
    // 必须用 pathToFileURL：Windows 下直接 import("C:\\...") 会被当成非法 URL scheme。
    await import(pathToFileURL(full).href);
    console.log(`  ✓ ${rel}`);
  } catch (err) {
    failed += 1;
    console.error(`  ✗ ${rel}`);
    console.error(err);
  }
}

console.log(`[test:unit] ${files.length - failed}/${files.length} 通过`);

// 用 exitCode 而非 process.exit()：后者会立刻终止进程，Windows 上会打断 libuv
// 尚未完成的句柄清理，触发 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`
// 并把退出码变成 127（掩盖真实的 0/1）。设 exitCode 让 node 自然退出。
process.exitCode = failed > 0 ? 1 : 0;
