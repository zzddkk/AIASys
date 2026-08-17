#!/usr/bin/env bash
# 各 pre-commit 检查共用的「本次提交是否涉及我关心的文件」判定。
#
# 为什么统一到这里：判空写法在 lefthook.yml 里内联时会被 Windows 的命令行解析
# 破坏（详见 lefthook.yml 头注释纪律 6），而放进 .sh 文件后是普通 bash 代码，
# 引号语义完全正常。范围判定是每个 hook 的第一步，也是最容易写错的一步
# （`[ -z $files ]` 在多文件时报 too many arguments），所以只留一份实现。
#
# 用法：
#   . "$(dirname "$0")/staged-scope.sh"
#   staged_matching '^apps/web/src/.*[.](ts|tsx)$' || exit 0   # 无匹配则跳过本检查
#   echo "$AIASYS_STAGED_FILES"   # 换行分隔的匹配结果（原始仓库相对路径）
#
# 刻意不用 lefthook 的 {staged_files}：它在 Windows 下展开数组时会附加多余字符，
# 拼进 test 表达式会报 "[: too many arguments" 且报错被吞掉。自己从 git 取可控。

set -uo pipefail

# staged_matching <ERE 正则> [路径前缀剥离]
#
# 匹配则返回 0 并把结果写入 AIASYS_STAGED_FILES（换行分隔）；无匹配返回 1。
# 第二个参数用于把 `apps/web/src/x.ts` 削成 `src/x.ts`，方便在子目录里直接传给工具。
staged_matching() {
  local pattern="$1"
  local strip_prefix="${2-}"

  AIASYS_STAGED_FILES="$(git diff --cached --name-only --diff-filter=ACM | grep -E "${pattern}" || true)"

  if [ -z "${AIASYS_STAGED_FILES}" ]; then
    return 1
  fi

  if [ -n "${strip_prefix}" ]; then
    AIASYS_STAGED_FILES="$(printf '%s\n' "${AIASYS_STAGED_FILES}" | sed "s|^${strip_prefix}||")"
  fi

  return 0
}

# staged_count —— AIASYS_STAGED_FILES 的行数
staged_count() {
  if [ -z "${AIASYS_STAGED_FILES:-}" ]; then
    echo 0
    return 0
  fi
  printf '%s\n' "${AIASYS_STAGED_FILES}" | wc -l | tr -d ' '
}
