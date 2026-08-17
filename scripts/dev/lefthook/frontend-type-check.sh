#!/usr/bin/env bash
# pre-commit: 前端类型检查。
#
# 必须用 -b（build mode），因为 apps/web/tsconfig.json 是 project references 结构，
# 直接 --noEmit 会因 files: [] 而不检查任何源文件（2026-08-09 实测：不带 -b 退出码 0
# 且零输出，带 -b 在同一 commit 上报 4 条错误）。
#
# tsc -b 是整项目增量检查、不针对单文件，所以这里只判断「有没有 ts/tsx 改动」，
# 有则整体跑一次。判空是为了避免改一个 .css 也触发 tsc。
#
# 若这一步报 TS2307 找不到模块（prismjs / @codemirror/language / @lezer/highlight 等），
# 先别怀疑代码：大概率是 dev 依赖没装全。本机 NODE_ENV=production 会让 npm 自动
# omit=dev，跳过 typescript 等 17 个 devDependencies。apps/web/.npmrc 的 include=dev
# 已压住这个行为，但若 node_modules 是在加 .npmrc 之前装的，需要重装一次：
#   cd apps/web && npm ci

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

staged_matching '^apps/web/src/.*[.](ts|tsx)$' || exit 0

cd apps/web
npx tsc -b --noEmit
