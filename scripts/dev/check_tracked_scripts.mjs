#!/usr/bin/env node
/**
 * 检查 CI 与 npm scripts 引用的本地脚本文件都在版本库里。
 *
 * 存在理由（2026-08-16 实测）：设计 token 守卫脚本被放在 apps/web/scripts/ 根层，
 * 而该目录的 .gitignore 有 `scripts/*.mjs`——文件从未进版本库。本地五道校验全绿
 * （工作区有这个文件），推到 CI 直接 `Cannot find module`。
 *
 * 这类失效有个特点：**本地怎么测都测不出来**。本地测的是工作区，CI 测的是版本库，
 * 在「文件是否存在」这一点上两者不等价。所以它只能靠静态检查拦，
 * 而且检查的判据必须是「git 跟踪状态」，不能是「文件存在」——后者恒真。
 *
 * 覆盖两层引用：
 *   1. .github/workflows/ 的 run 里直接写的脚本路径（含 working-directory 换算）；
 *   2. 各 package.json 的 scripts 里引用的本地文件（CI 多数走 npm run，
 *      这层是上面那次事故的实际路径，只查第 1 层会漏掉）。
 *
 * 用法：
 *   node scripts/dev/check_tracked_scripts.mjs             # 检查
 *   node scripts/dev/check_tracked_scripts.mjs --self-test # 自证判定逻辑有效
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, posix, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

/** 从一段 shell 文本里抽出「解释器 + 本地脚本路径」的引用 */
export function extractScriptRefs(text) {
  const refs = [];
  const re = /(?:^|[\s&|;(])(?:node|python3?|bash|sh)\s+([./\w-]+\.(?:mjs|cjs|js|py|sh))/g;
  let m;
  while ((m = re.exec(text)) !== null) refs.push(m[1]);
  return refs;
}

/**
 * 从一行里抽出 `cd <dir>`，用于跟踪 run 块内的目录切换。
 *
 * 必须处理 cd：本仓的 desktop-pr-check.yml 用的是 `cd apps/desktop` 而不是
 * working-directory，不跟踪就会把 `node scripts/smoke-test.cjs` 解析到仓库根，
 * 报出一个不存在的路径。误报和漏报一样致命——它会诱使人加白名单绕过，
 * 最后这道检查就只剩装饰（2026-08-16 本脚本首次运行即产生了这个误报）。
 */
export function extractCd(line) {
  const m = /(?:^|[\s&|;])cd\s+([./\w-]+)/.exec(line);
  return m ? m[1] : null;
}

/** 该路径是否被 git 跟踪。判据用 ls-files 而不是 existsSync——后者恒真，测不出本次事故。 */
function isTracked(absPath) {
  const rel = relative(REPO_ROOT, absPath).split("\\").join("/");
  try {
    execFileSync("git", ["ls-files", "--error-unmatch", "--", rel], {
      cwd: REPO_ROOT,
      stdio: "pipe",
    });
    return true;
  } catch {
    return false;
  }
}

function collectFromWorkflows() {
  const dir = join(REPO_ROOT, ".github", "workflows");
  const out = [];
  if (!existsSync(dir)) return out;

  for (const name of readdirSync(dir)) {
    if (!/\.ya?ml$/.test(name)) continue;
    const text = readFileSync(join(dir, name), "utf8");

    // working-directory 与 run 块内的 cd 共同决定相对路径的基准。逐行扫描：
    // wdBase 记住最近一次 working-directory 声明，cwd 在此基础上跟随 cd 移动，
    // 遇到新 step（以 `- ` 开头的行）时把 cwd 复位回 wdBase。
    // 不做完整 YAML 解析是因为这里只需要「基准目录 + run 文本」两样东西。
    let wdBase = REPO_ROOT;
    let cwd = REPO_ROOT;
    for (const line of text.split(/\r?\n/)) {
      if (/^\s*-\s/.test(line)) cwd = wdBase;

      const wd = /working-directory:\s*(\S+)/.exec(line);
      if (wd) {
        wdBase = resolve(REPO_ROOT, wd[1]);
        cwd = wdBase;
      }

      const cd = extractCd(line);
      if (cd) cwd = resolve(cwd, cd);

      for (const ref of extractScriptRefs(line)) {
        out.push({ source: `.github/workflows/${name}`, abs: resolve(cwd, ref), ref });
      }
    }
  }
  return out;
}

function collectFromPackageJsons() {
  const out = [];
  const candidates = [
    "package.json",
    "apps/web/package.json",
    "apps/desktop/package.json",
    "apps/backend/package.json",
  ];

  for (const relPath of candidates) {
    const abs = join(REPO_ROOT, relPath);
    if (!existsSync(abs)) continue;
    const pkg = JSON.parse(readFileSync(abs, "utf8"));
    const base = dirname(abs);
    for (const [scriptName, body] of Object.entries(pkg.scripts ?? {})) {
      for (const ref of extractScriptRefs(String(body))) {
        out.push({ source: `${relPath} → scripts.${scriptName}`, abs: resolve(base, ref), ref });
      }
    }
  }
  return out;
}

function selfTest() {
  console.log("自证：验证判定逻辑能识别未跟踪引用与提取失败");
  let ok = true;

  // 1. 能从各种 shell 写法里提取路径
  const extracted = extractScriptRefs(
    "node scripts/a.mjs && python3 ../../scripts/b.py; bash x/c.sh | tail -1",
  );
  const okExtract =
    extracted.length === 3 &&
    extracted.includes("scripts/a.mjs") &&
    extracted.includes("../../scripts/b.py") &&
    extracted.includes("x/c.sh");
  ok = ok && okExtract;
  console.log(`  ${okExtract ? "✓" : "✗"} 从 shell 文本提取脚本路径（实得 ${extracted.length}/3）`);

  // 2. 能跟踪 run 块里的 cd（漏这一条会误报，见 extractCd 头注释）
  const cdCases = [
    ["          cd apps/desktop", "apps/desktop"],
    ["  cd apps/web && npm ci", "apps/web"],
    ["          npm run build", null],
  ];
  let okCd = true;
  for (const [line, expect] of cdCases) {
    if (extractCd(line) !== expect) okCd = false;
  }
  ok = ok && okCd;
  console.log(`  ${okCd ? "✓" : "✗"} 从 run 文本跟踪 cd 目录切换`);

  // 3. 已跟踪文件判 true——用本文件自己当样本
  const selfTracked = isTracked(fileURLToPath(import.meta.url));
  // 本文件首次运行时可能尚未 git add，那时这一条不该算失败，只提示
  console.log(
    `  ${selfTracked ? "✓" : "!"} 已跟踪文件判定为 tracked${selfTracked ? "" : "（本文件还没 git add，跳过）"}`,
  );

  // 3. 不存在的路径必须判 false（这是拦本次事故的关键分支）
  const ghost = isTracked(join(REPO_ROOT, "scripts", "__definitely_not_tracked__.mjs"));
  ok = ok && ghost === false;
  console.log(`  ${ghost === false ? "✓" : "✗"} 未跟踪路径判定为 untracked`);

  console.log(ok ? "自证通过：判定逻辑有效" : "自证失败：判定逻辑不可信");
  return ok ? 0 : 1;
}

function main() {
  if (process.argv.includes("--self-test")) process.exit(selfTest());

  const refs = [...collectFromWorkflows(), ...collectFromPackageJsons()];
  const bad = [];
  const seen = new Set();

  for (const r of refs) {
    const key = r.abs;
    if (seen.has(key)) continue;
    seen.add(key);

    // 只管仓库内的路径；node_modules 里的第三方脚本不该被跟踪
    const rel = relative(REPO_ROOT, r.abs);
    if (rel.startsWith("..") || rel.includes("node_modules")) continue;

    if (!isTracked(r.abs)) {
      bad.push({ ...r, rel: rel.split("\\").join(posix.sep) });
    }
  }

  console.log(`已检查 ${seen.size} 个被引用的仓库内脚本路径`);

  if (bad.length === 0) {
    console.log("  ✓ 全部在版本库里");
    process.exit(0);
  }

  console.log("");
  console.log("  ❌ 以下脚本被 CI 或 npm scripts 引用，但不在版本库里：");
  console.log("");
  for (const b of bad) {
    console.log(`    ${b.rel}`);
    console.log(`      引用来源：${b.source}`);
    console.log(`      本地可能存在${existsSync(b.abs) ? "（确实存在——所以本地全绿而 CI 会红）" : ""}`);
  }
  console.log("");
  console.log("  多半是被某层 .gitignore 忽略了。查：git check-ignore -v <路径>");
  console.log("  apps/web/scripts/*.mjs 就是一例，有持久价值的脚本要放 scripts/committed/。");
  console.log("");
  process.exit(1);
}

main();
