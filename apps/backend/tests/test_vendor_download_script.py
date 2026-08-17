"""vendor 二进制自动下载脚本的可启动性测试。

守的是一个「从未成功过」的缺陷，而不是「偶尔失败」的缺陷。

apps/backend/scripts/download_vendor_binaries.py 由 app/core/vendor_binaries.py 用

    subprocess.run([sys.executable, str(script)], cwd=<仓库根>)

拉起。这两个路径都不含 app 包所在目录：cwd 是仓库根（app 在 apps/backend/ 下），
sys.path[0] 是脚本自己所在的 scripts/ 目录。于是脚本顶层的 `from app.core...`
必然 ModuleNotFoundError。

后果是 uv / fnm / sqlite-vec 缺失时的自动补齐能力一直是空的，开发者只能手工装。
而这条报错级别只有 WARNING、混在启动日志里、启动照常继续，所以长期没人追。

测试策略：用 runpy 以 run_name != "__main__" 加载脚本，只触发顶层 import，
不执行 main()。这样既精准命中缺陷位置（顶层 import），又不会真去下载几十 MB
二进制、不依赖网络，可以放心进 CI。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.core.subprocess_utils import subprocess_kwargs

# tests/ → apps/backend → apps → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "download_vendor_binaries.py"


def test_script_exists() -> None:
    """脚本路径必须与 vendor_binaries.py 里的 script_candidates 第一项一致。

    这条不是形式主义：脚本被移动过一次而调用方没跟着改的话，
    vendor_binaries 会静默走到「未找到脚本，跳过自动下载」的 debug 分支。
    """
    assert SCRIPT.is_file(), f"脚本不存在: {SCRIPT}"


def _run_toplevel_only(cwd: Path) -> subprocess.CompletedProcess[str]:
    """以调用方的真实 cwd 加载脚本顶层代码，但不执行 main()。"""
    code = (
        "import runpy, sys\n"
        f"runpy.run_path(r'{SCRIPT}', run_name='__imported_for_test__')\n"
        "print('TOPLEVEL_OK')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        **subprocess_kwargs(),
    )


def test_toplevel_import_works_from_repo_root() -> None:
    """从仓库根启动（真实调用方式）时，顶层 import 不得 ModuleNotFoundError。

    反向探针：注释掉脚本里的 sys.path 自举后，这条会以
    `ModuleNotFoundError: No module named 'app'` 失败。
    """
    result = _run_toplevel_only(REPO_ROOT)

    assert "ModuleNotFoundError" not in (result.stderr or ""), (
        f"顶层 import 失败，说明 sys.path 自举缺失或失效。\nstderr:\n{result.stderr}"
    )
    assert result.returncode == 0, f"退出码 {result.returncode}\nstderr:\n{result.stderr}"
    assert "TOPLEVEL_OK" in (result.stdout or "")


@pytest.mark.parametrize("cwd_name", ["repo_root", "backend_root", "scripts_dir"])
def test_toplevel_import_works_from_any_cwd(cwd_name: str) -> None:
    """cwd 不该影响可导入性。

    自举用的是 __file__ 推导的绝对路径，与 cwd 无关；这组参数化就是钉住这一点，
    避免有人改成依赖 cwd 的相对路径（那样只在某个目录下能跑）。
    """
    cwd = {
        "repo_root": REPO_ROOT,
        "backend_root": BACKEND_ROOT,
        "scripts_dir": BACKEND_ROOT / "scripts",
    }[cwd_name]

    result = _run_toplevel_only(cwd)
    assert "ModuleNotFoundError" not in (result.stderr or ""), (
        f"从 {cwd_name} 启动时顶层 import 失败\nstderr:\n{result.stderr}"
    )
    assert result.returncode == 0, f"从 {cwd_name} 启动退出码 {result.returncode}"


def test_script_declares_path_bootstrap_before_app_import() -> None:
    """自举必须出现在 app.* 导入之前。

    顺序反了同样会 ModuleNotFoundError，但上面的子进程测试若因环境里恰好装了
    app 包而通过，就会漏掉这种排序错误。所以这里直接检查源码顺序。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    lines = src.splitlines()

    def first_line_index(predicate) -> int:
        for i, raw in enumerate(lines):
            line = raw.strip()
            # 跳过注释行：脚本的说明注释里就引用了 `from app.core...` 这段文字，
            # 按裸字符串搜索会命中注释而不是真正的 import 语句（第一版写法正是
            # 这样误报的：注释在 695、真 import 在 1045，于是「顺序反了」）。
            if line.startswith("#"):
                continue
            if predicate(line):
                return i
        return -1

    bootstrap_idx = first_line_index(lambda ln: ln.startswith("sys.path.insert"))
    app_import_idx = first_line_index(
        lambda ln: ln.startswith("from app.") or ln.startswith("import app.")
    )

    assert bootstrap_idx != -1, "脚本缺少 sys.path 自举（非注释行里找不到 sys.path.insert）"
    assert app_import_idx != -1, "脚本未导入 app.*（若已改为不依赖 app，请同步删掉本测试）"
    assert bootstrap_idx < app_import_idx, (
        f"sys.path 自举（第 {bootstrap_idx + 1} 行）必须在 "
        f"`from app.`（第 {app_import_idx + 1} 行）之前，否则导入仍会失败"
    )


def test_vendor_binaries_points_at_this_script() -> None:
    """调用方的候选路径列表必须仍然包含本脚本。"""
    caller = BACKEND_ROOT / "app" / "core" / "vendor_binaries.py"
    src = caller.read_text(encoding="utf-8")
    assert "download_vendor_binaries.py" in src, (
        "vendor_binaries.py 不再引用 download_vendor_binaries.py，"
        "两边已脱节，请同步更新本测试与调用方"
    )
