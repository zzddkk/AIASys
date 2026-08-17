#!/usr/bin/env bash
# pre-commit: 设计 token 守卫（规则与理由见 apps/web/scripts/committed/check-design-tokens.mjs 头注释）。
#
# 与 CI 的 Design token guard 同一个脚本，放这里是为了提交时就拦住，不必等 CI。
# 它走 TS AST 扫全量 src、不吃文件列表——既不受命令行长度上限影响，
# 也不会因为「这次只改了一个文件」而漏掉跨文件的档位漂移。

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

staged_matching '^apps/web/src/.*[.](ts|tsx)$' || exit 0

cd apps/web
npm run check:tokens
