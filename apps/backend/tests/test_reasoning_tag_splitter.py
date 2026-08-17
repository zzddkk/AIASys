"""`ReasoningTagSplitter` 的行为测试。

覆盖重点是**流式切分的保真度**：同一段文本无论被切成几块、在哪切，
切出来的正文与 reasoning 都必须与一次性喂入完全一致。这条不变式用穷举
所有切分点来验证，而不是抽样几个位置——标签被切开正是本模块存在的理由。

其余用例对应根因分析文档第五节点名的四类要求：标签被切成任意两段、
闭标签缺失、正文里出现字面标签字样、无标签的正常流不受影响。
"""

from __future__ import annotations

import pytest

from app.services.agent.runtime_backends.aiasys.llm_clients.reasoning_tag_splitter import (
    ReasoningTagSplitter,
)


def _run(chunks: list[str], tag: str = "think") -> tuple[str, str]:
    """喂完所有 chunk 并 flush，返回累计的 (正文, reasoning)。"""
    splitter = ReasoningTagSplitter(tag)
    content, reasoning = "", ""
    for chunk in chunks:
        c, r = splitter.feed(chunk)
        content += c
        reasoning += r
    c, r = splitter.flush()
    return content + c, reasoning + r


# -- 一、基本切分 ---------------------------------------------------------


def test_单块内的完整标签被正确分离():
    content, reasoning = _run(["答案前<think>推理内容</think>答案后"])
    assert content == "答案前答案后"
    assert reasoning == "推理内容"


def test_无标签的正常流原样透传():
    content, reasoning = _run(["这是", "一段普通", "正文"])
    assert content == "这是一段普通正文"
    assert reasoning == ""


def test_整段都是推理时正文为空():
    content, reasoning = _run(["<think>全是推理</think>"])
    assert content == ""
    assert reasoning == "全是推理"


def test_多段标签交替():
    content, reasoning = _run(["a<think>r1</think>b<think>r2</think>c"])
    assert content == "abc"
    assert reasoning == "r1r2"


def test_空标签体():
    content, reasoning = _run(["a<think></think>b"])
    assert content == "ab"
    assert reasoning == ""


# -- 二、流式切分保真度（本模块的核心不变式）-----------------------------


@pytest.mark.parametrize(
    "text,want_content,want_reasoning",
    [
        ("答案前<think>推理</think>答案后", "答案前答案后", "推理"),
        ("<think>推理</think>只有答案", "只有答案", "推理"),
        ("a<think>r1</think>b<think>r2</think>c", "abc", "r1r2"),
        ("没有任何标签的纯正文", "没有任何标签的纯正文", ""),
    ],
)
def test_穷举所有切分点结果都一致(text: str, want_content: str, want_reasoning: str):
    """在每个位置切成两段，结果必须与一次性喂入一致。"""
    for i in range(len(text) + 1):
        content, reasoning = _run([text[:i], text[i:]])
        assert content == want_content, f"切分点 {i} 正文错: {content!r}"
        assert reasoning == want_reasoning, f"切分点 {i} reasoning 错: {reasoning!r}"


@pytest.mark.parametrize(
    "text,want_content,want_reasoning",
    [
        ("答案前<think>推理</think>答案后", "答案前答案后", "推理"),
        ("a<think>r</think>b", "ab", "r"),
    ],
)
def test_逐字符喂入结果一致(text: str, want_content: str, want_reasoning: str):
    """最极端的流式形态：每个 chunk 只有一个字符。"""
    content, reasoning = _run(list(text))
    assert content == want_content
    assert reasoning == want_reasoning


def test_标签被切成三段():
    content, reasoning = _run(["答案<thi", "nk>推", "理</thi", "nk>后"])
    assert content == "答案后"
    assert reasoning == "推理"


# -- 三、异常与边界 -------------------------------------------------------


