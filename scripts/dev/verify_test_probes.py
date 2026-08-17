#!/usr/bin/env python
"""测试有效性探针：往源码里注入缺陷，验证测试真的会失败。

存在理由：一个从不失败的检查与一个不存在的检查等价。测试全绿只证明
「当前代码没触发这些断言」，不证明「断言真的在约束什么」。断言写歪
（比较了两个常量、断言了必然成立的类型、mock 掉了被测逻辑本身）时，
测试会永久绿着，而它守护的行为其实早已无人看管。

做法是变异测试的定向版本：对每条关键断言，人工指定一个「本该被它抓住」
的缺陷，注入源码后跑对应测试，要求**必须失败**。注入后仍然通过的，
就是假测试，脚本以非零码退出并点名。

用法：
    python scripts/dev/verify_test_probes.py            # 跑全部探针
    python scripts/dev/verify_test_probes.py -k origin  # 按名字过滤
    python scripts/dev/verify_test_probes.py --list     # 只列出探针

安全性：每个探针在注入前把目标文件原文读入内存，无论测试结果如何都在
finally 中原样写回；脚本退出时会校验所有目标文件的内容与开跑前一致，
不一致则显著报错（这种情况说明恢复逻辑本身出了问题，需要 git checkout）。

已知的恢复漏洞与本脚本的兜底：进程被强杀（Ctrl+C 打断、系统关机、CI 超时
SIGKILL）时 finally 不会执行，注入的缺陷会残留在 working tree 里——2026-08-13
实测发生过一次，靠人眼 review diff 才发现。因此 main() 在开跑前先扫一遍所有
探针的 replace 片段是否已经出现在 target 里，命中即拒绝运行并点名文件，
防止「上一轮的注入」被本轮 commit 进仓库。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
WEB_ROOT = REPO_ROOT / "apps" / "web"

_MESSAGE_PROTOCOL = (
    "apps/backend/app/services/agent/runtime_backends/aiasys/llm_clients/message_protocol.py"
)
_AI_MESSAGE_CONTENT = "apps/web/src/components/chat/AiMessageContent/index.tsx"
_TOOL_BLOCK = "apps/web/src/components/chat/AiMessageContent/ToolBlock.tsx"
_RUNTIME_LLM_CONFIG = "apps/backend/app/services/agent/models/llm_config.py"
_USER_LLM_CONFIG = "apps/backend/app/models/llm_provider.py"
_CLIENT_FACTORY = "apps/backend/app/services/agent/runtime_backends/aiasys/llm_clients/__init__.py"


@dataclass(frozen=True)
class Probe:
    """一个探针：往 target 里把 find 换成 replace，期望 tests 失败。

    name        探针标识，用于 -k 过滤与报告。
    target      相对**仓库根**的源码路径（前后端统一口径）。
    find        要被替换的原文片段，必须在文件中**唯一**出现。
    replace     注入后的片段（即人为制造的缺陷）。
    tests       测试选择器：pytest 用相对 apps/backend 的路径，
                vitest 用相对 apps/web 的路径或目录前缀。
    rationale   这个缺陷对应什么真实风险，即「为什么值得为它写断言」。
    runner      "pytest"（后端）或 "vitest"（前端）。
    """

    name: str
    target: str
    find: str
    replace: str
    tests: tuple[str, ...]
    rationale: str
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    runner: str = "pytest"


_PROJECTION_TESTS = ("tests/test_message_protocol_projection.py",)
_DISPLAY_HINT_TESTS = ("src/components/chat/AiMessageContent",)
_KNOB_REACHABILITY_TESTS = ("tests/test_llm_config_knob_reachability.py",)

PROBES: tuple[Probe, ...] = (
    Probe(
        name="internal-field-leak-openai",
        target=_MESSAGE_PROTOCOL,
        find="        converted: dict[str, Any] = {\n"
        '            "role": role,\n'
        '            "content": message_content_to_openai_input(content),\n'
        "        }",
        replace="        converted: dict[str, Any] = {\n"
        '            "role": role,\n'
        '            "content": message_content_to_openai_input(content),\n'
        '            "origin": message.get("origin"),\n'
        "        }",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "internal_only_fields_never_reach_provider_payload",
        ),
        rationale="投影时顺手带上内部字段，是把 origin/turn_n 发给服务商的最常见写法",
    ),
    Probe(
        name="unrecognized-origin-kept",
        target=_MESSAGE_PROTOCOL,
        find='    origin = message.get("origin")\n    if origin in (',
        replace='    origin = message.get("origin")\n    if origin is not None or origin in (',
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "unrecognized_origin_is_dropped",
        ),
        rationale="放宽 origin 白名单后，拼错的 origin 会按 visible 兜底，把注入消息显示给用户",
    ),
    Probe(
        name="role-not-coerced",
        target=_MESSAGE_PROTOCOL,
        find='    if raw_role in {"system", "assistant", "tool"}:\n'
        "        return raw_role\n"
        '    return "user"',
        replace='    if raw_role in {"system", "assistant", "tool"}:\n'
        "        return raw_role\n"
        "    return raw_role  # type: ignore[no-any-return]",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "unknown_role_is_coerced_to_user",
        ),
        rationale="未知 role 透传会让请求带上服务商不认识的角色，整轮被拒",
    ),
    Probe(
        name="anthropic-system-not-extracted",
        target=_MESSAGE_PROTOCOL,
        find='        if role == "system":\n            text = extract_message_text(content).strip()',
        replace="        if False:  # probe\n            text = extract_message_text(content).strip()",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "anthropic_extracts_system or anthropic_joins_multiple_systems",
        ),
        rationale="system 残留在 messages 里会被 Anthropic 端点直接拒绝",
    ),
    Probe(
        name="anthropic-thinking-order-reversed",
        target=_MESSAGE_PROTOCOL,
        find='            anthropic_messages.append({"role": "assistant", "content": content_blocks})',
        replace="            content_blocks.reverse()  # probe\n"
        '            anthropic_messages.append({"role": "assistant", "content": content_blocks})',
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "thinking_block_precedes_text",
        ),
        rationale="thinking 块必须先于 text，顺序错了 Anthropic 会拒绝该轮",
    ),
    Probe(
        name="empty-tool-arguments-regression",
        target=_MESSAGE_PROTOCOL,
        find='        return raw_arguments if raw_arguments.strip() else "{}"',
        replace="        return raw_arguments",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "empty_arguments_become_empty_json_object",
        ),
        rationale="空串不是合法 JSON；这正是本轮修掉的真缺陷，探针防它复发",
    ),
    Probe(
        name="responses-empty-assistant-kept",
        target=_MESSAGE_PROTOCOL,
        find="            assistant_text = extract_message_text(content)\n"
        "            if assistant_text:",
        replace="            assistant_text = extract_message_text(content)\n"
        "            if True:  # probe",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "responses_drops_tool_call_message_with_empty_content",
        ),
        rationale="Responses 协议不接受 content 为空的 assistant 消息",
    ),
    Probe(
        name="blank-identifier-kept-as-empty",
        target=_MESSAGE_PROTOCOL,
        find='    tool_call_id = message.get("tool_call_id")\n'
        "    if isinstance(tool_call_id, str) and tool_call_id.strip():",
        replace='    tool_call_id = message.get("tool_call_id")\n'
        "    if isinstance(tool_call_id, str):",
        tests=_PROJECTION_TESTS,
        extra_args=(
            "-k",
            "blank_identifiers_are_dropped",
        ),
        rationale="留空串会让下游 get(key, default) 拿到空串而非默认值，进入静默中间态",
    ),
    Probe(
        name="display-hint-unknown-origin-hidden",
        target=_MESSAGE_PROTOCOL,
        find='    return _ORIGIN_TO_DISPLAY_HINT.get(origin, "visible")',
        replace='    return _ORIGIN_TO_DISPLAY_HINT.get(origin, "hidden")',
        tests=("tests/test_display_hint.py",),
        extra_args=(
            "-k",
            "unknown_origin",
        ),
        rationale="未知 origin 兜底成 hidden 会静默吞掉内容，用户看不到也不知道丢了什么",
    ),
    Probe(
        name="thinking-fake-empty-signature",
        target=_MESSAGE_PROTOCOL,
        find="        if isinstance(signature, str) and signature.strip():",
        replace="        if True:  # probe",
        tests=("tests/test_aiasys_message_ir.py",),
        extra_args=(
            "-k",
            "signature or thinking",
        ),
        rationale="伪造空签名会被 Claude 校验拒绝，是历史上真实踩过的坑",
    ),
    # ---- 前端（vitest）----
    # 这一层与后端那十个探针是互补关系，不是重复：后端管「哪些消息该给用户看」的
    # 判定，前端管这个判定有没有被真正落实到 DOM 上。后端映射对了、前端渲染写错，
    # 用户照样会看到注入内容。
    Probe(
        name="display-hint-not-in-merge-condition",
        target=_AI_MESSAGE_CONTENT,
        find="        MERGEABLE_TYPES.has(seg.type) &&\n"
        '        (lastSeg.display_hint ?? "visible") === (seg.display_hint ?? "visible")',
        replace="        MERGEABLE_TYPES.has(seg.type)",
        tests=_DISPLAY_HINT_TESTS,
        rationale=(
            "2026-08-10 实测的真缺陷：合并只比 type 时，[visible, hidden] 会渲染成 "
            "FIRST_VISIBLEMIDDLE_HIDDENLAST_VISIBLE，system/compaction_summary 的注入内容"
            "直接显示在界面上；反向则让正常回答整段消失"
        ),
        runner="vitest",
    ),
    Probe(
        name="hidden-segment-still-rendered",
        target=_AI_MESSAGE_CONTENT,
        find='      if (seg.display_hint === "hidden") {',
        replace='      if (seg.display_hint === "__never_matches__") {',
        tests=_DISPLAY_HINT_TESTS,
        rationale="hidden 判断失效等于 display_hint 机制整体失效，隐藏内容全部进 DOM",
        runner="vitest",
    ),
    # ---- 配置开关的可达性 ----
    # 这三个探针守的是一类特殊缺陷：算法与单测都对，但开关无法被任何用户设上，
    # 于是功能在生产里永久关闭，且完全静默（getattr 拿不到字段只会返回 None）。
    # 2026-08-10 的 reasoning_in_content_tag 就是这样上线的——31 条 splitter 单测
    # 全绿，而 <think> 剥离从未生效过一次。
    Probe(
        name="runtime-knob-field-missing",
        target=_RUNTIME_LLM_CONFIG,
        find="    reasoning_in_content_tag: str | None = Field(\n"
        "        default=None,\n"
        "        description=\"推理内容直接写在 content 里时的包裹标签名（如 'think'）。\"",
        replace="    renamed_by_probe: str | None = Field(\n"
        "        default=None,\n"
        "        description=\"推理内容直接写在 content 里时的包裹标签名（如 'think'）。\"",
        tests=_KNOB_REACHABILITY_TESTS,
        rationale=(
            "运行时模型少一个字段，_get_provider_attr 的 getattr(obj, key, None) 就永远返回 "
            "None——功能关闭且无任何报错，正是那次事故的机制"
        ),
    ),
    Probe(
        name="knob-not-forwarded-by-serializer",
        target=_USER_LLM_CONFIG,
        find="        if self.reasoning_in_content_tag is not None:\n"
        '            config["reasoning_in_content_tag"] = self.reasoning_in_content_tag\n'
        "        return config\n"
        "\n"
        "    def mask_api_key",
        replace="        return config\n\n    def mask_api_key",
        tests=_KNOB_REACHABILITY_TESTS,
        rationale=(
            "缺陷是两段式的：字段声明了但序列化没转发，值一样到不了运行时。只守前一段会漏掉这一半"
        ),
    ),
    Probe(
        name="undeclared-knob-slips-through",
        target=_CLIENT_FACTORY,
        find='    reasoning_format = _get_provider_attr(provider, "reasoning_format")',
        replace='    reasoning_format = _get_provider_attr(provider, "reasoning_format")\n'
        '    _probe_knob = _get_provider_attr(provider, "probe_undeclared_knob")',
        tests=_KNOB_REACHABILITY_TESTS,
        rationale=(
            "直接测那条可泛化守卫的用途：有人新增开关的读取却忘了声明字段时，必须在 CI "
            "就红，而不是等用户发现「配了没反应」"
        ),
    ),
    # ---- P0 借鉴项：最后一问预览的注入隔离 ----
    # 列表预览用最后一条「真实用户消息」，系统注入（system_notice/contextual_user/
    # compaction_summary）不能被当成用户的最后一问展示，否则会把系统提示泄露到会话列表。
    Probe(
        name="last-preview-includes-system-injection",
        target="apps/backend/app/services/workspace_registry.py",
        find='                if message.get("origin") not in (None, "user", "forked"):\n'
        "                    continue\n",
        replace="",
        tests=("tests/test_conversation_last_user_preview.py",),
        extra_args=("-k", "system_injected or compaction or origin_filter"),
        rationale=(
            "丢掉 origin 过滤后，列表预览会取到最后一条 system_notice/contextual_user 注入，"
            "把系统提示当成用户的最后一问展示在会话列表"
        ),
    ),
    # ---- P0 借鉴项：审批 Esc 必须映射拒绝，不得静默放行 ----
    Probe(
        name="approval-esc-maps-to-allow",
        target="apps/web/src/components/CapabilityConfirmationCard/index.tsx",
        find="      e.preventDefault();\n      void handleReject();",
        replace='      e.preventDefault();\n      void handleApprove("once");',
        tests=("src/components/__tests__/capabilityConfirmationTakeover.test.tsx",),
        rationale=(
            "Esc 若映射为允许，用户按 Esc 想取消却静默放行危险操作；对齐 codex 的 "
            "dismissal 永不映射为继续"
        ),
        runner="vitest",
    ),
    # ---- 设计 token 守卫自身的有效性 ----
    # 这三个探针测的对象不是单元测试，而是 committed/check-design-tokens.mjs 这个守卫脚本。
    # 理由与其他探针一致：守卫失效时不会报错，只会安静地输出「通过，0 处违规」，
    # 而它保护的视觉一致性早已无人看管。三条规则各注入一处真实违规，
    # 要求守卫必须以非零码退出。
    #
    # 注入点刻意选了三种不同的 JSX 形态：单行属性、跨多行属性、className 与
    # size 混用——AST 遍历若退化成行匹配，跨多行那一条会第一个失守。
    Probe(
        name="token-guard-catches-hardcoded-font-size",
        target="apps/web/src/components/chat/AiMessageContent/ToolBlock.tsx",
        find="pl-3 text-caption leading-relaxed",
        replace="pl-3 text-[12px] leading-relaxed",
        tests=(),
        rationale=(
            "R1 失效则字号重新各写各的。本项目改造前全站硬编码字号 881 处，"
            "10px/11px/12px/13px 四种混用，同一列表里相邻两行字号不同"
        ),
        runner="tokens",
    ),
    Probe(
        name="token-guard-catches-control-height-override",
        target="apps/web/src/components/CanvasEditor/CanvasDialogs.tsx",
        find='<Button size="sm" onClick={onCommit}>',
        replace='<Button className="h-8" onClick={onCommit}>',
        tests=(),
        rationale=(
            "R2 失效则控件高度回到手写状态（改造前 h-7 142 处、h-8 227 处），"
            "同一行里的按钮与下拉框差 4px，视觉上参差不齐"
        ),
        runner="tokens",
    ),
    Probe(
        name="token-guard-catches-dialog-size-override",
        target="apps/web/src/components/CanvasEditor/CanvasDialogs.tsx",
        find='<DialogContent size="xs">',
        replace='<DialogContent className="max-w-[520px]">',
        tests=(),
        rationale="R3 失效则每个弹窗自己拍宽度，同类弹窗宽度不一致、窄屏下溢出行为也不统一",
        runner="tokens",
    ),
)


def _run_pytest(tests: tuple[str, ...], extra_args: tuple[str, ...]) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", *extra_args]
    completed = subprocess.run(
        cmd,
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _run_vitest(tests: tuple[str, ...], extra_args: tuple[str, ...]) -> tuple[int, str]:
    """跑前端 vitest。

    用 shell=True 是因为 Windows 下 npx 是 .cmd，直接 exec 会 FileNotFoundError。
    vitest 没有 --no-header，也不接受 pytest 的 -q，因此参数表与 pytest 分开维护。
    """
    cmd = "npx vitest run " + " ".join([*tests, *extra_args])
    completed = subprocess.run(
        cmd,
        cwd=WEB_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _run_tokens(extra_args: tuple[str, ...]) -> tuple[int, str]:
    """跑设计 token 守卫。

    这一路 runner 守的不是单元测试，而是一个静态守卫脚本。它同样需要探针：
    守卫的价值全在「能不能抓住违规」上，而它的失效方式很隐蔽——AST 遍历漏掉
    某种 JSX 写法、正则少一个分支、CONTROL_TAGS 少登记一个原语，扫描都会照常
    输出「通过，0 处违规」。2026-08-16 实测过一次同类失效：行正则版守卫报 23 处，
    AST 版在同一份代码上报 245 处，差的 222 处是跨多行 JSX 属性。当时若没有对照
    就会把行正则版当成已经干净。

    tests 字段对本 runner 无意义（守卫总是全量扫 src），保留空元组。
    """
    cmd = "node scripts/committed/check-design-tokens.mjs " + " ".join(extra_args)
    completed = subprocess.run(
        cmd,
        cwd=WEB_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _run_tests(probe: Probe) -> tuple[int, str]:
    if probe.runner == "vitest":
        return _run_vitest(probe.tests, probe.extra_args)
    if probe.runner == "tokens":
        return _run_tokens(probe.extra_args)
    return _run_pytest(probe.tests, probe.extra_args)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_count(output: str, runner: str = "pytest") -> int:
    """判断「被测的东西真的跑起来了」，返回 0 表示没跑。

    探针的前提是那些测试真的被选中跑了。若 -k 过滤写歪导致 0 selected，
    pytest 会以「no tests ran」退出，不能算探针有效——那只是没测。

    tokens runner 没有用例概念，判据换成守卫是否真的扫过文件：脚本第一行会打印
    「AST 扫描 N 个源文件」。缺这行说明 node 没起来、依赖缺失或路径不对，
    此时的非零退出码来自崩溃而不是检出违规，必须判为 broken 而不是 effective。
    """
    if runner == "tokens":
        return 1 if "AST 扫描" in output else 0
    for line in reversed(output.splitlines()):
        for marker in ("passed", "failed", "error"):
            if marker in line:
                return 1
    return 0


def _execute_probe(probe: Probe) -> tuple[str, str]:
    """执行单个探针，返回 (结论, 说明)。

    结论取三值：
      "effective"   注入后测试失败 → 该测试真的在约束这个行为
      "false-green" 注入后测试仍通过 → 假测试
      "broken"      探针自身不可用（锚点失配 / 没有用例被选中）

    注入与恢复都在本函数内闭环，异常路径也走 finally 恢复。
    main 与 --self-test 共用本函数，保证自证检验的是真正在用的判定逻辑，
    而不是一份平行实现。
    """
    path = REPO_ROOT / probe.target
    original = path.read_text(encoding="utf-8")

    occurrences = original.count(probe.find)
    if occurrences != 1:
        # 匹配不到或匹配多处时注入内容不可控，必须报错而不是跳过。
        # 静默跳过会让探针「看起来跑过了」，正是本脚本要消灭的假绿。
        return "broken", f"find 片段出现 {occurrences} 次，需唯一（源码可能已改动）"

    try:
        path.write_text(original.replace(probe.find, probe.replace), encoding="utf-8")
        code, output = _run_tests(probe)
        ran = _selected_count(output, probe.runner)
    finally:
        path.write_text(original, encoding="utf-8")

    if ran == 0:
        return "broken", "0 条用例被选中，-k 过滤可能写歪"
    if code != 0:
        return "effective", "注入后失败（测试有效）"
    return "false-green", "注入后仍然通过 → 假测试！"


def _self_test_vitest() -> int:
    """前端侧自证，逻辑与 pytest 侧完全相同，只换目标文件与 runner。

    单独一个函数而不是在 _self_test 里加分支，是为了不给原有两个 Probe 定义
    加一层缩进——那种改法会让 diff 里全是缩进噪音，真正的改动反而看不见。
    """
    harmless = Probe(
        name="self-test-harmless-comment",
        target=_AI_MESSAGE_CONTENT,
        find="    // 合并连续的同类型 segments",
        replace="    // merge consecutive same-type segments (self-test)",
        tests=_DISPLAY_HINT_TESTS,
        rationale="改注释不改行为，测试必须仍然通过",
        runner="vitest",
    )
    bogus_anchor = Probe(
        name="self-test-bogus-anchor",
        target=_AI_MESSAGE_CONTENT,
        find="function aFunctionThatDoesNotExistAnywhere() {",
        replace="noop",
        tests=_DISPLAY_HINT_TESTS,
        rationale="锚点不存在，探针不可用",
        runner="vitest",
    )
    return _report_self_test("vitest", harmless, bogus_anchor)


def _self_test_tokens() -> int:
    """设计 token 守卫侧自证，与另两侧同构。

    无害案例选的是「合法档位之间的替换」（text-caption → text-sm，两者都在
    允许列表里）：守卫必须放过它。这一条同时兜住一类真实风险——如果哪天有人把
    R1 的判据从「方括号任意值」放宽/收紧成正则匹配 text-\\w+，Tailwind 内置档
    会被一并误报，那时守卫就会天天拦住合法代码，最终被人加 --no-verify 绕过。
    """
    harmless = Probe(
        name="self-test-tokens-legal-swap",
        target=_TOOL_BLOCK,
        find="pl-3 text-caption leading-relaxed",
        replace="pl-3 text-sm leading-relaxed",
        tests=(),
        rationale="两个档位都合法，守卫必须放过",
        runner="tokens",
    )
    bogus_anchor = Probe(
        name="self-test-tokens-bogus-anchor",
        target=_TOOL_BLOCK,
        find='className="a-class-that-does-not-exist-anywhere"',
        replace="noop",
        tests=(),
        rationale="锚点不存在，探针不可用",
        runner="tokens",
    )
    return _report_self_test("tokens", harmless, bogus_anchor)


def _report_self_test(runner: str, harmless: Probe, bogus_anchor: Probe) -> int:
    """跑两个反向案例并汇报。两侧自证共用，避免判定标准出现两份。"""
    print(f"自证（{runner}）：验证判定逻辑能识别假绿与坏探针\n")
    ok = True

    verdict, note = _execute_probe(harmless)
    passed = verdict == "false-green"
    ok &= passed
    print(f"  {'✓' if passed else '✗'} 无害改动 → 期望 false-green，实得 {verdict}（{note}）")

    verdict, note = _execute_probe(bogus_anchor)
    passed = verdict == "broken"
    ok &= passed
    print(f"  {'✓' if passed else '✗'} 坏锚点   → 期望 broken，实得 {verdict}（{note}）")

    print("\n自证" + ("通过：判定逻辑有效" if ok else "失败：判定逻辑不可信，探针结果无意义"))
    return 0 if ok else 1


def _self_test(runner: str = "pytest") -> int:
    """自证本脚本的判定逻辑没坏。

    两个反向案例，都必须被正确识别：
      1. 无害改动（只改注释文字）→ 测试不该失败 → 必须判为 false-green；
      2. 不存在的锚点 → 必须判为 broken。

    若这两个案例被判成 effective，说明判定逻辑恒真——那本脚本给出的
    「N 个探针全部有效」就是一句空话。自证失败时返回非零。

    runner 决定用哪一侧的目标：CI 的两个 job 各自只有一半运行环境
    （backend job 没有 node_modules，web job 没有 python venv），
    自证必须能跟着 --runner 走，否则等于要求每个 job 都装全套依赖。
    """
    if runner == "tokens":
        return _self_test_tokens()
    if runner == "vitest":
        return _self_test_vitest()
    harmless = Probe(
        name="self-test-harmless-comment",
        target=_MESSAGE_PROTOCOL,
        find="# origin → display_hint 映射表",
        replace="# origin -> display_hint mapping table (self-test)",
        tests=("tests/test_message_protocol_projection.py",),
        extra_args=(
            "-k",
            "unrecognized_origin_is_dropped",
        ),
        rationale="改注释不改行为，测试必须仍然通过",
    )
    bogus_anchor = Probe(
        name="self-test-bogus-anchor",
        target=_MESSAGE_PROTOCOL,
        find="def a_function_that_does_not_exist_anywhere():",
        replace="pass",
        tests=("tests/test_message_protocol_projection.py",),
        rationale="锚点不存在，探针不可用",
    )

    return _report_self_test("pytest", harmless, bogus_anchor)


def _detect_leftover_injections(probes: list[Probe]) -> list[str]:
    """开跑前检测残留注入：target 处于「find 已被换成 replace」的状态。

    这不是理论风险——2026-08-13 实测：探针进程被打断，finally 未执行，
    empty-tool-arguments-regression 的注入在 working tree 里躺到人工 review
    才被发现。不检测的话，下一轮探针会把自己的 find 换成 replace 再换回来，
    「恢复」到的正是残留状态，退出校验也发现不了；更糟的是开着残留直接开发，
    缺陷会被 commit 进仓库。

    判据是双条件，缺一不可：
      1. replace 的标记行在场——标记取 replace 中不属于 find 的第一行（strip 后），
         因为多数探针的 replace 含与 find 相同的行（删改型注入的保留部分），
         用共有行会大面积误报正常源码；
      2. find 的标记行缺席——注入恰恰是 find → replace 的替换，find 没了才说明
         替换真的发生过。只看条件 1 时，`return config` 这类短 replace 在正常
         文件里本来就有，会误拦干净的工作区（2026-08-13 加检测当天就误报过）。
    """
    leftovers: list[str] = []
    for probe in probes:
        path = REPO_ROOT / probe.target
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue

        def marker_of(snippet: str) -> str:
            return next(
                (line.strip() for line in snippet.splitlines() if line.strip()),
                "",
            )

        replace_marker = next(
            (
                line.strip()
                for line in probe.replace.splitlines()
                if line.strip() and line.strip() not in {l.strip() for l in probe.find.splitlines()}
            ),
            "",
        )
        find_marker = marker_of(probe.find)
        if not replace_marker or not find_marker:
            continue
        if find_marker not in content and replace_marker in content:
            leftovers.append(f"{probe.target}（探针 {probe.name} 的注入残留）")
    return leftovers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", dest="filter", default=None, help="按探针名字子串过滤")
    parser.add_argument("--list", action="store_true", help="只列出探针，不执行")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="自证：注入一个测试本不该抓住的无害改动，本脚本必须报出「假测试」",
    )
    parser.add_argument(
        "--runner",
        choices=("pytest", "vitest", "tokens", "all"),
        default="all",
        help=(
            "只跑指定 runner 的探针。CI 里必须分开跑：backend job 不装 apps/web 的 "
            "node_modules，vitest 探针在那里必然失败；web-check job 反之。"
            "tokens 是设计 token 守卫的探针，同样需要 apps/web 的 node_modules。"
        ),
    )
    args = parser.parse_args()

    # 残留注入检测必须在任何写入动作（自证也是写入）之前。
    # 检测范围跟随后续实际要跑的探针集合：有 -k/--runner 过滤时只查那批 target。
    future_probes = [p for p in PROBES if not args.filter or args.filter in p.name]
    if args.runner != "all":
        future_probes = [p for p in future_probes if p.runner == args.runner]
    leftovers = _detect_leftover_injections(future_probes)
    if leftovers:
        print("!! 检测到上一轮探针的注入残留，拒绝运行。", file=sys.stderr)
        print("   以下文件已含有某个探针的 replace 片段，说明上次运行被强杀、", file=sys.stderr)
        print("   finally 未恢复。请 git checkout 以下文件后重试：", file=sys.stderr)
        for item in leftovers:
            print(f"     {item}", file=sys.stderr)
        return 4

    if args.self_test:
        # --runner 必须透传给自证：web-check job 只装 node_modules 不装 pytest，
        # backend job 反之。曾在这里忽略 runner 恒跑 pytest 侧，CI 的
        # `--self-test --runner vitest` 实际去执行 pytest，job 里没有 pytest，
        # 输出无「passed」可解析 → 误判 broken → 自证恒失败（2026-08-13 CI 首跑实测）。
        # 默认 all 时两侧都自证：本地一条命令验全套，CI 分开各跑各的。
        runners = ("pytest", "vitest", "tokens") if args.runner == "all" else (args.runner,)
        rc = 0
        for runner in runners:
            rc |= _self_test(runner)
        return rc

    probes = [p for p in PROBES if not args.filter or args.filter in p.name]
    if args.runner != "all":
        probes = [p for p in probes if p.runner == args.runner]

    if args.list:
        for probe in probes:
            print(f"{probe.name:42s} {probe.rationale}")
        return 0

    if not probes:
        print(f"没有匹配 -k {args.filter!r} 的探针", file=sys.stderr)
        return 2

    baseline = {probe.target: _digest(REPO_ROOT / probe.target) for probe in probes}

    print(f"探针验证：{len(probes)} 个（注入缺陷后测试必须失败）\n")
    effective: list[str] = []
    false_green: list[str] = []
    broken: list[str] = []

    for probe in probes:
        verdict, note = _execute_probe(probe)
        if verdict == "effective":
            effective.append(probe.name)
            print(f"  ✓ {probe.name}: {note}")
        elif verdict == "false-green":
            false_green.append(probe.name)
            print(f"  ✗ {probe.name}: {note}")
            print(f"      风险: {probe.rationale}")
        else:
            broken.append(f"{probe.name}（{note}）")
            print(f"  ✗ {probe.name}: {note}")

    print("\n=== 恢复校验 ===")
    tampered = [
        target for target, digest in baseline.items() if _digest(REPO_ROOT / target) != digest
    ]
    if tampered:
        print("  !! 以下文件未恢复原状，请立即 git checkout：", file=sys.stderr)
        for target in tampered:
            print(f"     {target}", file=sys.stderr)
        return 3
    print(f"  所有 {len(baseline)} 个目标文件已恢复原状（哈希一致）")

    print(f"\n有效 {len(effective)} / 假测试 {len(false_green)} / 探针本身坏掉 {len(broken)}")
    if false_green or broken:
        for item in broken:
            print(f"  需修探针: {item}", file=sys.stderr)
        for item in false_green:
            print(f"  需修测试: {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
