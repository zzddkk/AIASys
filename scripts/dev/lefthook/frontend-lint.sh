#!/usr/bin/env bash
# pre-commit: 对本次改动的前端源文件跑 eslint。
#
# 历史：这段逻辑原先内联在 lefthook.yml 的 run 里，2026-08-16 实测被 Windows 的
# 命令行解析截断，导致 hook 恒定 exit 0 —— 一个含 parse error 的文件直接进了提交，
# 而 lefthook 显示 ✓、耗时 0.16 秒。触发点是下面那句带空格的中文 echo：
# 内联脚本里第 7 个双引号让解析器回到「引号外」状态，随后的空格直接截断了参数，
# 脚本在 `echo 改动` 处断掉、if 没有 fi。提取成 .sh 文件后不再经过那层解析。

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

staged_matching '^apps/web/src/.*[.](ts|tsx|js|jsx)$' 'apps/web/' || exit 0

cd apps/web

# 大批量改动时不能把文件列表全塞进命令行：Windows 有约 32K 的命令行长度上限，
# 2026-08-16 一次 130 文件的改动实测直接报 "The command line is too long."，
# 本该拦问题的 lint 反而成了提交阻塞，且报错信息完全指不到真实原因。
# 超过 60 个文件就改跑全量——全量慢几秒，但不会「因为改得多所以跑不了」。
count="$(staged_count)"
if [ "${count}" -gt 60 ]; then
  echo "  改动 ${count} 个文件，超过单次传参阈值，改跑全量 lint"
  npm run lint
else
  # 有意不加引号：需要 word splitting 把多个文件分别传给 eslint。
  # 前提是文件名不含空格，这是本仓约定。
  # shellcheck disable=SC2086
  npx eslint ${AIASYS_STAGED_FILES}
fi
