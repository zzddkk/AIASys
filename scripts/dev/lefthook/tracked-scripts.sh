#!/usr/bin/env bash
# pre-commit: CI 与 npm scripts 引用的脚本必须都在版本库里。
#
# 为什么需要：本地测的是工作区，CI 测的是版本库，在「文件是否存在」上两者不等价。
# 2026-08-16 实测，设计 token 守卫脚本落在被 .gitignore 忽略的目录里，
# 本地五道校验全绿、fork CI 直接 Cannot find module。这类失效本地测不出来，
# 只能靠静态检查在提交时拦住。

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

# 范围：动了 workflow 或任一 package.json 才有必要重查引用关系。
staged_matching '^([.]github/workflows/.*[.]ya?ml|(apps/[^/]+/)?package[.]json)$' || exit 0

node scripts/dev/check_tracked_scripts.mjs
