#!/usr/bin/env node
/**
 * 设计 token 守卫。
 *
 * 存在理由（2026-08-16 实测数据）：本项目此前只有颜色 token，没有尺寸/排版规范，
 * 于是每个组件自己拍数值——全站硬编码字号 881 处（10px 272 / 11px 503 / 12px 71
 * / 13px 12 / 9px 16 / 15px 2 / 18px 1），控件高度 h-7 142 处、h-8 227 处。
 * 视觉不一致不是「有人不小心写错」，是缺少可执行约束，靠人工 review 拦不住。
 *
 * 为什么用 TS AST 而不是逐行正则：第一版守卫用的是行级正则，报 23 处原语高度覆写；
 * 同一时刻 AST 扫描报 243 处。差的那 220 处全是 JSX 属性跨多行的写法——
 *   <Button
 *     variant="ghost"
 *     className="h-8 ..."
 *   />
 * 行正则永远看不到 tagName 和 className 在同一个元素里。守卫漏检比没有守卫更危险，
 * 因为它会给出「已经干净了」的假信号。
 *
 * 三类规则：
 *   R1 硬编码字号：一律走 text-nano/micro/caption/body 或 Tailwind 内置档
 *   R2 原语高度硬编码：Button/Input/SelectTrigger 用 size prop
 *   R3 DialogContent 宽高硬编码：用 size prop 选档
 *
 * 用法：
 *   node scripts/check-design-tokens.mjs          # 有违规则退出码 1
 *   node scripts/check-design-tokens.mjs --json   # 机器可读
 *   node scripts/check-design-tokens.mjs --rule R2   # 只看某条规则
 *
 * 本脚本自己也被探针守着（它同样会安静失效，见上文行正则版的教训）：
 *   python3 ../../scripts/dev/verify_test_probes.py --runner tokens
 * 三条规则各注入一处真实违规，要求本脚本必须以非零码退出。改动规则判据后
 * 请跑一次——探针报 false-green 就是规则被放水了。
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import ts from "typescript";

// 与 scripts/committed/ 下其他脚本同一种算法：从本文件目录往上两级到 apps/web。
// 本脚本原先放在 scripts/ 根层、用 new URL("..") 取一级，那个位置被
// apps/web/.gitignore 的 `scripts/*.mjs` 忽略——文件没进版本库，CI 上直接
// MODULE_NOT_FOUND（2026-08-16 实测，本地全绿而 fork CI 红）。移到 committed/
// 后必须同步改成两级，否则 SRC 会指向 scripts/ 而扫不到任何源文件。
const ROOT = resolve(import.meta.dirname, "..", "..");
const SRC = join(ROOT, "src");

const SKIP_DIRS = new Set(["node_modules", "dist", "__tests__", "e2e", "tests"]);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(tsx|ts)$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name)) out.push(full);
  }
  return out;
}

/** 受管的原语与其禁止硬编码的类前缀 */
const CONTROL_TAGS = new Set(["Button", "Input", "SelectTrigger"]);
/** 只管这四档对应的高度；h-5/h-6 这类超出档位范围的另说（见 README 说明） */
const MANAGED_HEIGHTS = /^h-(?:7|8|9|10)$/;
/** DialogContent 上不该出现的尺寸类 */
const DIALOG_SIZE_CLASS = /^(?:sm:)?(?:max-)?w-(?:\w+|\[[^\]]+\])$|^(?:max-)?h-\[\d+vh\]$/;

const MESSAGES = {
  "R1-font-size":
    "硬编码字号。用 text-nano(10px) / text-micro(11px) / text-caption(12px) / text-body(13px) 或 Tailwind 内置 text-sm/base/lg",
  "R2-control-height":
    '原语上手写高度。用 size="xs|sm|md|lg"（28/32/36/40px；Button 的 36px 档名为 default），三者档位同源，不写 size 时天然对齐',
  "R3-dialog-size":
    'DialogContent 上手写宽高。用 size="sm|md|lg|xl" 选档（sm=表单 / md=详情 / lg=设置面板 / xl=市场双栏）',
};

const violations = [];
const files = walk(SRC);

