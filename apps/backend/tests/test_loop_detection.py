"""死循环检测的单元测试：thinking 流循环 + 截断续写六道守卫。

算法移植自 step-code（stepfun-ai/Step-Realtime-CLI，MIT），见
`app/services/agent/runtime_backends/aiasys/loop_detection.py` 的文件头。

测试分两部分，分开的理由是它们回答不同的问题：

- **第一部分「移植保真度」**：用例数据逐条取自参考实现的 `tests/agent/continuation.test.ts`
  与 `tests/agent/thinkingLoop.test.ts`（2026-08-08 快照），断言口径保持一致。
  它回答的是「Python 版与 TS 版行为是否一致」——用自己造的数据测自己的实现，
  只能证明实现自洽，证明不了移植没走样。
- **第二部分「AIASys 侧行为」**：节流偏离、原地更新语义、用户可见文案。
  这些是移植时有意偏离或本地新增的部分，参考实现没有对应用例。
"""

from __future__ import annotations

from app.services.agent.runtime_backends.aiasys.loop_detection import (
    ContinuationState,
    ContinuationVerdict,
    ThinkingLoopDetector,
    advance_continuation,
    check_continuation_safety,
    describe_continuation_stop,
    find_repeating_tail,
    initial_continuation_state,
)


def st(
    first_text: str = "第一轮写到一半的正文",
    *,
    count: int = 0,
    last_chunk: str | None = None,
    stalled_streak: int = 0,
) -> ContinuationState:
    """造一个干净的初始状态（对应参考实现测试里的同名 helper）。"""
    state = initial_continuation_state(first_text)
    state.count = count
    state.last_chunk = last_chunk
    state.stalled_streak = stalled_streak
    return state


# ===========================================================================
# 第一部分：移植保真度（用例取自参考实现测试）
# ===========================================================================

# --- findRepeatingTail：尾部周期性重复检测 ---------------------------------


def test_fidelity_long_unit_three_repeats_hits() -> None:
    """长片段精确重复 3 次即命中（>= 20 字符周期）。"""
    unit = "好的，我来继续写这一部分的内容与说明。"
    long = unit + "补足到二十字符以上。"
    assert len(long) >= 20
    period = find_repeating_tail("前面是正常内容。" + long * 3)
    assert period is not None
    assert len(long) % period == 0


def test_fidelity_long_unit_two_repeats_is_ok() -> None:
    """长片段只重复 2 次不算异常（可能是修辞或结构对称）。"""
    unit = "这是一段足够长的、超过二十个字符的内容片段。"
    assert find_repeating_tail("开头。" + unit * 2) is None


def test_fidelity_mid_period_needs_six() -> None:
    """中等周期（5~19 字符）要求 6 次以上。"""
    unit = "继续写下去"  # 5 字
    assert find_repeating_tail("正文。" + unit * 5) is None
    assert find_repeating_tail("正文。" + unit * 6) is not None


def test_fidelity_short_period_needs_twenty() -> None:
    """短周期（<= 4 字符）要求 20 次以上——否则会误伤正常排版。"""
    assert find_repeating_tail("结论。" + "哈" * 10) is None
    assert find_repeating_tail("结论。" + "哈" * 20) is not None


def test_fidelity_normal_formatting_not_flagged() -> None:
    """正常排版不误伤：分隔线 / 省略号 / 列表前缀 / 缩进 / 表格 / 边框。

    这组是整个模块最重要的用例——任何一条被判成「复读」都会中断正常任务。
    """
    cases = [
        "## 小节标题\n\n" + "-" * 12 + "\n正文继续。",
        "他停顿了一下……然后接着说……最后总结……",
        "清单：\n- [ ] 第一项\n- [ ] 第二项\n- [ ] 第三项\n- [ ] 第四项\n",
        "```\n" + "    缩进行内容\n" * 4 + "```",
        "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n",
        "=" * 16 + "\n边框标题\n" + "=" * 16,
    ]
    for case in cases:
        assert find_repeating_tail(case) is None, f"不应误判为复读: {case[:30]!r}"


def test_fidelity_too_short_returns_none() -> None:
    """太短的字符串直接返回 None（不足以构成周期）。"""
    assert find_repeating_tail("") is None
    assert find_repeating_tail("ab") is None


def test_fidelity_repeat_only_at_head_not_flagged() -> None:
    """重复段在尾部才算；只在开头重复不命中（尾部才代表当前卡住）。"""
    unit = "这是一段足够长的、超过二十个字符的内容片段。"
    text = unit * 3 + "后来模型恢复正常并写出了新的内容，这段不重复。"
    assert find_repeating_tail(text) is None


