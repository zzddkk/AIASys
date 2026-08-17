#!/usr/bin/env bash
# pre-commit: Electron 侧单测（内含 .cjs 语法门）。
#
# 为什么需要这条：apps/desktop 此前完全没有 pre-commit 覆盖。其余检查的范围分别是
# apps/web/src 与 apps/backend/{app,tests}，desktop 的主进程与 scripts/ 下的构建
# 脚本一条都没管。
#
# 代价是实打实付过的：2026-08-10 改 vendor 下载脚本时留下多余的右花括号，
# download-sqlite-vec-binary.cjs 语法错误，五个 hook 全部放过（各自 0.2 秒就
# 「完成」，因为没有一条的范围包含 .cjs），靠人眼跑 node --check 才发现。
# 同一次检查还翻出 kill-dist.cjs 内容其实是 PowerShell、扩展名写成 .cjs，
# 语法错误已经在仓库里躺了很久没人知道。
#
# 跑 test:unit 而不是只做 node --check：test:unit 内部已含语法门（递归扫 scripts/
# 下所有 .cjs 过 node --check），顺带跑完单测，实测 2.1 秒，覆盖面大得多而耗时相当。
#
# 范围含 scripts/ 与 src/：构建脚本坏了和主进程坏了一样会让应用起不来。

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

staged_matching '^apps/desktop/(src|scripts)/.*[.](cjs|mjs|js)$' || exit 0

cd apps/desktop
npm run test:unit
