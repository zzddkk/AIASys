#!/usr/bin/env bash
# 验证 pre-commit hook 是否真的在按内容判断：违规的拦下，合规的放过。
#
# 为什么需要这个脚本：`lefthook run pre-commit` 全绿不能说明 hook 有效。
# 2026-08-09 实测两个静默失效的例子，都是「绿灯 + 违规代码照样进仓」：
#   - glob 写了 brace 扩展（{ts,tsx}），lefthook 不认，整个 hook 被跳过；
#   - 检查命令末尾挂 `|| true` / 包装脚本无条件 exit(0)，永远返回成功。
# 两者在日志里都只留一行不起眼的提示，肉眼审查很容易漏掉。
#
# 为什么必须有反向用例（NEGATIVE 那一节）：只测「违规被拦下」会产生假 PASS。
# 同日实测：editorconfig 包装器因 spawn .cmd 报 EINVAL 而恒定 exit 1，四个正向探针
# 于是全部 PASS——它拦下提交不是因为检测到违规，而是因为工具崩了。一个永远失败的
# hook 能通过所有正向探针。「拦下了」有两个解释，只取其一就是自欺。
#
# 判据用「HEAD 有没有移动」，不用 `$?`：命令一旦接管道（如 `| tail`），$? 是管道尾的
# 退出码，会把被正确阻塞的提交误报成成功——这个坑本次踩过两次。
#
# 用法：bash scripts/dev/lefthook/verify-hooks.sh
# 退出码：0 = 违规全拦、合规全放；1 = 有 hook 失效或过严（详见输出）。
#
# 安全性：只 git add 探针文件本身，绝不 `git add -A`；探针一旦进了提交立即
# `reset --soft` 撤销并保留工作区改动；结束时校验 HEAD 与开工时一致。

set -u

cd "$(dirname "$0")/../../.." || exit 1
REPO_ROOT="$(pwd)"
START_HEAD="$(git rev-parse HEAD)"
FAILED=0

# 撤销探针留下的痕迹：若它进了提交就回滚该提交（--soft 保住其他工作区改动）
cleanup_probe() {
  if [ "$(git rev-parse HEAD)" != "$START_HEAD" ]; then
    git reset -q --soft HEAD~1
  fi
  for f in "$@"; do
    git restore --staged "$f" 2>/dev/null
    rm -f "$f"
  done
}

# expect_blocked <名称> <探针路径> —— 违规文件，HEAD 不应移动
expect_blocked() {
  local name="$1" probe="$2"
  printf '  %-20s ' "$name"
  git add "$probe" 2>/dev/null
  git commit -q -m "probe: $name should be blocked" >/dev/null 2>&1
  if [ "$(git rev-parse HEAD)" = "$START_HEAD" ]; then
    echo "PASS  违规被拦下"
  else
    echo "FAIL  违规文件进了提交，该 hook 形同虚设"
    FAILED=1
  fi
  cleanup_probe "$probe"
}

echo "仓库: $REPO_ROOT"
echo "基准 HEAD: $(git log --oneline -1)"

if [ ! -f .git/hooks/pre-commit ]; then
  echo "警告: .git/hooks/pre-commit 不存在，hook 根本没安装。"
  echo "先跑: cd apps/web && npx lefthook install"
  exit 1
fi

# 前置条件：index 必须干净。editorconfig 之类的 hook glob 是 `*`，会检查本次提交的
# 全部 staged 文件，index 里若已有别的东西，探针结果就会被它们污染。
# 2026-08-09 实测：index 里躺着一个混合行尾的脚本，让「合规必须放过」误判成 FAIL，
# 排查方向也被带偏到了行尾策略上。所以这里直接拒绝在脏 index 上运行。
if [ -n "$(git diff --cached --name-only)" ]; then
  echo
  echo "前置条件不满足：index 里有 staged 改动，探针结果会被它们污染。"
  echo "请先 git commit 或 git stash，再跑本脚本。当前 staged 文件："
  git diff --cached --name-only | sed 's/^/  /'
  exit 1
fi

echo
echo "POSITIVE（违规必须拦下）"

P1="apps/backend/tests/test__hook_probe.py"
printf 'x   =    1\ndef  f( ):\n    return    x\n' > "$P1"
expect_blocked "backend-ruff" "$P1"

