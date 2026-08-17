#!/usr/bin/env python3
"""
下载 AIASys backend 所需的 vendor 二进制依赖。

用于本地开发场景（直接 uvicorn 启动 backend，不经过 desktop 的 prepare-runtime）。
会按需下载：
    - uv          -> vendor/uv/<platform>/
    - fnm         -> vendor/node/<platform>/
    - sqlite-vec  -> vendor/sqlite-vec/<platform>/

这些二进制原本由 apps/desktop/scripts/prepare-runtime.cjs 在打包桌面版时下载。
本脚本让纯后端开发也能自动补齐它们。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

# Windows 上把自己的 stdout/stderr 强制成 UTF-8。
#
# 本脚本的进度输出含中文（"[vendor] 平台: ..."），而 Windows 的 Python 默认按
# 控制台代码页编码 stdout。GitHub Actions 的 windows runner 是 cp1252，一遇中文
# 就 UnicodeEncodeError 并让整个脚本非零退出——2026-08-16 实测后果是 vendor 二进制
# 没下载、后端起不来、e2e-lifecycle 全套失败，而报错信息只有一行 charmap 编码错，
# 完全指不到「打印中文」这个真实原因。
#
# 放在这里而不是只在 CI 里设 PYTHONIOENCODING：本脚本还会被
# app/core/vendor_binaries.py 以子进程方式调起，那条路径不经过 CI 的 env，
# 依赖外部环境变量等于把修复交给调用方去记得。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

# 必须在导入 app.* 之前自举 sys.path。
#
# 本脚本由 app/core/vendor_binaries.py 以 `subprocess.run([sys.executable, script],
# cwd=<仓库根>)` 拉起。那两个路径都不含 app 包：cwd 是仓库根（app 在
# apps/backend/ 下），而 sys.path[0] 是脚本自己所在的 scripts/ 目录。于是顶层
# `from app.core...` 必然 ModuleNotFoundError。
#
# 实测后果不是「偶尔失败」而是「从未成功过」：每次后端启动都留下一条
#
#   vendor 二进制自动下载失败 (exit 1): ModuleNotFoundError: No module named 'app'
#
# 也就是 uv / fnm / sqlite-vec 缺失时的自动补齐能力一直是空的，开发者只能手工装。
# 这条报错混在启动日志里且级别只有 WARNING，启动照常继续，所以长期没人追。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.subprocess_utils import subprocess_kwargs  # noqa: E402


def get_platform_slug() -> str:
    """将 Python 的 platform 信息映射到项目内部使用的 platform slug。"""
    system = platform.system()
    machine = platform.machine()

    if system == "Darwin":
        if machine == "arm64":
            return "darwin-arm64"
        if machine in ("x86_64", "AMD64"):
            return "darwin-x64"
    elif system == "Linux":
        if machine == "arm64":
            return "linux-arm64"
        if machine in ("x86_64", "AMD64"):
            return "linux-x64"
    elif system == "Windows":
        if machine in ("x86_64", "AMD64", "x64"):
            return "win-x64"

    raise RuntimeError(f"不支持的平台: {system} {machine}")


def run_node_download_script(script_name: str, platform_slug: str, repo_root: Path) -> None:
    """调用 desktop/scripts 下对应的 node 下载脚本。"""
    script_path = repo_root / "apps" / "desktop" / "scripts" / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"下载脚本不存在: {script_path}")

    cmd = ["node", str(script_path), platform_slug]
    print(f"[vendor] 执行: {' '.join(cmd)}")
    env = dict(os.environ)
    env["PYTHON"] = sys.executable
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        **subprocess_kwargs(),
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"{script_name} 执行失败 (exit {result.returncode})")
    print(result.stdout, end="")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    platform_slug = get_platform_slug()

    print(f"[vendor] 平台: {platform_slug}")
    print(f"[vendor] 仓库根目录: {repo_root}")

    scripts = [
        "download-uv-binary.cjs",
        "download-fnm-binary.cjs",
        "download-sqlite-vec-binary.cjs",
    ]

    for script in scripts:
        try:
            run_node_download_script(script, platform_slug, repo_root)
        except Exception as e:
            print(f"[vendor] 失败: {e}", file=sys.stderr)
            return 1

    print("[vendor] 全部 vendor 二进制准备完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