# --- checkContinuationSafety：六道守卫 -------------------------------------


def test_fidelity_healthy_continuation_is_safe() -> None:
    """正常推进 → safe。"""
    verdict = check_continuation_safety("接着写出了新的一段内容。", st(), 10)
    assert verdict.safe
    assert verdict.reason is None


def test_fidelity_guard1_no_progress() -> None:
    """① 空内容 / 纯空白 → no_progress（确定性，无阈值）。"""
    for chunk in ("", "   \n\t "):
        verdict = check_continuation_safety(chunk, st(), 10)
        assert not verdict.safe
        assert verdict.reason == "no_progress"


def test_fidelity_guard2_identical_to_previous() -> None:
    """② 与上一轮完全相同 → identical_to_previous。"""
    chunk = "这一段和上轮一模一样。"
    verdict = check_continuation_safety(chunk, st("首轮", last_chunk=chunk), 10)
    assert not verdict.safe
    assert verdict.reason == "identical_to_previous"


def test_fidelity_guard2_one_char_difference_is_safe() -> None:
    """② 只差一个字就不算完全相同（判据是严格相等，不是相似）。"""
    state = st("首轮", last_chunk="这一段和上轮几乎一样。")
    assert check_continuation_safety("这一段和上轮几乎一样！", state, 10).safe


def test_fidelity_guard3_restarted_from_beginning() -> None:
    """③ 开头与首轮相同 → restarted_from_beginning。"""
    first = "好的，我来从头介绍这个方案的全部内容，包括背景与动机。"
    verdict = check_continuation_safety(first + "（后面又写了一遍）", st(first), 10)
    assert not verdict.safe
    assert verdict.reason == "restarted_from_beginning"


def test_fidelity_guard3_skipped_when_first_head_too_short() -> None:
    """③ 首轮正文短于阈值时跳过该判定——短开头不足以作为特征。

    这条同时钉住参考实现修复前的 bug：headOf 固定截 80 字符时，
    首轮短于 80 的情况下比较永远不等，判定形同不存在。
    """
    assert check_continuation_safety("好的。接着往下写新的内容。", st("好的。"), 10).safe


def test_fidelity_guard3_effective_between_20_and_80() -> None:
    """③ 首轮长于 20 但短于 80 字符时判定仍然有效（修复点）。"""
    first = "我先说明这个方案的整体背景与设计动机所在。"
    assert 20 <= len(first) < 80
    verdict = check_continuation_safety(first + "然后又重复了一遍。", st(first), 10)
    assert not verdict.safe
    assert verdict.reason == "restarted_from_beginning"


def test_fidelity_guard4_repeating_tail_reports_period() -> None:
    """④ 尾部复读 → repeating_tail，并带出周期长度供诊断。"""
    unit = "我需要继续完成这个任务的剩余部分的内容。"  # 20 字，走「3 次」档
    assert len(unit) >= 20
    verdict = check_continuation_safety("正常开头。" + unit * 3, st(), 10)
    assert not verdict.safe
    assert verdict.reason == "repeating_tail"
    assert verdict.detail is not None and verdict.detail > 0


def test_fidelity_guard5_stalled_on_third_low_round() -> None:
    """⑤ 连续低产 3 轮 → stalled；前两轮不停。"""
    tiny = "好的。"
    assert check_continuation_safety(tiny, st("首轮", stalled_streak=0), 10).safe
    assert check_continuation_safety(tiny, st("首轮", stalled_streak=1), 10).safe
    verdict = check_continuation_safety(tiny, st("首轮", stalled_streak=2), 10)
    assert not verdict.safe
    assert verdict.reason == "stalled"


def test_fidelity_guard5_normal_output_clears_streak() -> None:
    """⑤ 一次正常产出会清零 stalled 计数（偶发短输出不该累积）。"""
    state = st("首轮", stalled_streak=2)
    advance_continuation("这是一段足够长的正常续写内容，超过二十字符。", state)
    assert state.stalled_streak == 0


def test_fidelity_guard6_max_continues() -> None:
    """⑥ 达到次数上限 → max_continues。"""
    verdict = check_continuation_safety("正常新内容。", st("首轮", count=9), 10)
    assert not verdict.safe
    assert verdict.reason == "max_continues"


