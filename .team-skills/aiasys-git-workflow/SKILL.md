---
name: aiasys-git-workflow
description: |
  AIASys 项目专属 Git 工作流。当在 AIASys 项目中执行 git commit、创建分支、
  准备 PR、审计 worktree、整理提交历史时触发。
  适用于 AIASys 仓库内的版本控制规范，不适用于其他项目或个人仓库。
---

# AIASys Git 工作流

Git 提交、分支管理和 PR 协作的规范，确保代码历史清晰、安全、可追溯。

`git-workflow` 现在也是以下旧入口的 canonical skill：

- `pull-request`
- `worktree-status`

如果用户显式提到这些旧名字，允许通过 compatibility alias 触发，但执行时应回到本 skill。

---

## 核心原则

1. **精确暂存**：禁止 `git add .`，显式指定文件
2. **原子提交**：每批提交只做一件事
3. **安全第一**：提交前检查敏感信息
4. **清晰历史**：提交信息简洁明了

---

## 提交规范

### 精确暂存

```bash
# (正确) 正确：显式指定文件
git add apps/backend/app/api/file_router.py
git add apps/web/src/components/Button.tsx

# (错误) 错误：一键添加所有
git add .
```

### 提交信息格式

```
<type>(<scope>): <subject>

<body>
```

**类型（Type）：**

| 类型 | 用途 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat(agent): 添加 Worker 汇报功能` |
| `fix` | 修复 bug | `fix(api): 修复 MCP 配置保存失败` |
| `docs` | 文档更新 | `docs: 更新 API 文档` |
| `style` | 代码格式 | `style: 格式化代码` |
| `refactor` | 重构 | `refactor: 重构会话管理` |
| `perf` | 性能优化 | `perf: 优化数据库查询` |
| `test` | 测试相关 | `test: 添加单元测试` |
| `chore` | 构建/工具 | `chore: 更新依赖` |
| `revert` | 回滚 | `revert: 回滚错误提交` |

**范围（Scope）：**

```
agent      - Agent 相关
api        - API 接口
frontend   - 前端代码
database   - 数据库
infra      - 基础设施
docs       - 文档
```

**示例：**

```bash
git commit -m "feat(mcp): 添加 MCP 配置批量导入功能

- 支持 CSV/JSON 格式导入
- 提供导入预览功能
- 记录导入日志

Closes: #123"
```

### 作者身份

```bash
# 使用当前仓库配置的用户
git config user.name "Your Name"
git config user.email "your.email@example.com"

