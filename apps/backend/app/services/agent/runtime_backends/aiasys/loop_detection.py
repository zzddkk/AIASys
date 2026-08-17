"""死循环检测：thinking 流循环 + 截断续写守卫。

本模块为纯算法，无 IO、无框架依赖，便于穷举单测。

## 来源与许可

算法移植自 step-code（stepfun-ai/Step-Realtime-CLI，MIT License，
Copyright (c) 2026 stepfun-ai）的 `src/agent/thinkingLoop.ts` 与
`src/agent/continuation.ts`，2026-08-08 移植。阈值取值与分级理据一并保留——
这些注释说明了「为什么是这个值」，比代码本身更难重建，删掉等于丢失设计依据。

移植时的**有意偏离**（三处，各有理由，见对应位置注释）：
1. thinking 检测加了节流（`_CHECK_INTERVAL`）：原实现每个 delta 都全量扫尾部，
   在 Python 下对长思考流是 O(n^2)，会拖慢流式响应。
2. 长重复路径的窗口重复计数保留「允许重叠」的语义（手写 find 循环而非 str.count），
   与原实现严格一致——str.count 是非重叠计数，会让检测更保守、行为分叉。
3. 续写守卫的次数上限从常量改为入参，对齐 AIASys 既有的 `_continuation_count` 上限 3。

## 两类循环的分工

- `ThinkingLoopDetector`：思考**流式中途**的周期性复读。思考型模型在难任务上会反复
  输出同一段直到 max_tokens 耗尽、正文零输出。在中途检测可以省掉整段无效思考的 token。
- `check_continuation_safety`：正文被 max_tokens 截断后**自动续写**过程中的病态循环。
  与前者是两个不同阶段，互不替代。

两者都不负责「工具调用重复」——那由 `session_tools.py` 的相似度检测（3/5/8 三档）管。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 一、thinking 流死循环检测
# ---------------------------------------------------------------------------

#: 检测窗口大小（字符）。取 50：小于常见列表项/代码行的重复单元，大于大多数标点抖动。
_WINDOW = 50
#: 一个窗口在更早内容中出现 >= 该次数，计为「重复」。1 次出现 = 初次，>=2 次 = 开始重复。
_REPEAT_THRESHOLD = 3
#: 末尾连续命中窗口数 >= 该值，判定为「循环进行中」。2 个窗口 = 至少 100 字符仍在重复。
_CONSECUTIVE_TAIL_HITS = 2
#: 短周期循环的最小重复次数：末尾周期 p 的单元重复 >= 该次数才判定。
_SHORT_PERIOD_MIN_REPEATS = 8
#: 短周期的最大周期长度：超过即交给长重复路径。
_SHORT_PERIOD_MAX = 12
#: 触发检测的最小流长度：思考太短不可能成循环，避免开头误判。
_MIN_CHARS = 400
#: 【移植偏离 1】累积多少新字符才做一次检测。
#:
#: 原实现每个 thinking_delta 都调 detect()，而 detect() 的长重复路径要在累积文本里
#: 做 find 扫描（O(n)）。思考流几万字符、delta 数千个时，总代价是 O(n^2)，
#: 在 Python 下足以拖慢流式响应。取 200：小于一个检测窗口的 4 倍，
#: 最坏情况下推迟 200 字符才发现循环——相对于「整段思考白烧」的代价可以忽略。
_CHECK_INTERVAL = 200


@dataclass(frozen=True)
class ThinkingLoopVerdict:
    """thinking 循环判定结果。"""

    #: 是否判定为死循环。
    looping: bool
    #: 命中的重复单元预览，供提示文案展示。
    sample: str | None = None
    #: 重复次数（长重复路径的命中次数，或短周期路径的重复单元数）。
    repeats: int | None = None


_NOT_LOOPING = ThinkingLoopVerdict(looping=False)


class ThinkingLoopDetector:
    """思考流死循环检测器。

    每次 thinking / reasoning 增量到来时 `ingest`，返回当前判定。
    检测算法只看「流末尾」与「更早内容」的关系，天然避开代码/列表类内容在中部的合理重复：

    1. **短周期循环（逐字复读）**：流末尾存在周期 <= 12 的精确循环（如「的的的的」「…，…，…，」）。
    2. **长重复（段落级）**：流末尾的 50 字符块在更早内容中出现过 >= 3 次，
       且末尾连续 2 个窗口全部命中（确认循环仍在继续，而非历史上一段恰好重复）。

    设计约束是**宁漏不误报**：误报触发「中止思考」会打断正常推理，故阈值从严。
    同一回合只触发一次（`_fired`），避免每帧重复提示。
    """

    __slots__ = ("_buf", "_fired", "_since_last_check")

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._fired = False
        self._since_last_check = 0

    def ingest(self, delta: str) -> ThinkingLoopVerdict:
        """吃进一段思考增量，返回判定。已触发过则本回合不再触发。"""
        if not delta:
            return _NOT_LOOPING
        self._buf.append(delta)
        if self._fired:
            return _NOT_LOOPING
        self._since_last_check += len(delta)
        if self._since_last_check < _CHECK_INTERVAL:
            return _NOT_LOOPING
        self._since_last_check = 0
        verdict = self._detect()
        if verdict.looping:
            self._fired = True
        return verdict

    def text(self) -> str:
        """当前累积的思考文本（供注入诱导时截取上下文）。"""
        return "".join(self._buf)

    def reset(self) -> None:
        """回合结束时清空。检测器按回合复用，不跨回合累积。"""
        self._buf.clear()
        self._fired = False
        self._since_last_check = 0

    # -- 内部 ---------------------------------------------------------------

    def _detect(self) -> ThinkingLoopVerdict:
        buf = "".join(self._buf)
        # list 合并后回填，避免下次再拼一遍
        self._buf = [buf]
        if len(buf) < _MIN_CHARS:
            return _NOT_LOOPING

        short = _detect_short_period(buf)
        if short is not None:
            return short
        return _detect_long_repeat(buf)


def _detect_short_period(buf: str) -> ThinkingLoopVerdict | None:
    """短周期循环（逐字复读）：末尾周期 p in [1, 12] 的精确循环。"""
    for period in range(1, _SHORT_PERIOD_MAX + 1):
        span = period * _SHORT_PERIOD_MIN_REPEATS
        if len(buf) < span:
            break
        tail = buf[-span:]
        unit = tail[:period]
        if all(tail[i : i + period] == unit for i in range(period, len(tail), period)):
            return ThinkingLoopVerdict(looping=True, sample=unit, repeats=_SHORT_PERIOD_MIN_REPEATS)
    return None


def _count_occurrences(haystack: str, needle: str, cap: int) -> int:
    """数 needle 在 haystack 中的出现次数，**允许重叠**，达到 cap 即停。

    【移植偏离 2】不用 `str.count`：它是非重叠计数，对 "aaa" 找 "aa" 只算 1 次，
    而原 TS 实现（`indexOf(win, idx + 1)`）算 2 次。虽然 50 字符窗口重叠出现极罕见，
    但用 count 会让行为与参考实现分叉，日后对不上账。提前 break 保证代价不高于原实现。
    """
    count = 0
    idx = haystack.find(needle)
    while idx != -1:
        count += 1
        if count >= cap:
            break
        idx = haystack.find(needle, idx + 1)
    return count


def _detect_long_repeat(buf: str) -> ThinkingLoopVerdict:
    """长重复（段落级）：末尾连续 N 个窗口各自在更早内容中重复。

    从尾向前取窗口，步长 WINDOW/2（重叠采样，避免窗口对齐恰好错过重复单元）。
    """
    consecutive = 0
    sample: str | None = None
    max_repeats = 0
    step = _WINDOW // 2
    end = len(buf)
    floor = _MIN_CHARS // 2
    while end - _WINDOW >= floor and consecutive < _CONSECUTIVE_TAIL_HITS:
        win = buf[end - _WINDOW : end]
        earlier = buf[: end - _WINDOW]
        count = _count_occurrences(earlier, win, _REPEAT_THRESHOLD)
        max_repeats = max(max_repeats, count)
        if count >= _REPEAT_THRESHOLD:
            consecutive += 1
            if sample is None:
                sample = win
        else:
            # 末尾窗口不再重复 → 循环已停，不算进行中
            break
        end -= step
    if consecutive >= _CONSECUTIVE_TAIL_HITS:
        return ThinkingLoopVerdict(looping=True, sample=sample, repeats=max_repeats)
    return _NOT_LOOPING


#: 检测到 thinking 死循环后注入的诱导跳出提示。
#:
#: 措辞要点：不指责模型、不解释机制，直接给下一步动作。要求它「基于已有分析给出最优答案」
#: 而不是「重新思考」——后者会让它再进一轮同样的循环。
THINKING_LOOP_NUDGE = (
    "<system>\n"
    "注意：你的思考过程出现了周期性重复（陷入循环）。"
    "请停止继续推理，基于已有的分析直接给出当前最优的最终答案。"
    "如果信息确实不足以得出结论，就直接说明缺什么，不要反复推演。\n"
    "</system>"
)


# ---------------------------------------------------------------------------
# 二、截断自动续写的循环守卫
# ---------------------------------------------------------------------------

#: 续写被拦下的原因。文案层据此选提示语，测试据此断言具体命中了哪条守卫。
ContinuationStopReason = Literal[
    "no_progress",  # 新增正文为空：零进展
    "identical_to_previous",  # 本轮新增与上轮完全相同：模型卡死
    "restarted_from_beginning",  # 开头与首轮相同：丢了上下文从头重写
    "repeating_tail",  # 尾部周期性重复：复读机
    "stalled",  # 连续多轮产出极少：龟速循环
    "max_continues",  # 达到次数上限
]

#: 「从头重来」判定所用的开头片段长度上限。
_HEAD_LEN = 80
#: 「从头重来」判定的最小开头长度。首轮正文比这更短时**跳过该判定**——
#: 十来个字的开头不足以作为特征，「好的，我来继续」这类通用开场会让正常续写被误判。
_MIN_HEAD_FOR_RESTART = 20
#: 龟速判定：单轮新增少于该字符数即计入 stalled。正常续写一轮至少写完一句话。
_STALL_CHARS = 20
#: 龟速判定：连续多少轮都低产才停。给 3 轮容错，避免偶发一次短输出就中断。
_STALL_STREAK = 3
#: 周期检测的最大周期长度。超过这个长度的重复段已属罕见，且成本随之上升。
_MAX_PERIOD = 500


@dataclass(frozen=True)
class ContinuationVerdict:
    """守卫判定结果。safe=True 表示可以继续续写。"""

    safe: bool
    reason: ContinuationStopReason | None = None
    #: 诊断细节（周期长度、已续写轮数等），进提示文案帮用户判断。
    detail: int | None = None


@dataclass
class ContinuationState:
    """跨轮次累积的守卫状态。由会话持有，每次续写后更新。

    初始状态不设 `first_head`，因此「从头重来」判据自动跳过——首次截断时手里只有首轮
    正文本身，拿它跟自己比必然相等，会把每次首轮都误判成从头重来。`first_head` 由首次
    `advance_continuation` 自动补上，之后该判据才开始生效。
    """

    #: 已自动续写的次数。
    count: int = 0
    #: 上一轮续写产出的正文（用于完全相等判定）。
    last_chunk: str | None = None
    #: 首轮（被截断那次）正文的开头片段（用于「从头重来」判定）。
    first_head: str | None = None
    #: 连续「产出极少」的轮数。
    stalled_streak: int = 0

    def reset(self) -> None:
        """新一轮用户消息开始时清空。"""
        self.count = 0
        self.last_chunk = None
        self.first_head = None
        self.stalled_streak = 0


def _head_of(text: str) -> str:
    return text.lstrip()[:_HEAD_LEN]


def initial_continuation_state(first_text: str) -> ContinuationState:
    """用已知的首轮正文构造状态。

    与直接 `ContinuationState()` 的区别是预置了 `first_head`，因此「从头重来」判据
    立即生效。首次截断时用它（此时手里已有首轮正文），跨轮推进用 `advance_continuation`。
    """
    return ContinuationState(first_head=_head_of(first_text))


def _min_repeats_for(period: int) -> int:
    """某个周期长度需要重复几次才判定为异常。**周期越短要求越多**。

    分级的目的是把「正常排版重复」与「模型卡住」分开，而不是用统一阈值一刀切：

    - 短模式在正常文本里极常见（省略号、`---` 分隔线、缩进、`===` 边框），要求 20 次才算异常
    - 20 字符以上的片段精确重复 3 次，正常写作几乎不会发生（代码样板也会有变量名差异）
    """
    if period <= 4:
        return 20
    if period < 20:
        return 6
    return 3


def find_repeating_tail(text: str, max_period: int = _MAX_PERIOD) -> int | None:
    """找出字符串尾部的周期性重复，返回周期长度；无重复返回 None。

    从最短周期试起，检查「末尾 p 字符」是否与它前面的若干个 p 字符块**逐块完全相等**。
    只做字符串切片的相等比较，不引入任何相似度概念——要么完全是周期，要么不是。

    这是刻意的选择：相似度阈值方案必然误伤长输出（代码样板、列表项、表格行、
    反复出现的术语都会让相似度虚高），而误伤的代价是正常任务被中断。
    """
    n = len(text)
    limit = min(max_period, n // 3)
    for period in range(1, limit + 1):
        need = _min_repeats_for(period)
        if n < period * need:
            continue
        unit = text[n - period :]
        if all(text[n - k * period : n - (k - 1) * period] == unit for k in range(2, need + 1)):
            return period
    return None


def check_continuation_safety(
    chunk: str,
    state: ContinuationState,
    max_continues: int,
) -> ContinuationVerdict:
    """判定本轮续写产出是否安全、可以继续。

    检查顺序即优先级：三条确定性判据在前（零进展 / 完全相等 / 从头重来），
    两条文本病态判据在后（复读 / 龟速），次数上限兜底。先判确定性的，
    让停止理由尽可能精确——同一个病态可能同时命中多条，报最根本的那条更有用。

    设计原则：**能用确定性判据的地方绝不用阈值**。主判据是「长度为零」「字符串完全相等」
    这类二值判断，阈值只用在确定性判据覆盖不到的文本病态上，且每个阈值都能说出理据。

    :param chunk: 本轮续写产出的正文（不含思考）。
    :param state: 跨轮状态，**本函数不修改它**（纯函数）；更新交 `advance_continuation`。
    :param max_continues: 次数上限；0 表示关闭自动续写。
    """
    if max_continues <= 0:
        return ContinuationVerdict(safe=False, reason="max_continues", detail=0)

    trimmed = chunk.strip()
    if not trimmed:
        return ContinuationVerdict(safe=False, reason="no_progress")

    if state.last_chunk is not None and chunk == state.last_chunk:
        return ContinuationVerdict(safe=False, reason="identical_to_previous")

    # 「从头重来」：用 first_head 的实际长度去截本轮开头再比对。
    # 不能固定截 _HEAD_LEN——首轮正文短于 _HEAD_LEN 时，本轮的前 _HEAD_LEN 会带上后续内容，
    # 两者永远不等，判定静默失效（这个缺陷在参考实现里由测试抓出来过）。
    head = state.first_head
    if head is not None and len(head) >= _MIN_HEAD_FOR_RESTART:
        if chunk.lstrip()[: len(head)] == head:
            return ContinuationVerdict(safe=False, reason="restarted_from_beginning")

    period = find_repeating_tail(chunk)
    if period is not None:
        return ContinuationVerdict(safe=False, reason="repeating_tail", detail=period)

    next_streak = state.stalled_streak + 1 if len(trimmed) < _STALL_CHARS else 0
    if next_streak >= _STALL_STREAK:
        return ContinuationVerdict(safe=False, reason="stalled", detail=next_streak)

    if state.count + 1 >= max_continues:
        return ContinuationVerdict(safe=False, reason="max_continues", detail=state.count + 1)

    return ContinuationVerdict(safe=True)


def advance_continuation(chunk: str, state: ContinuationState) -> None:
    """记入一轮续写产出，**原地更新** state。

    首次调用时用本轮产出的开头补上 `first_head`——从下一轮起「从头重来」判据才有比较基准。

    与参考实现的差异：那边返回新对象（TS 侧 state 由 loop 以值传递持有），
    这里原地改（AIASys 侧 state 挂在会话实例上，返回新对象反而要调用方记着回写，易漏）。
    """
    trimmed = chunk.strip()
    state.count += 1
    state.last_chunk = chunk
    if state.first_head is None:
        state.first_head = _head_of(chunk)
    state.stalled_streak = state.stalled_streak + 1 if len(trimmed) < _STALL_CHARS else 0


#: 各停止原因对用户的说明。续写被拦下时要告诉用户「为什么停」，
#: 否则表现为「回答莫名截断」，比继续复读更难排查。
CONTINUATION_STOP_MESSAGES: dict[ContinuationStopReason, str] = {
    "no_progress": "输出被截断后续写无新内容（零进展），已停止续写。",
    "identical_to_previous": "续写内容与上一轮完全相同（模型卡死），已停止续写。",
    "restarted_from_beginning": "续写内容从头重复了开头（模型丢失上下文），已停止续写。",
    "repeating_tail": "续写内容尾部出现周期性重复（复读），已停止续写。",
    "stalled": "连续多轮续写产出极少（龟速循环），已停止续写。",
    "max_continues": "输出被截断，续写次数已达上限。",
}


def describe_continuation_stop(verdict: ContinuationVerdict) -> str:
    """把守卫判定渲染成给用户看的 system 提示。"""
    reason = verdict.reason or "max_continues"
    text = CONTINUATION_STOP_MESSAGES.get(reason, CONTINUATION_STOP_MESSAGES["max_continues"])
    if verdict.detail is not None and reason in ("repeating_tail", "stalled", "max_continues"):
        text = f"{text}（{reason}={verdict.detail}）"
    return f"<system>\n{text}\n</system>"