def test_闭标签缺失时残留归reasoning():
    """模型被 max_tokens 截断，没吐出闭标签。"""
    content, reasoning = _run(["答案<think>推理被截断"])
    assert content == "答案"
    assert reasoning == "推理被截断"


def test_尾部的假标签前缀必须还给正文():
    """`<` 结尾终究没构成标签，是普通文本，不能被吞掉。"""
    content, reasoning = _run(["价格 a <", " b"])
    assert content == "价格 a < b"
    assert reasoning == ""


def test_流末尾停在假前缀上也不吞内容():
    content, reasoning = _run(["公式 x <thi"])
    assert content == "公式 x <thi"
    assert reasoning == ""


def test_流末尾停在闭标签前缀上时残片归reasoning而非正文():
    """标签内被截断，且残留的是 `</think>` 的部分前缀。

    这条补的是 flush() 里 `inside` 分支的缺口。上面那条
    test_闭标签缺失时残留归reasoning 看起来覆盖了截断场景，实际没有走到这个分支：
    _drain 在 inside 状态下会把内容直接放行进 reasoning 并清空缓冲，等到 flush 时
    tail 已经是空的，走的是 `if not tail` 那条早退。

    只有当缓冲里剩下的恰好是闭标签的真前缀（`</thi` 之类）时，flush 才真的需要
    判断归属。反向探针实测：把 flush 的 `("", tail) if self._inside` 改成无条件
    `(tail, "")`，29 条测试全绿放行——也就是说「截断时残片泄露进正文」当时没有任何
    测试拦得住，而这正是本模块要解决的那类问题。
    """
    content, reasoning = _run(["答案<think>推理</thi"])
    assert content == "答案"
    assert reasoning == "推理</thi"


def test_逐块喂入且末块是闭标签前缀时不泄露到正文():
    """同上，但拆成多块喂，确认跨 chunk 的缓冲残留同样归 reasoning。"""
    content, reasoning = _run(["<think>", "推理", "</thin"])
    assert content == ""
    assert reasoning == "推理</thin"


def test_相似但不匹配的标签不触发():
    content, reasoning = _run(["a<thinking>b</thinking>c"])
    assert content == "a<thinking>b</thinking>c"
    assert reasoning == ""


def test_嵌套不处理只认最外层():
    """根因分析文档第五节第 4 点：inside 期间再遇开标签当普通字符。

    开源模型不产生嵌套 think，为它加复杂度不值得；这里锁定「不处理」这个
    有意决定，防止后人误当 bug 修掉。
    """
    content, reasoning = _run(["a<think>外<think>内</think>b"])
    assert content == "ab"
    assert reasoning == "外<think>内"


def test_空chunk被忽略():
    content, reasoning = _run(["a", "", "<think>", "", "r", "</think>", "", "b"])
    assert content == "ab"
    assert reasoning == "r"


def test_没有任何输入时flush返回空():
    splitter = ReasoningTagSplitter("think")
    assert splitter.flush() == ("", "")


def test_inside状态可观测():
    splitter = ReasoningTagSplitter("think")
    assert splitter.inside is False
    splitter.feed("a<think>")
    assert splitter.inside is True
    splitter.feed("r</think>")
    assert splitter.inside is False


# -- 四、标签名配置 -------------------------------------------------------


def test_自定义标签名():
    content, reasoning = _run(["a<reasoning>r</reasoning>b"], tag="reasoning")
    assert content == "ab"
    assert reasoning == "r"


@pytest.mark.parametrize("raw", ["think", "<think>", "</think>", "  think  "])
def test_标签名的书写形式被归一(raw: str):
    """配置里写成 `think` / `<think>` / `</think>` 都应等价。"""
    content, reasoning = _run(["a<think>r</think>b"], tag=raw)
    assert content == "ab"
    assert reasoning == "r"


@pytest.mark.parametrize("bad", ["", "   ", "<>", "//"])
def test_空标签名被拒绝(bad: str):
    with pytest.raises(ValueError):
        ReasoningTagSplitter(bad)
