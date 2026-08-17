# e2e/manual —— 人工审查脚本，不是自动化测试

这个目录下的 10 个 `.spec.ts` **一条断言都没有**（`expect(` 出现 0 次），输出全是截图。
它们是用来「把界面跑到某个状态、截图下来给人看」的工具，结论必须由人看图得出。

放在这里而不是 `e2e/` 根层，是为了让这件事在目录结构上就能看出来。

## 为什么单独隔开

它们此前散在 `e2e/` 根层，与真正的回归测试混在一起，造成两个问题：

**一，制造「有 e2e 覆盖」的假象。** 目录里躺着十几个 `.spec.ts`，看起来覆盖不错，
实际上其中只有一个有断言。零断言脚本无论产品坏成什么样都不会失败——它和不存在
的检查等价，但比不存在更糟，因为它提供了虚假的安全感。

**二，它们本来就跑不起来。** `playwright.lifecycle.config.ts` 的 `testDir` 是
`./e2e/lifecycle`，而 playwright 会用 `testDir` 过滤命令行传入的路径参数。根层文件
既不会被套件自动扫到，手工点名也会报 `no tests found`，只能靠临时改配置跑。
现在有了 `playwright.manual.config.ts`，它们才有稳定入口。

同期发现的一个真实代价：`workspace-delete-dialog.smoke.spec.ts` 是根层唯一有断言的
文件（7 条），写好之后**从未被执行过一次**。它已经改造并搬进 `e2e/lifecycle/`，
现在会随套件运行（首次实跑：通过，3.7 秒）。

## 怎么跑

这些脚本需要一个起好的全栈环境。复用 lifecycle 的服务启停逻辑，只换配置：

```bash
# 从仓库根执行。服务的起、等、收都由脚本负责，跑完自动清理残留进程。
PLAYWRIGHT_CONFIG=playwright.manual.config.ts \
  bash scripts/dev/run_lifecycle_playwright.sh e2e/manual/ux-audit-chat-layout.spec.ts
```

截图落在 `apps/web/test-results/manual/`，HTML 报告在
`apps/web/playwright-report/manual/`，与回归套件的产物分开存放。

## 新增文件放哪

按有没有断言分：

- **有断言、要防回归** → `e2e/lifecycle/`。那里会被 CI 跑到，可以用 `support.ts` 的
  API helper 自己造前置状态（`createWorkspace` / `createSession` 等），不要依赖
  「运行时恰好有数据」。
- **只截图给人看** → 本目录。文件名建议保留 `ux-audit-` 前缀，一眼能认出性质。

如果一个脚本从「看截图」演化成了「有明确期望」，把期望写成 `expect` 并搬去
`e2e/lifecycle/`——留在这里它永远不会失败，等于白写。

## 2026-08-11 追加：db-preview-smoke.spec.ts

从 `lifecycle/` 移过来的，原因是它是一条**假绿**：

- 0 个 `expect()`，结论全靠 `console.log` 打印，人不看输出就等于没测；
- 找不到「资源」按钮时直接 `return`，测试照样记为通过；
- 全量跑的日志里它打印 `Panel check: { sql: false, schema: false }`——两个目标面板
  都没找到，仍然计入 passed；
- 截图路径写死 `/home/ke/projects/AIASys/artifacts/screenshots`，在 Windows 上被
  解析成 `C:\home\ke\...`，实测在 C 盘根下造出了一个 568K 的野目录。

**遗留缺口**：内置数据库预览目前没有任何自动化断言覆盖。要补一条真测试，需要先确定
「资源 → 内置数据库 → SQL / 表结构」这条路径在当前 UI 里的准确入口（这条 spec 里三种
兜底定位器全部失配，说明入口已经变过）。补的时候按 `lifecycle/` 的规矩来：断言可观察结果，
不要用 `if (!visible) return` 兜底放过。
