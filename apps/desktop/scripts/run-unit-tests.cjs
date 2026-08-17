"use strict";

/**
 * desktop 单元测试 runner（跨平台、递归发现）。
 *
 * 起因是 package.json 原先写 `node --test src/__tests__/*.test.cjs`，这有两个坑，
 * 而它们都已经在本仓库的 web 侧真实发生过（见 apps/web/scripts/committed/run-unit-tests.mjs
 * 与 .github/workflows/ci.yml 里的注释）：
 *
 * 1. glob 依赖 shell 展开。CI 跑在 ubuntu-latest（bash）能展开，Windows cmd/PowerShell
 *    下不展开，node 会把字面量当路径。主力开发在 Windows，于是形成「CI 绿、本地跑不了」
 *    的不对称。
 *
 * 2. 范围写死在单层目录。`src/__tests__/*.test.cjs` 只匹配一层，任何放进子目录的测试
 *    文件都不会被执行——而这不会报错，只会静默少跑。web 侧正是这样让 21 个断言从上线
 *    起从未执行过（手动跑其实全通过，纯粹是没人跑）。
 *
 * 所以这里在 Node 内部递归发现，不经过 shell；范围是整个 src/，新增测试放哪层都能被捡到。
 *
 * 与 web 侧 runner 的一个区别：desktop 的测试是 node:test 声明式（test()/assert），
 * 不是自执行断言脚本，所以这里把文件列表交给 `node --test` 执行，而不是 await import()。
 */

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SRC_DIR = path.resolve(
  // 仅供测试注入：需要验证「空目录必须 exit 1」这条防线本身是否有效，
  // 而这条防线的价值实测过——`node --test <无匹配的 glob>` 的退出码是 0（假绿），
  // 改错后缀或目录名不会有任何人察觉。
  process.env.AIASYS_DESKTOP_TEST_SRC_DIR || path.join(__dirname, "..", "src")
);
const PATTERN = /\.test\.cjs$/;
const SKIP_DIRS = new Set(["node_modules", "dist", ".dist", "dist-temp", "__snapshots__"]);

/** 递归收集 dir 下所有匹配 PATTERN 的文件，返回绝对路径数组。 */
function collect(dir) {
  const found = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return found;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      found.push(...collect(full));
    } else if (entry.isFile() && PATTERN.test(entry.name)) {
      found.push(full);
    }
  }
  return found;
}

function main() {
  const files = collect(SRC_DIR).sort();

  // 空结果按失败处理。「没发现测试」和「测试全通过」是两件不同的事，
  // 静默 exit 0 会把发现逻辑坏掉伪装成绿灯——这正是最难察觉的假绿形态。
  if (files.length === 0) {
    console.error(`[test:unit] 在 ${SRC_DIR} 下递归未发现任何匹配 ${PATTERN} 的测试文件`);
    console.error("[test:unit] 这大概率是发现逻辑坏了（改了后缀/目录），而不是真的没有测试");
    process.exit(1);
  }

  console.log(`[test:unit] 递归发现 ${files.length} 个测试文件:`);
  for (const f of files) {
    console.log(`  - ${path.relative(path.join(SRC_DIR, ".."), f).replace(/\\/g, "/")}`);
  }

  const result = spawnSync(process.execPath, ["--test", ...files], {
    stdio: "inherit",
    windowsHide: true,
  });

  if (result.error) {
    console.error(`[test:unit] 启动 node --test 失败: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

module.exports = { collect, SRC_DIR, PATTERN, SKIP_DIRS };

if (require.main === module) {
  main();
}
