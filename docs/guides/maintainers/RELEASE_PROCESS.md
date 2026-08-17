# AIASys 维护者发布流程

本文档面向项目维护者，规定如何从 `dev` 分支稳定地发布一个新版本。

## 版本号规则

- 采用语义化版本（SemVer）：`MAJOR.MINOR.PATCH`
- 当前阶段以 beta 预发布为主：`X.Y.Z-beta.N`（如 `0.4.17-beta.1`）
- **0.4.x 阶段一律 PATCH +1，不 bump MINOR。** 这不是疏忽而是既有惯例：`v0.4.2` 到
  `v0.4.34` 全程没有一次 MINOR bump，含新功能的版本同样走 patch。0.x 阶段 SemVer 本身
  对 MINOR/PATCH 的约束就弱（公开 API 视为不稳定），而 fork 与上游版本号必须对得上，
  单方面跳到 0.5.0 只会制造混乱。
  - 判断新版本号时**不要**按「这批有没有新功能」去决定 minor/patch——按惯例 +1 即可。
  - 什么时候才 bump MINOR 或进 1.0：由维护者显式决定，不由改动内容自动推导。
- **承载版本号的文件共四处，必须全部一致**：
  - `package.json`（仓库根）
  - `apps/web/package.json`
  - `apps/desktop/package.json`
  - `apps/backend/pyproject.toml`

  根 `package.json` 的 version 目前没有任何消费者（CI 读的是 web / desktop 各自的
  package.json），但它必须一起同步：2026-08-15 实测它停在 `0.4.33` 而其余三处是
  `0.4.34`，同一个仓库里读到两个「当前版本」，已实际误导过判断。
  `scripts/dev/release.sh` 的 `VERSION_FILES` 数组是这四处的唯一列举点，
  新增承载版本号的文件时只改那里。

## 何时该发版

以下任一条件命中即触发发版，不靠感觉判断：

| 触发条件 | 阈值 | 依据 |
|---|---|---|
| 未发布提交数 | ≥ 30 个（`git rev-list --count <上个 tag>..dev`） | 历史相邻 tag 间提交数实测为 9/134/160/2/61/13/15/8/29，中位数约 15；30 已属积压区间 |
| 距上次 tag 时间 | ≥ 14 天且期间有用户可感知改动 | 历史发版间隔多在 1–8 天，14 天属明显异常 |
| 用户可感知的功能缺陷已修复 | 出现即尽快发 | 修复停在仓库里对用户等于没修 |

**不单独触发发版的改动**：纯测试、纯 CI、纯重构、纯格式、docs-only。这类改动可以攒着，
跟下一次由上表触发的发版一起走。

**反面教材（2026-08-16 实测）**：`v0.4.34` 之后积压了 118 个提交、跨度五周才发版，
其间包含 team 多 agent 协作、think 呈现重做、多个用户可感知缺陷修复。两条阈值都早已越线，
但因为没有成文判据，一直没人触发发版。这就是本节存在的原因。

## 发布前准备

1. 确认 `dev` 分支已合并所有待发布功能/修复
2. 确认 `dev` 分支 CI 通过（5 个 job：backend ubuntu / backend windows / web-check /
   desktop-check / e2e-lifecycle）
3. 确认 `docs/changelog/vX.Y.Z_YYYY-MM-DD.md` 已按 `docs/changelog/README.md` 规范编写
4. 跑一次 `./scripts/dev/release.sh --dry-run X.Y.Z` 确认前置检查全过
   （dry-run 零副作用，跑完自动还原版本号文件）

## 发布步骤

### 方式一：使用发布脚本（推荐）

```bash
git checkout main
git pull upstream main

# 演练模式，不实际提交
./scripts/dev/release.sh --dry-run X.Y.Z-beta.N

# 正式发布
./scripts/dev/release.sh X.Y.Z-beta.N
```

脚本会自动完成：
- 检查当前分支为 `main`
- 检查工作区干净
- 检查本地 `main` 与 `upstream/main` 一致
- 检查 changelog 文件存在
- 同步四处版本号（见 VERSION_FILES）
- 提交版本号变更
- 打 tag `vX.Y.Z-beta.N`
- 推送 `main` 分支

最后一步需要手动执行：

```bash
git push upstream vX.Y.Z-beta.N
```

### 方式二：手动发布

1. 创建 release PR：`dev` → `main`
2. 通过 CI 和 review 后合并
3. 本地切到 `main` 并拉取最新代码
4. 同步四处版本号（根/web/desktop package.json + backend/pyproject.toml）
5. 提交版本号变更：`chore(release): bump version to X.Y.Z-beta.N`
6. 打 tag：`git tag vX.Y.Z-beta.N`
7. 推送：`git push upstream main && git push upstream vX.Y.Z-beta.N`

## 发布后验证

1. 等待 `.github/workflows/ci-desktop.yml` 完成
2. 检查 release 产物：
  ```bash
  gh release view vX.Y.Z-beta.N --json assets
  ```
3. 确认产物包含：
  - `AIASys-X.Y.Z-beta.N-linux.zip`
  - `AIASys-X.Y.Z-beta.N.AppImage`
  - `AIASys-X.Y.Z-beta.N-arm64.dmg`
  - `AIASys-X.Y.Z-beta.N-arm64-mac.zip`
  - `AIASys-X.Y.Z-beta.N-win.zip`
  - `AIASys Setup X.Y.Z-beta.N.exe`

## 发布纪律

- 禁止直接 push 到 `main` 或 `dev`
- 管理员自己的改动也必须通过 PR
- 每个 release 必须有对应的 changelog
- 四处版本号必须一致（含仓库根 package.json）
- 发布前 CI 必须通过

## 回滚

如果发布后发现严重问题：

1. 在 GitHub 上删除错误的 release 和 tag
2. 修复问题并通过 PR 合并到 `dev`，再合并到 `main`
3. 使用新的版本号重新发布（不要复用已删除的 tag）