# 提交时自动使用上述身份
# 禁止使用 "AI Developer" 等占位身份
```

---

## 分批次提交工作流

### 适用场景

- 工作区文件较多且杂乱
- 需要按逻辑分组提交
- 准备 PR 前整理提交历史

### 执行流程

**1. 检查当前状态**

```bash
git status --short
git log --oneline -5
```

**2. 分析文件分组**

按以下逻辑分组：
- **配置类**：`.gitignore`, `.env.example`, `lefthook.yml`
- **文档类**：`README.md`, `docs/**/*.md`
- **代码类**：按模块/功能分组
- **测试类**：`tests/**/*`, `**/*.test.ts`
- **资源类**：示例数据、图片
- **工具类**：脚本、CI/CD 配置

**3. 分批提交**

```bash
# 批次 1: 配置更新
git add .gitignore lefthook.yml
git commit -m "chore: 更新项目配置"

# 批次 2: 文档更新
git add docs/ README.md
git commit -m "docs: 更新文档"

# 批次 3: 功能代码
git add apps/backend/app/services/
git add apps/web/src/components/
git commit -m "feat: 添加 MCP 配置管理功能"

# 批次 4: 测试代码
git add tests/
git commit -m "test: 添加 MCP 服务单元测试"
```

---

## 提交前检查

### 安全检查（必须）

```bash
#!/bin/bash
# 提交前安全检查脚本

echo "(安全) 安全检查..."

# 1. 检查敏感文件
echo "检查敏感文件..."
SENSITIVE=$(git diff --cached --name-only | grep -E '\.(env|key|pem|p12|pfx)$' || true)
if [ -n "$SENSITIVE" ]; then
    echo "(错误) 发现敏感文件:"
    echo "$SENSITIVE"
    echo "请取消暂存: git reset HEAD <file>"
    exit 1
fi

# 2. 检查硬编码密钥
echo "检查硬编码密钥..."
KEYS=$(git diff --cached | grep -iE '(api_key|apikey|secret|password|sk-[a-zA-Z0-9]{32,}|ghp_[a-zA-Z0-9]{36})' || true)
if [ -n "$KEYS" ]; then
    echo "(警告) 发现可能的密钥:"
    echo "$KEYS"
    read -p "确认要继续吗? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 3. 检查大文件
echo "检查大文件..."
LARGE=$(git diff --cached --numstat | awk '$1 > 100 || $2 > 100 {print $3}' || true)
if [ -n "$LARGE" ]; then
    echo "(警告) 发现大改动文件:"
    echo "$LARGE"
fi

# 4. 检查空文件
echo "检查空文件..."
for file in $(git diff --cached --name-only --diff-filter=A); do
    if [ -f "$file" ] && [ ! -s "$file" ]; then
        echo "(警告) 空文件: $file"
    fi
done

echo "(正确) 安全检查完成"
```

### 代码检查

```bash
# 前端检查
cd apps/web
npm run lint
npm run type-check
npm run build

# 后端检查
cd apps/backend
ruff check app/
ruff format --check app/
```

---

## 分支管理

### 分支命名

```
feature/description   - 新功能
bugfix/description    - Bug 修复
hotfix/description    - 紧急修复
refactor/description  - 重构
docs/description      - 文档
```

### 活跃分支

- `main` — 生产分支，由主管理员管理
- `dev` — 开发分支，日常开发合并目标
- `feature/*`、`fix/*` — 功能/修复分支，从 dev 切出

**工作流**：
1. 从 `dev` 切出功能分支
2. 开发完成后合并回 `dev`
3. 需要发布时，从 `dev` 提 PR 到 `main`，由主管理员合并

### main/dev 分支关系保护

仓库已配置 `.github/workflows/branch-relationship.yml`，在 push 到 `main` 或 PR 合并到 `main` 时执行检查：

1. **PR 来源检查**：目标分支为 `main` 的 PR，来源分支必须是 `dev`。
2. **独立提交检查**：`main` 上不允许存在 `dev` 没有的非 merge commit。

这意味着 `main` 只能通过合并 `dev` 来更新。merge commit 是正常产物，会被允许；但直接 push 到 `main` 或从其他分支合并到 `main` 会被 CI 拦截。

**配置要求**：在 GitHub Settings → Branches → Branch protection rules 中，将 `Branch Relationship / ensure-main-not-ahead-of-dev` 设为 `main` 分支的 Required status check，才能真正阻止非法合并。

### 工作流

```bash
# 1. 创建功能分支
git checkout -b feature/mcp-config

# 2. 开发并提交
git add ...
git commit -m "feat(mcp): ..."

# 3. 保持与主分支同步
git fetch upstream
git rebase upstream/dev

# 4. 推送分支到自己的 fork
git push -u origin feature/mcp-config

# 5. 创建 PR（从 fork 向 upstream/dev）

# 6. 合并后清理
git checkout main
git pull upstream main
git branch -d feature/mcp-config
```

### 在 fork 上完成 CI 验证

所有工作流文件都在仓库中，fork 仓库默认会运行 GitHub Actions。外部贡献者和管理员都可以**在自己的 fork 上**完成 CI 验证：

**常规检查**：push 到 fork 的功能分支后，lint / type-check / test 工作流会自动运行。

**桌面端构建**：`.github/workflows/ci-desktop.yml` 监听 `v*` 标签。在 fork 上可以这样触发：

```bash
# 将功能分支合并到 fork main（或直接在 main 上验证）
git checkout main
git merge feature/mcp-config
git push origin main

# 打 tag 并推送，触发 fork 的 Desktop Build CI
git tag v0.4.17-fork-test.1
git push origin v0.4.17-fork-test.1
```

注意：
- fork 上的 tag 仅用于验证构建，不应与上游正式 tag 冲突。
- 正式发版仍按上游流程：从 `upstream/dev` PR 到 `upstream/main`，合并后在 `upstream/main` 上打 tag。

### fork 先行 CI 与上游选择性发版

项目采用「fork 先行验证，上游选择性发布」的双层模型：

```
贡献者 fork
├── feature/xxx 分支开发
├── push 到 fork 触发 lint / test / type-check
├── 合并到 fork main + 打 v* tag 触发桌面构建 CI
└── 确认 CI 通过后，向 upstream/dev 提 PR

upstream 主仓库
├── dev 接收并审查 PR
├── main 只合入经过验证的 release 批次
└── 管理员选择性在 main 上打 tag，触发正式桌面发布
```

**对贡献者的要求：**

1. **常规改动**：至少让 fork 上的 lint / test / type-check CI 通过后再提 PR。
2. **涉及桌面端**：建议先在 fork 上合并到 main 并打 `vX.Y.Z-fork-test.N` tag，确认 Desktop Build CI 通过后再向上游提 PR。
3. **修正 commit**：根据 review 意见，在自己的 fork 分支上 rebase / amend，force push 到 fork，不要直接 push 到 upstream。

**对上游维护者的要求：**

1. **不替贡献者跑 CI**：桌面构建在 fork 侧完成，上游只 review 代码和 CI 结果。
2. **选择性打 tag**：只有准备发布时，才在 `upstream/main` 上打正式 `v*` tag 触发全局桌面发布。
3. **上游 tag 即正式发布**：fork 的 tag 是验证 tag，upstream 的 tag 是 release tag，两者语义不同。

### 修正 commit

在 PR 审查过程中，作者应在自己的 fork 分支上修正 commit：

```bash
# 本地整理历史（未 push 或仅 push 到自己 fork 时）
git rebase -i upstream/dev

# 合并上游最新改动后解决冲突
git fetch upstream
git rebase upstream/dev

# 已 push 到自己 fork，安全更新
git push --force-with-lease origin feature/mcp-config
```

红线：
- 禁止对已合并到 `upstream/dev` / `upstream/main` 的 commit 做 rebase / force push。
- 禁止替外部贡献者 rebase 其 PR 分支，除非对方明确授权。

---

## Rebase 使用规范

### 核心原则

Rebase 用于**整理自己的本地历史**，不是用于改写已共享的历史。

> **原则上禁止**：对已经合并到 `dev` / `main` 的 commit 做 rebase 或 force push。
>
> 例外情况（仅管理员执行，且需提前通知所有协作者）：
> - 清理敏感信息泄露
> - 修正错误作者身份或 bot 占位身份
> - 移除不应出现在正式历史中的 `Co-authored-by` 等元数据
>
> 这些操作会重写公共历史，执行后所有协作者必须重新同步。

### 适用场景

| 场景 | 是否推荐 | 说明 |
|---|---|---|
| 本地分支尚未 push | ✅ 推荐 | 整理 commit、清理临时提交，无协作副作用 |
| 自己 fork 上的分支，刚 push，无他人基于它工作 | ✅ 可以 | 整理后再提 PR，可 force push 到自己 fork |
| 外部贡献者的 PR 分支 | ❌ 禁止 | 不要替作者 rebase，会破坏对方本地分支和 review 上下文 |
| 多人协作的功能分支 | ❌ 禁止 | 一旦有人基于旧 commit 工作，历史会分叉 |
| 已合并到 `dev` / `main` 的历史 | ❌ 绝对禁止 | 等同于重写项目主历史 |

### 与贡献记录的关系

- `git rebase` 默认**保留每个 commit 的 author 信息**，只改变 commit hash
- GitHub 贡献统计基于 author，因此正常 rebase **不会抹除贡献记录**
- 会抹除贡献记录的行为：
  - `git commit --amend --author=...` 改写他人 commit 的作者
  - Squash merge 时把多个作者的 commit 压成一个，且未加 `Co-authored-by:`
  - 用 `git rebase -i` 配合 `exec 'git commit --amend --reset-author --no-edit'` 批量重置 author

### 推荐操作

```bash
# 1. 本地整理历史（未 push 前）
git rebase -i HEAD~5

# 2. 自己的 fork 分支在提 PR 前保持与 upstream/dev 同步
git fetch upstream
git rebase upstream/dev

# 3. 如果已经 push 到 fork，需要 force push（仅当自己分支无他人依赖时）
git push --force-with-lease origin feature/mcp-config
```

### 多人协作时的替代方案

如果分支有多人协作，或外部贡献者已基于该分支工作，**用 merge 代替 rebase**：

```bash
git fetch upstream
git merge upstream/dev
```

---

## Worktree 工作流（高级）

### 何时使用

- 需要同时处理多个任务
- 需要维护多个版本
- 用户明确要求并行开发

### 使用规范

```bash
# 1. 创建 worktree
git worktree add ../aiasys-feature-mcp feature/mcp-config

# 2. 进入 worktree 工作
cd ../aiasys-feature-mcp

# 3. 完成后合并
git checkout main
git merge feature/mcp-config

# 4. 清理 worktree
git worktree remove ../aiasys-feature-mcp
git branch -d feature/mcp-config
```

**注意事项：**
- 每个 worktree 必须有独立分支
- 明确修改范围边界
- 主控掌握合并顺序

### Worktree 状态审计

当用户问：

- 哪些 worktree 可以清理
- 哪些 worktree 已合并
- 哪些分支还脏着

不要临时发明检查步骤，直接读取：

- `references/worktree-audit.md`

该参考稿承接了旧 `worktree-status` skill 的具体审计流程和输出表格格式。

---

## 变更记录更新

`docs/changelog/` 是当前仓库稳定的人类可维护 changelog 入口，位于 `docs/changelog/README.md` 有详细编写规范。

规则：

- **任何用户可感知的功能新增、bug 修复、性能优化、接口不兼容修改，必须随 PR 同步更新 `docs/changelog/`**。
- 文档-only 改动（如 README、AGENTS.md）默认不写 changelog；若文档更新伴随真实功能修复，应记录功能修复本身。
- 纯版本号变更、纯格式整理、运维操作不写 changelog。
- 每个 release 必须有一份对应的 `docs/changelog/vX.Y.Z_YYYY-MM-DD.md`。

AI 在协助用户准备 PR 时：

1. 先判断本次改动是否包含用户可感知的行为变化。
2. 若包含，检查是否已存在对应的 changelog 文件或条目。
3. 若不存在，按 `docs/changelog/README.md` 规范补充。
4. 不要把 changelog 更新和代码改动塞进同一个 commit，应独立为 `docs(changelog): ...` commit。

---

## WSL 下 git HTTPS 失败 fallback

在 WSL 里操作 GitHub HTTPS 时，若遇到 TLS 握手/连接重置（如 `OpenSSL SSL_connect` 错误），可切到 Windows PowerShell 使用同一仓库路径完成推送/PR。

```powershell
# 进入 WSL 文件系统的 Windows 路径
cd "\\wsl$\Ubuntu-22.04\home\ke\projects\AIASys"

# 使用 gh（通过 GH_TOKEN 认证）创建 PR、合并、打标签
$env:GH_TOKEN = "<your-token>"
gh pr create --title "..." --body "..." --base dev
gh pr merge --squash
gh release create v0.0.0-beta.1 --prerelease --title "Beta v0.0.0-beta.1"
```

注意：

- 用 Windows git 向 WSL 仓库提交时，设置 `core.autocrlf=false` 与 `core.filemode=false`，避免跨环境换行和权限噪音。
- 从 WSL bash 通过 `powershell.exe -Command "..."` 传 PowerShell 命令时，bash 会展开 `$env`，应写成 `\$env:GH_TOKEN=...`，否则会变成 `:GH_TOKEN=...`。

## Beta 版本发布检查清单

适合从 `dev` 合并到 `main` 后发布 beta 预发布版本。

### 推荐方式：使用发布脚本

```bash
# 在 main 分支上执行
./scripts/dev/release.sh X.Y.Z-beta.N
```

脚本会自动完成：分支检查、工作区检查、changelog 检查、三端版本号同步、提交、打 tag。

演练模式（不实际提交/tag）：

```bash
./scripts/dev/release.sh --dry-run X.Y.Z-beta.N
```

### 手动方式

1. **合并主线**：feature → dev → main（均通过 PR，不要直接 push）。
2. **同步版本号**：修改以下三处为同一版本：
  - `apps/web/package.json`
  - `apps/desktop/package.json`
  - `apps/backend/pyproject.toml`
3. **更新 changelog**：在 `docs/changelog/` 新建 `v{version}_{YYYY-MM-DD}.md`，记录主要变更。
4. **提交版本号变更**：
  ```bash
  git add apps/web/package.json apps/desktop/package.json apps/backend/pyproject.toml
  git commit -m "chore(release): bump version to X.Y.Z-beta.N"
  ```
5. **打 tag 并推送**：
  ```bash
  git tag vX.Y.Z-beta.N
  git push upstream main
  git push upstream vX.Y.Z-beta.N
  ```
6. **等待 CI 构建**：`v*` tag 会触发 `.github/workflows/ci-desktop.yml`，在 Linux / Windows / macOS 三端构建桌面安装包并上传到 release。
7. **验证产物**：通过 `gh release view vX.Y.Z-beta.N --json assets` 确认 AppImage / exe / dmg / zip 已上传。

### 发布纪律

- **禁止直接 push `main` 或 `dev`**，管理员自己的改动也必须走 PR。
- 发布前确认 CI 在 `main` 上通过。
- 每个 release 必须有对应的 changelog。

---

## 完整提交示例

```bash
# 1. 检查状态
git status

# 2. 分批提交
git add apps/backend/app/api/routes/mcp.py
git add apps/backend/app/services/mcp_external_market_service.py
git commit -m "feat(mcp): 添加 MCP 配置 CRUD 接口

- 实现配置增删改查
- 添加参数验证
- 统一错误处理"

git add apps/web/src/pages/MCPConfig/
git commit -m "feat(ui): 添加 MCP 配置管理页面

- 配置列表展示
- 新增/编辑表单
- 删除确认对话框"

git add tests/
git commit -m "test: 添加 MCP 功能测试

- 单元测试
- 集成测试"

# 3. 安全检查
git diff --cached --name-only | grep -E '\.(env|key|pem)$'
# Windows 替代: git diff --cached --name-only | findstr /R "\.(env|key|pem)$"
# 应无输出

# 4. 代码检查
# 如果项目提供跨平台检查脚本，运行它；否则手动执行等价检查
./.agents/skills/aiasys-workflow/scripts/check.sh  # Linux / macOS
# Windows 上若脚本不可用，直接运行脚本内的等价命令

# 5. Push
git push origin feature/mcp-config
```

---

## 快速检查清单

**提交前：**
- [ ] 使用 `git add <具体文件>` 而非 `git add .`
- [ ] 提交信息符合 `<type>(<scope>): <subject>` 格式
- [ ] 作者身份正确（非 AI Developer）

**安全检查：**
- [ ] 无敏感文件（.env, *.key, *.pem）
- [ ] 无硬编码密钥
- [ ] 无意外的大文件

**代码检查：**
- [ ] Lint 通过
- [ ] 类型检查通过
- [ ] 测试通过

**Push 前：**
- [ ] 已更新 Changelog
- [ ] 已同步文档
- [ ] 无 TODO/FIXME 遗留

---

## 应急处理

### 撤销提交

```bash
# 撤销最后一次提交，保留修改
git reset --soft HEAD~1

# 撤销最后一次提交，丢弃修改（危险！）
git reset --hard HEAD~1

# 撤销已 push 的提交（团队慎用）
git revert HEAD
git push
```

### 修改历史

```bash
# 修改最后一次提交
git commit --amend

# 修改多个提交（交互式）
git rebase -i HEAD~3
```

### 密钥泄露应急

```bash
# 1. 立即撤销密钥（在服务商控制台）

# 2. 从历史中移除
pip install git-filter-repo
git filter-repo --path .env --invert-paths

# 3. 强制推送
git push origin --force --all

# 4. 通知协作者
```

---

## 关联 Skill

- **commit-history-audit** — Commit 历史审计与清理。当需要检查整体历史质量、发现重复提交、
  清理 stale 分支、或合并 commit 时使用。本 Skill 管每次提交怎么写，commit-history-audit
  管历史已有问题怎么查。

---

*清晰的 Git 历史是团队协作的基础——每个提交都应该是可理解、可回滚的原子单元。*
