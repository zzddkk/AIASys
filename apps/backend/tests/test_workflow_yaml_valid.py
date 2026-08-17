"""GitHub Actions workflow 文件的可解析性测试。

这道门守的是一类「看着有门、实际没门」的失效：workflow 文件 YAML 语法坏掉时，
GitHub 只会在 Actions 页面标一个 "Invalid workflow file"，不会阻止合并、
也不会在别的 workflow 里报错。于是那个 workflow 从此静默不跑，而仓库里
「有这道检查」的印象还在。

2026-08-10 实测发现两个这样的文件（都不是当天改的，坏了很久没人知道）：

  pr-auto-review.yml   第 49 行 `}cd apps/backend && uv run ruff check --fix app/`
                       顶格在 column 1，脱离了 run: | 的 block scalar 缩进。
                       起因是 shell 里用 `FIX_CMDS="${FIX_CMDS:+${FIX_CMDS}\n}..."`
                       这种「字符串里嵌裸换行」的写法，换行后的部分丢了缩进。

  pr-merge-check.yml   第 45 行起的中文 markdown 段落同样顶格，脱离缩进。

两者都是往多行字符串里塞换行/markdown 时缩进没跟上。这类错误肉眼极难发现——
文件看起来只是「有一行没缩进」，而后果是整个 workflow 报废。

注：本测试只保证「能被解析」与「结构上像个 workflow」，不保证语义正确。
语义层面的检查（步骤能否真跑通）由各 workflow 自己在 CI 上体现。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def test_workflow_dir_is_discoverable() -> None:
    """先证明检索方法有效，再采信下面的逐文件结果。

    空列表有两个解释：真的没有 workflow，或者路径拼错了。不加这条的话，
    路径一旦写错，下面的参数化测试会「零用例通过」，看起来一片绿。
    """
    assert WORKFLOW_DIR.is_dir(), f"workflow 目录不存在: {WORKFLOW_DIR}"
    files = _workflow_files()
    assert len(files) >= 5, (
        f"只发现 {len(files)} 个 workflow 文件，少于预期，检索路径可能不对: {WORKFLOW_DIR}"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_yaml_parses(path: Path) -> None:
    """每个 workflow 必须能被 YAML 解析。"""
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        pytest.fail(
            f"{path.name} YAML 解析失败，该 workflow 在 GitHub 上会被标为 "
            f"Invalid workflow file 并静默不跑:\n{exc}"
        )

    assert isinstance(doc, dict), f"{path.name} 顶层应是映射，实际 {type(doc).__name__}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_has_required_keys(path: Path) -> None:
    """必须有触发条件与 jobs，否则等于一个不会运行的空壳。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    # PyYAML 按 YAML 1.1 把裸 `on` 解析成布尔 True，两种键都接受。
    trigger = doc.get("on", doc.get(True))
    assert trigger is not None, f"{path.name} 缺少 on:（没有触发条件）"

    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} 缺少 jobs: 或为空"

    for job_name, job in jobs.items():
        assert isinstance(job, dict), f"{path.name} 的 job {job_name} 结构异常"
        has_steps = isinstance(job.get("steps"), list) and job["steps"]
        # 复用型 job 用 uses: 而非 steps:
        has_uses = bool(job.get("uses"))
        assert has_steps or has_uses, f"{path.name} 的 job {job_name} 既没有 steps 也没有 uses"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_jobs_declare_timeout(path: Path) -> None:
    """每个 job 必须声明 timeout-minutes。

    没有它时，挂起的 job 会跑到 GitHub 默认上限 6 小时。挂起比失败更糟：
    看不到结论、烧配额、还阻塞后续队列。ci.yml 的注释里已经写明这条纪律，
    这里把它变成机器检查——纪律写在注释里只对读注释的人生效。
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}

    missing = [
        name
        for name, job in jobs.items()
        if isinstance(job, dict) and not job.get("uses") and "timeout-minutes" not in job
    ]
    assert not missing, (
        f"{path.name} 的这些 job 没有 timeout-minutes: {missing}。"
        "挂起时会占用 runner 到默认 6 小时上限。"
    )
