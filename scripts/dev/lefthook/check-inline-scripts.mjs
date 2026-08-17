#!/usr/bin/env node
/**
 * 强制 lefthook.yml 纪律 6：run 里只允许单行命令，多行脚本必须放进 .sh 文件。
 *
 * 为什么需要静态强制而不是靠 review：内联多行脚本的失效是**静默**的。
 * 2026-08-16 实测，lefthook 2.0.2 在 Windows 下构造 `sh.exe -c "<run>"` 时
 * 不转义脚本内的双引号，Windows 的命令行解析于是在双引号处进出「引号态」，
 * 一旦在引号外遇到空格就截断参数——脚本从那里起整段丢失。
 *
 * 最小复现：
 *   run: |          期望输出    实得
 *   echo "A1 x y"   A1 x y      A1        ← 双引号内有空格，被截断
 *   echo 'D1 x y'   D1 x y      D1 x y    ← 单引号安全
 *
 * 代价是实打实付过的：frontend-lint 内联过一句
 *   echo "改动 $count 个文件，超过单次传参阈值，改跑全量 lint"
 * 它是脚本里第 7 个双引号（奇数 → 引号外状态），紧随其后的空格把脚本截断在
 * `echo 改动` 处，if 失去 fi。该 hook 因此恒定 exit 0：含 parse error 的文件
 * 被直接放过，lefthook 显示 ✓、耗时 0.16 秒，而同一份文件直接跑 eslint 退出 1。
 *
 * 是否触发取决于双引号的奇偶与空格位置——这不是人能靠肉眼稳定判断的性质，
 * 所以整类禁掉，不做个案判断。
 *
 * 判据（两条，都可数）：
 *   1. run 不得使用块标量（`run: |` / `run: >`）——多行脚本的唯一写法；
 *   2. run 的单行值不得含双引号。
 *
 * 为什么用文本扫描而不是 YAML 解析：本检查要禁的正是「写成块标量」这个字面形态，
 * 文本层判定与意图一一对应；且不引入 yaml 依赖（js-yaml 在本仓只是 eslint 的传递
 * 依赖，随时可能因升级消失，那会让本检查悄悄退化成「装不上就跳过」）。
 *
 * 用法：
 *   node scripts/dev/lefthook/check-inline-scripts.mjs             # 检查
 *   node scripts/dev/lefthook/check-inline-scripts.mjs --self-test # 自证判定逻辑有效
 *
 * 自证喂三份构造内容（合规 / 块标量 / 单行带双引号），要求判定分别是
 * 放过 / 拦下 / 拦下。少了这一步，本脚本自己也可能变成恒绿检查。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..", "..");

/** 允许内联多行的 command 名（当前无；加入需在此写明豁免理由） */
const ALLOWED = new Set();

/**
 * 扫描 lefthook 配置文本，返回违规列表。
 *
 * 只认两种 run 写法：
 *   run: <单行命令>
 *   run: |            ← 块标量，违规
 * command 名取「缩进比 run 少、且以冒号结尾」的最近一行。
 */
export function findViolations(text) {
  const lines = text.split(/\r?\n/);
  const violations = [];
  let currentCommand = "unknown";

  for (const line of lines) {
    if (/^\s*#/.test(line)) continue;

    // command 名：4 空格缩进 + 名字 + 冒号，且行尾无值
    const nameMatch = /^ {4}([A-Za-z0-9_-]+):\s*$/.exec(line);
    if (nameMatch) {
      currentCommand = nameMatch[1];
      continue;
    }

    const runMatch = /^\s*run:\s*(.*)$/.exec(line);
    if (!runMatch) continue;
    if (ALLOWED.has(currentCommand)) continue;

    const value = runMatch[1].trim();
    if (value === "|" || value === ">" || value === "|-" || value === ">-") {
      violations.push({
        command: currentCommand,
        why: `run 用了块标量（${value}），多行脚本必须收进 scripts/dev/lefthook/*.sh`,
      });
      continue;
    }
    if (value.includes('"')) {
      violations.push({
        command: currentCommand,
        why: "run 含双引号，Windows 下会被截断，改用 .sh 文件承载",
      });
    }
  }

  return violations;
}

function selfTest() {
  const good = [
    "pre-commit:",
    "  commands:",
    "    fine:",
    "      run: bash scripts/dev/lefthook/frontend-lint.sh",
  ].join("\n");

  const blockScalar = [
    "pre-commit:",
    "  commands:",
    "    bad:",
    "      run: |",
    "        files=$(git diff --cached --name-only)",
    "        echo done",
  ].join("\n");

  const quoted = [
    "pre-commit:",
    "  commands:",
    "    bad:",
    '      run: sh -c "echo hello world"',
  ].join("\n");

  const cases = [
    { name: "合规配置 → 放过", text: good, expectViolation: false },
    { name: "块标量多行 → 拦下", text: blockScalar, expectViolation: true },
    { name: "单行含双引号 → 拦下", text: quoted, expectViolation: true },
  ];

  console.log("自证：验证判定逻辑能分辨合规与两类违规");
  let ok = true;
  for (const c of cases) {
    const hit = findViolations(c.text).length > 0;
    const passed = hit === c.expectViolation;
    ok = ok && passed;
    console.log(`  ${passed ? "✓" : "✗"} ${c.name}${passed ? "" : "（判定逻辑失效）"}`);
  }

  console.log(ok ? "自证通过：判定逻辑有效" : "自证失败：判定逻辑不可信，本检查的结果无意义");
  return ok ? 0 : 1;
}

function main() {
  if (process.argv.includes("--self-test")) {
    process.exit(selfTest());
  }

  const path = join(REPO_ROOT, "lefthook.yml");
  const violations = findViolations(readFileSync(path, "utf8"));

  if (violations.length === 0) {
    console.log("  ✓ lefthook.yml 无内联多行脚本");
    process.exit(0);
  }

  console.log("");
  console.log("  ❌ lefthook.yml 违反纪律 6（见该文件头注释）");
  console.log("  内联多行脚本在 Windows 下会被静默截断，导致 hook 恒定通过。");
  console.log("");
  for (const v of violations) {
    console.log(`    [${v.command}] ${v.why}`);
  }
  console.log("");
  console.log("  修法：把逻辑移进 scripts/dev/lefthook/<name>.sh，run 只留一行 bash 调用。");
  console.log("");
  process.exit(1);
}

main();
