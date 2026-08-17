#!/usr/bin/env bash
# scripts/dev/release.sh -- AIASys 发布辅助脚本
#
# 用法：
#   ./scripts/dev/release.sh 0.4.17          # 正式发布
#   ./scripts/dev/release.sh --dry-run 0.4.17 # 演练，不实际提交/tag
#
# 规范：
#   - 必须在 main 分支上执行
#   - 工作区必须干净
#   - 必须已存在 docs/changelog/v{version}_{YYYY-MM-DD}.md
#   - 版本号会同步到 根/web/desktop package.json 与 backend/pyproject.toml（见 VERSION_FILES）
#   - --dry-run 零副作用：跑完自动还原版本号文件
#   - 自动提交、打 tag v{version} 并推送到 upstream

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DRY_RUN=false

usage() {
  cat <<EOF
Usage:
  ./scripts/dev/release.sh [--dry-run] <version>

Examples:
  ./scripts/dev/release.sh 0.4.17
  ./scripts/dev/release.sh --dry-run 0.4.17
EOF
  exit 1
}

# 解析参数
if [[ "$#" -eq 0 ]]; then
  usage
fi

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      VERSION="$1"
      shift
      ;;
  esac
done

if [[ -z "${VERSION:-}" ]]; then
  echo "错误：未指定版本号" >&2
  usage
fi

# 版本号格式校验：X.Y.Z 或 X.Y.Z-beta.N
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
  echo "错误：版本号格式不正确，期望 X.Y.Z 或 X.Y.Z-beta.N，得到 '$VERSION'" >&2
  exit 1
fi

TAG="v${VERSION}"
TODAY="$(date +%Y-%m-%d)"
CHANGELOG_FILE="docs/changelog/${TAG}_${TODAY}.md"

cd "$PROJECT_ROOT"

# 1. 分支检查
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "main" ]]; then
  echo "错误：必须在 main 分支上执行发布，当前分支为 '$current_branch'" >&2
  exit 1
fi

# 2. 工作区检查
if [[ -n "$(git status --short)" ]]; then
  echo "错误：工作区不干净，请先提交或 stash 改动" >&2
  git status --short
  exit 1
fi

# 3. 远程同步检查（可选但建议）
echo "==> 拉取 upstream/main 最新状态..."
git fetch upstream main >/dev/null 2>&1 || {
  echo "错误：无法 fetch upstream/main，请确认 remote 配置" >&2
  exit 1
}

local_main="$(git rev-parse main)"
upstream_main="$(git rev-parse upstream/main)"
if [[ "$local_main" != "$upstream_main" ]]; then
  echo "错误：本地 main ($local_main) 与 upstream/main ($upstream_main) 不一致" >&2
  echo "请先执行：git pull upstream main" >&2
  exit 1
fi

# 4. changelog 检查
if [[ ! -f "$CHANGELOG_FILE" ]]; then
  echo "错误：未找到 changelog 文件 '$CHANGELOG_FILE'" >&2
  echo "请先按 docs/changelog/README.md 规范编写 changelog，再执行发布。" >&2
  exit 1
fi
echo "==> 找到 changelog: $CHANGELOG_FILE"

# 5. 版本号同步
echo "==> 同步版本号到 $VERSION ..."

update_json_version() {
  local file="$1"
  local version="$2"
  node -e "
    const fs = require('fs');
    const p = JSON.parse(fs.readFileSync('$file', 'utf8'));
    p.version = '$version';
    fs.writeFileSync('$file', JSON.stringify(p, null, 2) + '\n');
  "
}

