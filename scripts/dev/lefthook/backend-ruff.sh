#!/usr/bin/env bash
# pre-commit: 后端 ruff 检查与格式核对。
#
# 范围含 tests/：原先只查 apps/backend/app/，tests/ 不在范围内，积压了 10 个未
# 格式化文件而无人发现（同期 app/ 侧 431 个文件全部合格，对比很明显）。

set -uo pipefail

cd "$(dirname "$0")/../../.." || exit 1
. scripts/dev/lefthook/staged-scope.sh

staged_matching '^apps/backend/(app|tests)/.*[.]py$' 'apps/backend/' || exit 0

cd apps/backend

# --no-sync：跳过 uv 的环境同步步骤。裸 uv run 执行前会核对 venv 与
# uv.lock 是否一致，不一致就联网重新解析——代理关闭/离线时会挂在连包
# 索引上，提交卡死在 backend-ruff（2026-08-13 实测两次）。ruff 只需
# 用 venv 里已装的版本，同步对它没有价值。
#
# $AIASYS_STAGED_FILES 有意不加引号（需要 word splitting 把多个文件传给 ruff）；
# 前提是 Python 文件名不含空格，这是本仓约定。
# shellcheck disable=SC2086
uv run --no-sync ruff check ${AIASYS_STAGED_FILES}
# shellcheck disable=SC2086
uv run --no-sync ruff format --check ${AIASYS_STAGED_FILES}