def test_fidelity_zero_max_continues_disables() -> None:
    """maxContinues = 0 → 直接关闭自动续写。"""
    verdict = check_continuation_safety("正常新内容。", st(), 0)
    assert not verdict.safe
    assert verdict.reason == "max_continues"


def test_fidelity_deterministic_guard_takes_priority() -> None:
    """确定性判据优先于阈值判据：同时命中时报更根本的那条。"""
    unit = "我需要继续完成这个任务的剩余部分内容。"
    chunk = unit * 3
    state = st("首轮", last_chunk=chunk)
    verdict = check_continuation_safety(chunk, state, 10)
    assert not verdict.safe
    assert verdict.reason == "identical_to_previous"


# --- advanceContinuation：状态推进 -----------------------------------------


def test_fidelity_advance_accumulates_and_keeps_first_head() -> None:
    """累加轮数、记住本轮内容、保留首轮开头。"""
    state = initial_continuation_state("首轮正文开头很长足够作为判据使用")
    first_head = state.first_head
    advance_continuation("第二轮内容，这一段足够长不会触发龟速判定。", state)
    assert state.count == 1
    assert state.last_chunk == "第二轮内容，这一段足够长不会触发龟速判定。"
    assert state.first_head == first_head
    assert state.stalled_streak == 0


def test_fidelity_advance_accumulates_stalled_streak() -> None:
    """低产轮累加 stalled_streak。"""
    state = initial_continuation_state("首轮")
    advance_continuation("短。", state)
    advance_continuation("也短。", state)
    assert state.stalled_streak == 2
    assert state.count == 2


# --- thinkingLoop：思考流死循环检测 ----------------------------------------


def test_fidelity_thinking_short_period_repeat() -> None:
    """短周期逐字复读（「的」×N）→ 判定循环。"""
    detector = ThinkingLoopDetector()
    # 先凑够 MIN_CHARS（15 字 × 20 = 300，仍不足 400）
    assert not detector.ingest("让我先思考一下这个问题的背景。" * 20).looping
    assert detector.ingest("的" * 200).looping


def test_fidelity_thinking_paragraph_repeat() -> None:
    """段落级周期重复（同一段话反复输出）→ 判定循环。"""
    detector = ThinkingLoopDetector()
    para = "首先我需要分析这个问题的核心矛盾，它涉及到多个层面的因素，需要逐一排查确认。"
    verdict = detector.ingest(para * 12)
    assert verdict.looping
    assert verdict.sample is not None


def test_fidelity_thinking_normal_reasoning_not_flagged() -> None:
    """正常推理文本（无重复）→ 不判定。"""
    detector = ThinkingLoopDetector()
    text = "".join(
        f"第{i}步分析：因素{i}与因素{i + 1}的关系需要考察，"
        f"因为{i * 7}和{i * 13}的比值影响了结论{i}的成立条件。"
        for i in range(40)
    )
    assert not detector.ingest(text).looping


def test_fidelity_thinking_list_template_not_flagged() -> None:
    """列表/枚举式重复（结构相同内容不同）→ 不判定。

    避免代码/清单场景误报：模板一致但内容递增，是合理重复而非死循环。
    """
    detector = ThinkingLoopDetector()
    text = "".join(
        f"{i + 1}. 选项{i + 1}的评估结果是{'通过' if i % 2 == 0 else '不通过'}，"
        f"理由是指标{i}的读数为{i * 3}。\n"
        for i in range(30)
    )
    assert not detector.ingest(text).looping


def test_fidelity_thinking_fires_only_once() -> None:
    """触发后不再重复触发（fired 一次性）。"""
    detector = ThinkingLoopDetector()
    assert detector.ingest("的" * 500).looping
    assert not detector.ingest("的" * 100).looping


def test_fidelity_thinking_below_min_chars() -> None:
    """短文本不触发（MIN_CHARS 门槛）。"""
    detector = ThinkingLoopDetector()
    assert not detector.ingest("的" * 50).looping


def test_fidelity_thinking_reset_allows_redetect() -> None:
    """reset 后可重新检测。"""
    detector = ThinkingLoopDetector()
    assert detector.ingest("的" * 500).looping
    detector.reset()
    assert detector.text() == ""
    assert not detector.ingest("正常内容").looping
    # reset 清掉了 fired，同样的病态输入应再次命中
    assert detector.ingest("的" * 500).looping


# ===========================================================================
# 第二部分：AIASys 侧行为（移植偏离与本地新增）
# ===========================================================================


