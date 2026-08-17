"""从正文流中剥离 `<think>` 类推理标签，归入 reasoning 通道。

本模块为纯算法（无 IO、无框架依赖），便于穷举单测。

## 为什么需要它

部分开源模型（GLM、DeepSeek-R1、Qwen3 等在 vLLM / SGLang 部署下）不把推理放到
独立的 `reasoning_content` 字段，而是直接写在 `content` 里用 `<think>...</think>`
包裹。此时 `openai_client` 按 `KNOWN_REASONING_KEYS` 扫字段一无所获，标签连内容
一起作为正文渲染给用户——即「thinking 内容泄露正文」。

根因分析见 `AIASys-product-design/参考资料/thinking内容泄露正文的根因分析.md`。
该文档的关键结论：**修复必须在后端做，不能在前端渲染层用正则清理**。前端清理有
两种固有失效方式，且都已在本项目发生过：多清（曾把合法 `<code>` 内容一起吃掉）、
漏清（清理点写在思考块组件里，而泄露路径走的是 text 段，根本够不着）。

## 为什么是状态机而不是正则

流式响应下标签会被任意切开。`<think>` 可能拆成 `<thi` + `nk>` 落在两个 chunk 里，
正则对单个 chunk 匹配不到，跨 chunk 又无法回溯已发出的内容。因此必须维护一个
未定长尾部缓冲：只有确认剩余文本**不可能**构成标签前缀时，才把它作为正文放行。

## 安全取舍：只对显式声明的 provider 生效

`tag` 由 `ProviderCapabilities.reasoning_in_content_tag` 显式配置，默认 None 即
完全不启用。不做全局猜测，因为用户正文里可能出现合法的 `<think>` 字样（讨论本
功能时就会），无条件剥离会吃掉用户内容。这一条是 step-code 收紧 ANTML 标签正则
时留下的教训：**裸词匹配会被项目自身内容误触发**。
"""

from __future__ import annotations

__all__ = ["ReasoningTagSplitter"]


class ReasoningTagSplitter:
    """把 `<tag>…</tag>` 包裹的推理从正文流中切出来。

    用法：每个流**新建一个实例**（状态跨 chunk 累积，复用会串流），逐块 `feed()`，
    流末调用一次 `flush()` 收尾。

        splitter = ReasoningTagSplitter("think")
        for chunk_text in stream:
            content, reasoning = splitter.feed(chunk_text)
        content, reasoning = splitter.flush()
    """

    def __init__(self, tag: str) -> None:
        name = (tag or "").strip().strip("<>/")
        if not name:
            raise ValueError("tag 不能为空")
        self._open = f"<{name}>"
        self._close = f"</{name}>"
        #: 尚不能判定归属的尾部残留（可能是被切开的标签前缀）
        self._buf = ""
        #: True 表示当前位于标签内部，增量归 reasoning
        self._inside = False

    @property
    def inside(self) -> bool:
        """当前是否在标签内部。仅用于测试与诊断。"""
        return self._inside

    def feed(self, text: str) -> tuple[str, str]:
        """吃进一个增量，返回 `(正文增量, reasoning 增量)`。

        两者都可能为空字符串——文本全在标签内时正文为空，反之亦然；
        若整段都是待判定的标签前缀，则两者同时为空（内容留在缓冲里）。
        """
        if not text:
            return "", ""
        self._buf += text
        return self._drain()

    def flush(self) -> tuple[str, str]:
        """流结束时清空缓冲，返回 `(正文增量, reasoning 增量)`。

        残留的归属按当前状态决定：`inside` 时归 reasoning（模型被 max_tokens
        截断、没吐出闭标签），`outside` 时归正文（尾部那截 `<` 终究没构成标签，
        是普通文本，必须还给用户而不能吞掉）。
        """
        tail, self._buf = self._buf, ""
        if not tail:
            return "", ""
        return ("", tail) if self._inside else (tail, "")

    # -- 内部实现 -------------------------------------------------------

    def _drain(self) -> tuple[str, str]:
        """反复消费缓冲，直到只剩「可能是标签前缀」的尾部。"""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []

        while True:
            marker = self._close if self._inside else self._open
            sink = reasoning_parts if self._inside else content_parts

            idx = self._buf.find(marker)
            if idx >= 0:
                # 找到完整标签：标签前的内容归当前状态，然后翻转状态
                if idx:
                    sink.append(self._buf[:idx])
                self._buf = self._buf[idx + len(marker) :]
                self._inside = not self._inside
                continue

            # 没有完整标签。保留可能构成标签前缀的尾部，其余放行。
            keep = _prefix_overlap(self._buf, marker)
            if keep:
                sink.append(self._buf[:-keep])
                self._buf = self._buf[-keep:]
            else:
                sink.append(self._buf)
                self._buf = ""
            break

        return "".join(content_parts), "".join(reasoning_parts)


def _prefix_overlap(text: str, marker: str) -> int:
    """返回 `text` 尾部与 `marker` 头部的最长重叠长度（真前缀，不含完整匹配）。

    例：`_prefix_overlap("abc<thi", "<think>")` == 4，表示尾部 4 个字符
    `<thi` 可能是被切开的开标签，必须留在缓冲里等下一个 chunk。

    从长到短试，命中即返回，保证「最长」——取短的会把本属于标签的字符当正文放行。
    """
    limit = min(len(text), len(marker) - 1)
    for size in range(limit, 0, -1):
        if text.endswith(marker[:size]):
            return size
    return 0