/** 从 className 的 initializer 里取出所有静态字符串片段（含 cn()/模板串内部的字面量） */
function collectClassStrings(node, sf, acc = []) {
  if (!node) return acc;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    acc.push({ text: node.text, node });
  } else if (ts.isTemplateExpression(node)) {
    acc.push({ text: node.head.text, node: node.head });
    for (const span of node.templateSpans) acc.push({ text: span.literal.text, node: span.literal });
    node.templateSpans.forEach((s) => collectClassStrings(s.expression, sf, acc));
  } else if (ts.isJsxExpression(node)) {
    collectClassStrings(node.expression, sf, acc);
  } else if (ts.isCallExpression(node)) {
    node.arguments.forEach((a) => collectClassStrings(a, sf, acc));
  } else if (ts.isConditionalExpression(node)) {
    collectClassStrings(node.whenTrue, sf, acc);
    collectClassStrings(node.whenFalse, sf, acc);
  } else if (ts.isBinaryExpression(node)) {
    collectClassStrings(node.left, sf, acc);
    collectClassStrings(node.right, sf, acc);
  } else if (ts.isParenthesizedExpression(node)) {
    collectClassStrings(node.expression, sf, acc);
  } else if (ts.isArrayLiteralExpression(node)) {
    node.elements.forEach((e) => collectClassStrings(e, sf, acc));
  }
  return acc;
}

for (const file of files) {
  const src = readFileSync(file, "utf8");
  const rel = relative(ROOT, file).split(sep).join("/");
  const lines = src.split("\n");
  /** 行内含豁免标记则跳过该行的所有违规 */
  const exempt = (line) => lines[line - 1]?.includes("token-guard-ignore");

  // --- R1：全文扫硬编码 px 字号（rem 大字号属展示型排版，不进控件档位体系） ---
  lines.forEach((line, i) => {
    const re = /\btext-\[[\d.]+px\]/g;
    let m;
    while ((m = re.exec(line)) !== null) {
      if (exempt(i + 1)) continue;
      violations.push({ rule: "R1-font-size", file: rel, line: i + 1, match: m[0] });
    }
  });

  if (!/(Button|Input|SelectTrigger|DialogContent)\b/.test(src)) continue;

  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

  const visit = (node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(sf);
      const isControl = CONTROL_TAGS.has(tag);
      const isDialog = tag === "DialogContent";
      if (isControl || isDialog) {
        const classAttr = node.attributes.properties.find(
          (a) => ts.isJsxAttribute(a) && a.name.getText(sf) === "className",
        );
        if (classAttr?.initializer) {
          for (const { text, node: strNode } of collectClassStrings(classAttr.initializer, sf)) {
            const line = sf.getLineAndCharacterOfPosition(strNode.getStart(sf)).line + 1;
            if (exempt(line)) continue;
            for (const cls of text.split(/\s+/).filter(Boolean)) {
              if (isControl && MANAGED_HEIGHTS.test(cls)) {
                violations.push({ rule: "R2-control-height", file: rel, line, match: `<${tag} … ${cls}` });
              }
              if (isDialog && DIALOG_SIZE_CLASS.test(cls)) {
                violations.push({ rule: "R3-dialog-size", file: rel, line, match: `<DialogContent … ${cls}` });
              }
            }
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}

const onlyRule = process.argv.includes("--rule")
  ? process.argv[process.argv.indexOf("--rule") + 1]
  : null;
const shown = onlyRule ? violations.filter((v) => v.rule.startsWith(onlyRule)) : violations;

if (process.argv.includes("--json")) {
  console.log(JSON.stringify({ scanned: files.length, violations: shown }, null, 2));
} else {
  console.log(`设计 token 守卫：AST 扫描 ${files.length} 个源文件`);
  if (shown.length === 0) {
    console.log("通过，0 处违规。");
  } else {
    const byRule = new Map();
    for (const v of shown) {
      if (!byRule.has(v.rule)) byRule.set(v.rule, []);
      byRule.get(v.rule).push(v);
    }
    for (const [rule, items] of byRule) {
      console.log(`\n[${rule}] ${items.length} 处 — ${MESSAGES[rule]}`);
      for (const v of items.slice(0, 30)) console.log(`  ${v.file}:${v.line}  ${v.match}`);
      if (items.length > 30) console.log(`  ...还有 ${items.length - 30} 处（用 --json 看全量）`);
    }
    console.log(`\n合计 ${shown.length} 处违规。`);
  }
}

process.exit(shown.length === 0 ? 0 : 1);