P2="apps/web/src/__hook_probe_type.ts"
printf 'export const probe: number = "字符串不是数字";\n' > "$P2"
expect_blocked "frontend-type-check" "$P2"

P3="apps/web/src/__hook_probe_lint.ts"
printf 'export function probe(): number {\n  debugger;\n  return 1;\n}\n' > "$P3"
expect_blocked "frontend-lint" "$P3"

# 位置很重要：上面两个探针在 src/ 根层，下面这个在子目录。
# 2026-08-09 实测，glob `src/**/*` 只匹配子目录、漏掉根层，两种位置结果不同——
# 而 App.tsx / main.tsx 正在根层。所以两个位置都要测，缺一个就会漏掉这类偏差。
P5="apps/web/src/utils/__hook_probe_deep.ts"
printf 'export const probeDeep: number = "字符串不是数字";\n' > "$P5"
expect_blocked "type-check(子目录)" "$P5"

# desktop 探针：语法坏掉的 .cjs 必须被 desktop-unit 拦下。
# 这个探针的由来是一次真实漏网——改 vendor 下载脚本时留下多余的右花括号，
# 当时五个 hook 没有一条覆盖 apps/desktop，语法错误顺利进了工作区。
# 放在 scripts/ 下而不是 src/ 下：构建脚本是实际出事的位置。
P6="apps/desktop/scripts/__hook_probe_broken.cjs"
printf 'const broken = {;\n' > "$P6"
expect_blocked "desktop-unit" "$P6"

# design-tokens 探针：硬编码字号必须被守卫拦下。
# 三条规则里选 R1，因为它是最容易被「顺手写个 text-[11px]」引入的一类，
# 也是改造前存量最多的一类（881 处）。R2/R3 的规则有效性由
# scripts/dev/verify_test_probes.py --runner tokens 在 CI 里逐条验证，
# 这里只需确认「守卫确实挂在 pre-commit 这条链路上」。
#
# 归因说明：本脚本的判据是「HEAD 有没有移动」，任一 hook 拦下都算 PASS，
# 所以探针内容必须对其余 hook 无害，否则会把「别人拦的」记成「它拦的」。
# 2026-08-16 实测这份内容：eslint 退出 0、tsc -b 退出 0、只有 design-tokens
# 报 1 处 R1 违规并退出 1。改动这个探针后请重测这三项。
P7="apps/web/src/__hook_probe_tokens.tsx"
printf 'export const Probe = () => <span className="text-[13px]">x</span>;\n' > "$P7"
expect_blocked "design-tokens" "$P7"

P4="apps/backend/tests/__hook_probe_ec.txt"
printf 'trailing spaces here   \nno final newline' > "$P4"
expect_blocked "editorconfig" "$P4"

echo
echo "NEGATIVE（合规必须放过，用于排除「恒定失败」的假 PASS）"

Q1="apps/backend/tests/test__hook_probe_ok.py"
printf 'PROBE_VALUE = 1\n\n\ndef probe_ok() -> int:\n    return PROBE_VALUE\n' > "$Q1"
Q2="apps/web/src/__hook_probe_ok.ts"
printf 'export const probeOk: number = 1;\n' > "$Q2"

printf '  %-20s ' "全部 hook"
git add "$Q1" "$Q2" 2>/dev/null
git commit -q -m "probe: compliant files should pass" >/dev/null 2>&1
if [ "$(git rev-parse HEAD)" != "$START_HEAD" ]; then
  echo "PASS  合规被放过"
else
  echo "FAIL  合规文件也被拦，说明某个 hook 恒定失败（正向 PASS 不可信）"
  FAILED=1
fi
cleanup_probe "$Q1" "$Q2"

echo
if [ "$(git rev-parse HEAD)" != "$START_HEAD" ]; then
  echo "异常: HEAD 与开工时不一致，请检查 git log。"
  FAILED=1
else
  echo "HEAD 未被污染: $(git log --oneline -1)"
fi

if [ "$FAILED" -eq 0 ]; then
  echo "结果: 违规全部拦下、合规全部放过，hook 按内容生效。"
else
  echo "结果: 存在失效或过严的 hook，见上方 FAIL 行。"
fi
exit "$FAILED"
