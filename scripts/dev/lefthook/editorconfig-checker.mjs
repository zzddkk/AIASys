#!/usr/bin/env node
/**
 * 跨平台 editorconfig-checker 包装器（lefthook pre-commit 用）。
 *
 * 为什么不直接在 lefthook.yml 里写 `npx editorconfig-checker`：
 *
 * 1. 版本解析会跑偏。这个包只声明在 apps/web/package.json（^6.1.1），而 lefthook 的
 *    工作目录是仓库根，根目录没有 node_modules/editorconfig-checker，于是 npx 回退到
 *    全局缓存里的旧包 v3.7.0。2026-08-09 实测：同样三个文件，apps/web 的 v6.1.1 判
 *    全部合格（退出码 0、零输出），而根目录 npx 解析到的 v3.7.0 判「检查未通过」。
 *    也就是说那条警告长期是假警报，跟文件内容无关。
 *    所以这里显式定位 apps/web/node_modules 里的包，不依赖 npx 的路径查找。
 *
 * 2. 不能 spawn .bin/editorconfig-checker.cmd。Node 在 CVE-2024-27980 的修复之后
 *    禁止不带 shell 直接 spawn .cmd/.bat，实测报 EINVAL。而加 shell:true 会让下面
 *    excludePattern 里的 `|`、`$`、`(` 被 shell 解释，等于开了注入口子。
 *    正确做法是绕过 .bin 包装，用当前 node 可执行文件直接跑包的 bin 入口
 *    （package.json 的 bin 字段指向 dist/index.js），参数按数组传递、不过 shell。
 *
 * 关于退出码：以前这里无条件 process.exit(0)，只把失败打印成警告。那等于该 hook 永远
 * 成功，是 lefthook.yml 纪律 2 点名的「假绿」在 JS 里的写法，已改为传播真实退出码。
 *
 * ── 为什么禁用 end-of-line 与 insert-final-newline 两项检查 ──────────────
 *
 * 本仓的行尾归 git 管：.gitattributes 里 `* text=auto`，库内一律存 LF（实测 HEAD 版本
 * 的 .editorconfig / README.md / lefthook.yml 全是纯 LF），Windows 上 checkout 后工作区
 * 转成 CRLF（实测 README.md 工作区 CRLF=165、裸 LF=0）。这是 git 的正常行为。
 *
 * 而 editorconfig-checker 检查的是工作区文件，于是在 Windows 上必然与 .editorconfig 的
 * `end_of_line = lf` 冲突——存量文件无一例外会报错。它的 final-newline 检查还会把 CRLF
 * 结尾误判成「no final newline」（实测：只禁 end-of-line 时 README.md 仍剩这一条）。
 * 两项都禁用后，README.md / .editorconfig / pyproject.toml 全部放行，而真正有价值的检查
 * 仍然生效（已实测：尾随空格、tab 缩进都能被检出）。
 *
 * 换句话说，这两项不是「放宽标准」，是把重复且在本平台必然误报的检查交回 git 负责，
 * editorconfig 只保留 git 管不到的部分：缩进风格与宽度、尾随空格、字符集、行长。
 *
 * 注意参数必须放在文件名之前。实测放到文件名之后，Go 二进制会把它当成待检查文件并
 * panic（open -disable-end-of-line: The system cannot find the file specified）。
 *
 * ── 为什么只对 Python 额外禁用 indent-size ──────────────────────────────
 *
 * indent-size 检查只看行首空格数是否为 indent_size 的整数倍，它不理解字符串字面量，
 * 于是 Python 的 docstring 示意图与括号续行对齐必然误报。2026-08-09 实测：
 * apps/backend/app/ 下前 150 个 py 文件里就有 25 处被判 "want multiple of 4"，
 * test_hermes_env_race.py 那段用 6 空格画的并发时序说明也在其中——那是文档内容，
 * PEP 8 不管，ruff format 也不动。
 *
 * 这类存量违规平时不出声，只在文件恰好进入 staged 集合时才拦人，属于随机摩擦。而
 * Python 缩进本来就有权威：ruff format 保证 PEP 8 的 4 空格，两个工具同时管必然打架，
 * 且 ruff 更准。所以 py 侧禁掉 indent-size，把判定权交给 ruff。
 *
 * 但不能全局禁用。实测 apps/web 既没有 prettier 配置也没有 eslint indent 规则，
 * editorconfig 的 indent-size 是前端缩进的唯一守护，一起禁掉就是真的放宽标准。
 * 因此按扩展名分两批调用：.py 批加 -disable-indent-size，其余批保持严格。
 * 注意 py 侧仍然检查 indent_style（tab/空格混用）、尾随空格、字符集与行长，
 * 只是不再数空格个数。
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
const WEB_MODULES = path.join(REPO_ROOT, "apps", "web", "node_modules");
const PKG_DIR = path.join(WEB_MODULES, "editorconfig-checker");
const ENTRY = path.join(PKG_DIR, "dist", "index.js");

const excludePattern = "\\.git|node_modules|\\.venv|dist|\\.pytest_cache|\\.ruff_cache|\\.(png|jpe?g|gif|svg|ico|icns|webp|avif|mp4|mp3|wav|zip|gz|tar|dmg|exe|dll|so|dylib|pyd)$";

// 见上方说明：行尾与末尾换行由 git 的 text=auto 保证，这里不重复检查。
const DISABLED_CHECKS = ["-disable-end-of-line", "-disable-insert-final-newline"];
// 仅 Python：缩进宽度由 ruff format 负责，见上方说明。
const PY_DISABLED_CHECKS = [...DISABLED_CHECKS, "-disable-indent-size"];

const files = process.argv.slice(2).filter(Boolean);

if (files.length === 0) {
  process.exit(0);
}

if (!existsSync(WEB_MODULES)) {
  console.warn("[editorconfig] 跳过：apps/web/node_modules 不存在（尚未安装依赖）。");
  process.exit(0);
}

if (!existsSync(ENTRY)) {
  console.error("[editorconfig] 检查工具缺失：" + ENTRY);
  console.error("[editorconfig] 依赖装漏了，跑一次：cd apps/web && npm ci");
  console.error("[editorconfig] 注意本机 NODE_ENV=production 会让 npm 自动 omit=dev，");
  console.error("[editorconfig] apps/web/.npmrc 已用 include=dev 压住，但需重装一次才生效。");
  process.exit(1);
}

/** 跑一批文件，返回退出码；空批直接算通过。 */
function check(batch, disabledChecks) {
  if (batch.length === 0) {
    return 0;
  }
  const result = spawnSync(
    process.execPath,
    [ENTRY, "-exclude", excludePattern, ...disabledChecks, ...batch],
    { stdio: "inherit", windowsHide: true, cwd: REPO_ROOT },
  );
  if (result.error) {
    console.error("[editorconfig] 无法执行检查工具：" + result.error.message);
    return 1;
  }
  return result.status ?? 1;
}

const pyFiles = files.filter((f) => f.toLowerCase().endsWith(".py"));
const otherFiles = files.filter((f) => !f.toLowerCase().endsWith(".py"));

// 两批都要跑完再决定退出码，不能短路——否则前一批失败时后一批的问题被藏起来，
// 提交者修完第一批才看到第二批，等于把一次反馈拆成两轮。
const pyStatus = check(pyFiles, PY_DISABLED_CHECKS);
const otherStatus = check(otherFiles, DISABLED_CHECKS);

process.exit(pyStatus || otherStatus);
