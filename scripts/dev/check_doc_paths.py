#!/usr/bin/env python
"""文档路径核验：文档里指向源码的路径，必须真的存在。

存在理由：文档写错路径不会让任何检查变红，但会持续误导读它的人和 AI。
一次实测（2026-08-10）里，AI 因为一条不存在的目录路径，顺着错误前提编出
了整份「目录清单 + 能力字段表 + 行号」的幻觉报告——错的不是模型，是它
拿到的文档。代码重构改了目录名，文档没跟着改，这类漂移只能靠机器盯。

判据按文档类型分三档，因为它们承担的责任不同：

  current   架构 / 设计 / README / Skill，描述「当前」状态。路径失效即缺陷，
            本档失效会让脚本以非零码退出。
  changelog 历史快照，记录的是当时的路径。重构后失效属正常，只统计不报警。
  skill     与 current 同档，但解析基准额外含文档自身目录（Skill 内引用
            自带脚本时用的是相对 Skill 根的路径）。

两类噪音在设计上就排掉，不靠事后维护白名单：

  1. 围栏代码块（``` 包裹）内的路径一律跳过。文档里的代码块是示例——虚构
     项目的画像、JSON 配置样例、git 命令演示——里面的路径本就不必存在。
     正文里的路径才是「指路」，指错了才误导。
  2. 同行出现「已移除 / 已删除 / 已废弃 / 已归档 / 原 」等字样时跳过。文档
     说明「原 X 已移除」是正确写法，不该因为 X 真的不存在而报错。

剩下确实需要人工判定的，登记在 ALLOWLIST 并写明理由。

用法：
    python scripts/dev/check_doc_paths.py              # 核验，失效则非零退出
    python scripts/dev/check_doc_paths.py --self-test  # 自证检测器有效
    python scripts/dev/check_doc_paths.py --stats      # 含 changelog 的全量统计
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 不扫描的目录：依赖、产物、运行时数据
EXCLUDE_DIR_PARTS = {
    "node_modules",
    ".git",
    "playwright-report",
    "test-results",
    ".venv",
    "__pycache__",
    "dist",
}

# 路径解析的候选基准。文档常省略前缀写 `app/xxx`（相对 apps/backend）
# 或 `src/xxx`（相对 apps/web），逐个试，任一命中即算存在。
GLOBAL_BASES = (
    REPO_ROOT,
    REPO_ROOT / "apps" / "backend",
    REPO_ROOT / "apps" / "web",
    REPO_ROOT / "apps",
    REPO_ROOT / "apps" / "web" / "src",
)

# 首段限定为已知代码根，避免把散文里的 `scripts/tools`（英文的「或」）、
# `src/tests/docs`（「三个目录」简写）这类当成路径。
PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"((?:app|apps|src|docs|scripts|tests|infra|packages|e2e|\.github|\.team-skills)"
    r"/[A-Za-z0-9_./\-]+)"
)

FENCE_RE = re.compile(r"^\s*(```|~~~)")

# 同行含这些字样时，路径不存在属正常表述
EXEMPT_CONTEXT = ("已移除", "已删除", "已废弃", "已归档", "不再作为", "原 `")

# 模板占位符特征
PLACEHOLDER_TOKENS = ("YYYY", "MM-DD", "x.x.x", "X.Y.Z", "{", "}", "<", ">", "*")

# 需要人工判定的例外，每条必须写清理由。
# 新增条目前先自问：这是真例外，还是文档该改？倾向后者。
ALLOWLIST: dict[str, str] = {
    "infra/deploy/.env": "部署时由用户按 .env.example 自建，仓库内不存在",
    "apps/backend/data/app.db": "首次启动时由 create_all 生成",
    "apps/backend/data": "运行时会话数据目录，按需创建",
    "apps/backend/.docker-images": "镜像导出产物，构建时生成",
    "apps/desktop/.dist": "打包中间产物",
    "apps/desktop/dist": "electron-builder 输出目录，打包时生成",
    "apps/backend/node_modules": "后端 Node 运行环境，按需安装",
    ".team-skills/_archived": "Skill 归档目标位，首次归档时创建",
    "docs/product": "2026-04-12 已下沉归档，文档中作为历史说明出现",
    "docs/implementation-status": "同上，历史说明",
    "scripts/tools": "英文散文里的 scripts/tools 是「或」，非路径",
    "src/tests/docs": "中文散文里指 src、tests、docs 三个目录，非路径",
    "apps/backend/config.toml": "用户按 config.example.toml 自建，gitignore 不入库",
    "apps/web/dist": "前端构建产物，npm run build 生成",
    "apps/backend/.venv": "uv sync 创建的后端虚拟环境，gitignore 不入库",
    "apps/web/test-results/manual": "Playwright 手工套件输出目录，跑测试时生成",
    "apps/web/playwright-report/manual": "Playwright 手工套件 HTML 报告，跑测试时生成",
    "apps/backend/logs": "后端运行日志目录，首次启动时创建",
    "apps/web/node_modules": "npm ci 安装的前端依赖目录，gitignore 不入库",
}


@dataclass
class Finding:
    path: str
    doc: str
    line: int


def _iter_docs() -> list[Path]:
    out: list[Path] = []
    for p in REPO_ROOT.rglob("*.md"):
        if set(p.parts) & EXCLUDE_DIR_PARTS:
            continue
        # apps/backend/data 下是运行时产生的会话数据，不是文档
        if "/apps/backend/data/" in p.as_posix():
            continue
        out.append(p)
    return out


def _classify(rel_doc: str) -> str:
    if "/changelog/" in rel_doc or rel_doc.startswith("docs/changelog/"):
        return "changelog"
    if "/skill/" in rel_doc or "/skills/" in rel_doc or rel_doc.endswith("SKILL.md"):
        return "skill"
    return "current"


def _bases_for(doc: Path) -> tuple[Path, ...]:
    """Skill 内的 references/ 可能引用 Skill 根下的 scripts/，故往上找几层。"""
    extra: list[Path] = []
    d = doc.parent
    for _ in range(4):
        extra.append(d)
        if d == REPO_ROOT:
            break
        d = d.parent
    return GLOBAL_BASES + tuple(extra)


def _is_ignorable(raw: str, line: str) -> bool:
    if any(tok in raw for tok in PLACEHOLDER_TOKENS):
        return True
    if any(word in line for word in EXEMPT_CONTEXT):
        return True
    cleaned = raw.rstrip("/.,;:)")
    if cleaned in ALLOWLIST:
        return True
    return any(cleaned.startswith(f"{k}/") for k in ALLOWLIST)


def scan() -> tuple[dict[str, list[Finding]], dict[str, list[int]]]:
    """返回 (按档分组的失效清单, 按档分组的 [引用总数, 可解析数])。

    main 与 --self-test 共用本函数，保证自证检验的是真正在用的判定逻辑。
    """
    findings: dict[str, list[Finding]] = {"current": [], "changelog": [], "skill": []}
    stats: dict[str, list[int]] = {k: [0, 0] for k in findings}

    for doc in _iter_docs():
        rel_doc = doc.relative_to(REPO_ROOT).as_posix()
        kind = _classify(rel_doc)
        bases = _bases_for(doc)
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        in_fence = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in PATH_RE.finditer(line):
                raw = m.group(1)
                if _is_ignorable(raw, line):
                    continue
                cleaned = raw.rstrip("/.,;:)")
                stats[kind][0] += 1
                if any((b / cleaned).exists() for b in bases):
                    stats[kind][1] += 1
                else:
                    findings[kind].append(Finding(cleaned, rel_doc, lineno))
    return findings, stats


def _print_report(findings: dict[str, list[Finding]], stats: dict[str, list[int]]) -> None:
    for kind, label in (
        ("current", "架构/设计文档"),
        ("skill", "Skill 文档"),
        ("changelog", "changelog（历史快照，不报警）"),
    ):
        total, ok = stats[kind]
        print(f"{label}: 引用 {total}  可解析 {ok}  失效 {total - ok}")


def _report_failures(findings: dict[str, list[Finding]]) -> int:
    hard = findings["current"] + findings["skill"]
    if not hard:
        return 0
    print()
    print(f"发现 {len(hard)} 处失效路径引用（描述当前状态的文档）：")
    grouped: dict[str, list[Finding]] = {}
    for f in hard:
        grouped.setdefault(f.path, []).append(f)
    for path, refs in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {path}  ({len(refs)} 处)")
        for f in refs:
            print(f"    {f.doc}:{f.line}")
    print()
    print("修法：确认该对象的真实位置后改文档；对象已删除时改写为「原 X 已移除」；")
    print("确属例外（构建产物、用户自建文件）时登记到脚本的 ALLOWLIST 并写明理由。")
    return 1


def _self_test() -> int:
    """自证：检测器对已知失效路径必须报错，对已知存在路径必须放过。

    只验证判定函数本身，不改动仓库文件——注入真文件再恢复的风险不值得，
    判定逻辑是纯函数，直接喂输入即可。
    """
    ok = True

    # 1. 已知不存在的路径，正文引用，必须被判为失效
    bogus = "app/services/definitely_not_a_real_module.py"
    line = f"**实现位置**：`{bogus}`"
    if _is_ignorable(bogus, line) or any((b / bogus).exists() for b in GLOBAL_BASES):
        print(f"  FAIL 失效路径未被判定为失效: {bogus}")
        ok = False
    else:
        print(f"  PASS 失效路径被判为失效: {bogus}")

    # 2. 已知存在的路径必须放过（防止检测器把一切都判失效而「恒红」）
    real = "app/services/agent/message_content.py"
    if not any((b / real).exists() for b in GLOBAL_BASES):
        print(f"  FAIL 真实路径被判为失效: {real}（基准配置错了）")
        ok = False
    else:
        print(f"  PASS 真实路径被放过: {real}")

    # 3. 围栏代码块内的失效路径必须被跳过
    findings, _ = scan()
    fenced_leak = [f for f in findings["current"] + findings["skill"] if "alembic" in f.path]
    if fenced_leak:
        print("  FAIL 代码块内的示例路径泄漏成失效项（围栏识别失效）")
        ok = False
    else:
        print("  PASS 代码块内示例路径已跳过")

    # 4. 「已移除」豁免必须生效
    removed_line = "> 原 `app/services/execution_logger.py` 已移除"
    if not _is_ignorable("app/services/execution_logger.py", removed_line):
        print("  FAIL 「已移除」说明未被豁免")
        ok = False
    else:
        print("  PASS 「已移除」说明已豁免")

    print()
    if ok:
        print("self-test 通过：检测器既能抓错，也不会恒红。")
        return 0
    print("self-test 失败：检测器本身有问题，先修它再看核验结果。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="自证检测器有效，不核验仓库")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不以失败退出")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    findings, stats = scan()
    _print_report(findings, stats)
    if args.stats:
        return 0
    return _report_failures(findings)


if __name__ == "__main__":
    sys.exit(main())