def test_local_throttle_defers_detection_within_interval() -> None:
    """节流（移植偏离 1）：累计增量不足检测间隔时不做检测。

    参考实现每个 delta 都全量扫尾部，在 Python 下对长思考流是 O(n^2)。
    本用例锁住偏离本身——调整 _CHECK_INTERVAL 时它应该失败，提醒同步更新文档。

    构造上要把节流与 MIN_CHARS 门槛隔离开：先用一次正常内容把累积推过 400 字符，
    之后每次只喂 10 个「的」。从第一次喂起尾部就已满足短周期判定（周期 1 需 8 次重复），
    若没有节流应当立即命中；实际要到累计增量跨过 200 才报，命中被推迟了 19 次。
    """
    detector = ThinkingLoopDetector()
    # 用递增内容构造真正不重复的长文本——把同一句话重复 N 次凑长度会命中长重复路径，
    # 提前置 fired，后面就测不到节流了（这个坑踩过一次）。
    warmup = "".join(f"第{i}步先确认指标{i}的读数是否落在合理区间内。" for i in range(20))
    assert len(warmup) > 400
    assert not detector.ingest(warmup).looping
    for _ in range(19):
        assert not detector.ingest("的" * 10).looping  # 累计 190 < 200，不检测
    verdict = detector.ingest("的" * 10)  # 累计 200，跨过间隔
    assert verdict.looping
    assert verdict.sample == "的"


def test_local_throttle_does_not_block_large_single_delta() -> None:
    """单次大增量不受节流影响：一次喂进超过间隔的量应立即检测。"""
    detector = ThinkingLoopDetector()
    assert detector.ingest("的" * 500).looping


def test_local_advance_mutates_in_place() -> None:
    """原地更新（移植偏离 3）：参考实现返回新对象，这里改 state 本身。

    理由是 AIASys 侧 state 挂在会话实例上，返回新对象要靠调用方记着回写，容易漏。
    """
    state = initial_continuation_state("首轮正文足够长可以作为判据使用的内容")
    returned = advance_continuation("新内容，足够长的一段续写文本内容。", state)
    assert returned is None  # 不返回新对象
    assert state.count == 1
    assert state.last_chunk == "新内容，足够长的一段续写文本内容。"


def test_local_check_does_not_mutate_state() -> None:
    """检查函数是纯函数：不修改传入状态（与参考实现一致）。"""
    state = st("首轮", count=2, last_chunk="上轮内容", stalled_streak=1)
    before = (state.count, state.last_chunk, state.first_head, state.stalled_streak)
    check_continuation_safety("短。", state, 10)
    after = (state.count, state.last_chunk, state.first_head, state.stalled_streak)
    assert before == after


def test_local_state_reset_clears_everything() -> None:
    """新一轮用户消息开始时清空全部状态。"""
    state = ContinuationState(count=3, last_chunk="x", first_head="y", stalled_streak=2)
    state.reset()
    assert state.count == 0
    assert state.last_chunk is None
    assert state.first_head is None
    assert state.stalled_streak == 0


def test_local_stop_message_distinguishes_reasons() -> None:
    """不同停止原因给不同说明——都说「已达上限」用户无法判断发生了什么。

    这是本地新增：参考实现的文案走 i18n 层，不在算法模块内。
    """
    no_progress = describe_continuation_stop(ContinuationVerdict(safe=False, reason="no_progress"))
    repeating = describe_continuation_stop(
        ContinuationVerdict(safe=False, reason="repeating_tail", detail=37)
    )
    assert "零进展" in no_progress
    assert "复读" in repeating
    assert "37" in repeating
    assert no_progress != repeating


def test_local_stop_message_wrapped_for_frontend() -> None:
    """文案带 <system> 包裹，与既有 system_warning 事件格式一致。"""
    text = describe_continuation_stop(ContinuationVerdict(safe=False, reason="stalled", detail=3))
    assert text.startswith("<system>")
    assert text.rstrip().endswith("</system>")


def test_local_every_stop_reason_has_message() -> None:
    """六个停止原因都要有对应文案，不能落到兜底串上。"""
    reasons = [
        "no_progress",
        "identical_to_previous",
        "restarted_from_beginning",
        "repeating_tail",
        "stalled",
        "max_continues",
    ]
    rendered = {
        r: describe_continuation_stop(ContinuationVerdict(safe=False, reason=r))  # type: ignore[arg-type]
        for r in reasons
    }
    assert len(set(rendered.values())) == len(reasons), "存在两个原因共用同一文案"