update_toml_version() {
  local file="$1"
  local version="$2"
  # 用 node 而不是 python3：Windows 的 Git Bash 里没有 python3（只有 python 或
  # 干脆不在 PATH），2026-08-16 实测发布脚本在 Windows 上跑到这一步直接
  # `python3: command not found` 中断——前置检查全过、版本号只改了一半。
  # 脚本上面已经依赖 node 处理 package.json，统一用 node 就没有第二个运行时依赖。
  # 语义与原 python 实现一致：只替换首个行首 `version = "..."`（对应 count=1 + MULTILINE）。
  node -e "
    const fs = require('fs');
    const file = process.argv[1];
    const version = process.argv[2];
    const src = fs.readFileSync(file, 'utf8');
    const re = /^version = \".*?\"/m;
    if (!re.test(src)) {
      console.error('错误：在 ' + file + ' 里找不到行首的 version = \"...\"');
      process.exit(1);
    }
    fs.writeFileSync(file, src.replace(re, 'version = \"' + version + '\"'));
  " "$file" "$version"
}

# 承载版本号的全部文件，只在这里列一次——此前 update / git diff / git add 三处
# 各列一遍，漏改一处就会静默漂移：根 package.json 正是这样在 0.4.34 那次发布里
# 被漏掉，一直停在 0.4.33（2026-08-15 实测）。
VERSION_FILES=(
  # 根 package.json 的 version 目前没有任何消费者（CI 读的是 apps/desktop 与
  # apps/web 各自的 package.json）。仍然纳入同步而不是删掉它：留着不管必然漂移，
  # 而漂移出来的旧版本号会误导读它的人和 AI（本轮就误判过一次当前版本）。
  "package.json"
  "apps/web/package.json"
  "apps/desktop/package.json"
  "apps/backend/pyproject.toml"
)

# 演练模式下装 EXIT trap：版本号同步中途失败（缺 node、文件里没有 version 行等）
# 也要还原。2026-08-16 实测过一次真实后果——同步在 python3 缺失处中断，三个
# package.json 已被改成新版本号却留在工作区，之后被误提交进测试环境。
# 只在 dry-run 装：真实发布要的就是这些改动留下并被提交。
if [[ "$DRY_RUN" == true ]]; then
  trap 'git checkout -- "${VERSION_FILES[@]}" 2>/dev/null || true' EXIT
fi

update_json_version "package.json" "$VERSION"
update_json_version "apps/web/package.json" "$VERSION"
update_json_version "apps/desktop/package.json" "$VERSION"
update_toml_version "apps/backend/pyproject.toml" "$VERSION"

# 6. 检查版本号是否真的改了
if [[ -n "$(git status --short)" ]]; then
  echo "==> 版本号变更如下："
  git diff -- "${VERSION_FILES[@]}"
else
  echo "==> 版本号已是 $VERSION，无需变更"
fi

# 7. 演练模式：不实际提交和 tag
if [[ "$DRY_RUN" == true ]]; then
  # 演练必须零副作用。原先只提示「请手动 reset」，忘了就留下一个版本号被改过的
  # 脏工作区，而下一次真实发布的前置检查恰好要求工作区干净——等于给自己埋雷。
  # 还原交给上面的 EXIT trap，这里只说明结果——避免两处各写一遍还原逻辑。
  echo ""
  echo "==> [DRY RUN] 演练完成：前置检查全过，版本号文件将被还原，未提交、未打 tag、未推送"
  echo ""
  echo "    如需继续真实发布，请重新执行（去掉 --dry-run）："
  echo "      ./scripts/dev/release.sh $VERSION"
  exit 0
fi

# 8. 提交版本号变更
read -r -p "确认提交版本号变更并打 tag $TAG 推送到 upstream? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "已取消"
  exit 1
fi

git add "${VERSION_FILES[@]}"
git commit -m "chore(release): bump version to $VERSION"

# 9. 打 tag 并推送
git tag "$TAG"
git push upstream main
# git push upstream "$TAG"  # 由 CI 监听 v* tag 触发桌面构建
echo "==> 已推送 main 分支"
echo "==> 请手动推送 tag 触发 CI：git push upstream $TAG"
echo ""
echo "发布流程："
echo "  1. git push upstream $TAG"
echo "  2. 等待 .github/workflows/ci-desktop.yml 完成"
echo "  3. 检查 release 产物：gh release view $TAG --json assets"
