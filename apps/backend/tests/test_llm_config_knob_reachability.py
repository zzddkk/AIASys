"""LLM 配置开关的「可达性」测试。

这个文件守的不是某个算法对不对，而是一个更隐蔽的失效形态：**运行时代码读取了一个
配置开关，但任何用户都没有办法把它设上**。

具体事故（2026-08-10 实测）：`create_llm_client` 通过 `_get_provider_attr(provider,
"reasoning_in_content_tag")` 读取「正文内推理标签」开关，用来决定是否剥离
`<think>…</think>`。但那一轮只加了读取侧与算法（ReasoningTagSplitter，31 条单测全绿），
没有把字段加到任何配置模型上。`_get_provider_attr` 对 Pydantic 模型走
`getattr(obj, key, None)`，字段不存在就返回 None，于是：

- 单元测试全绿（它们直接构造 ReasoningTagSplitter，绕过配置层）
- 功能在生产里永久关闭，thinking 泄露正文的修复等于没上线
- 没有任何报错，日志里也不会留痕

这类缺陷靠「给算法补测试」是发现不了的——算法本来就是对的。必须单独验证
「配置能不能到达运行时」这条链路。

因此本文件分三层：
1. 端到端串通：用户配置 → to_sdk_config() → 运行时模型 → client 实例属性
2. 可泛化守卫：凡是被 `_get_provider_attr` 读取的键，都必须是某个运行时模型的字段
3. 转发守卫：凡是用户配置模型上的 reasoning* 开关，to_sdk_config 都必须转发

第 2、3 层是为了让下一个加开关的人不必记住这件事——漏了会直接红。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.models.llm_provider import LLMModelConfig, LLMProviderConfig
from app.services.agent.models.llm_config import LlmModelConfig, LlmProviderConfig
from app.services.agent.runtime_backends.aiasys.llm_clients import (
    _get_provider_attr,
    create_llm_client,
)

_TAG_FIELD = "reasoning_in_content_tag"


def _user_provider(**overrides: Any) -> LLMProviderConfig:
    """构造一份用户侧 provider 配置（即 config.json 里的形态）。"""
    payload: dict[str, Any] = {
        "id": "selfhosted-vllm",
        "name": "自建 vLLM",
        "type": "openai_chat_completions",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": SecretStr("sk-local"),
    }
    payload.update(overrides)
    return LLMProviderConfig(**payload)


def _user_model(**overrides: Any) -> LLMModelConfig:
    payload: dict[str, Any] = {
        "id": "r1",
        "name": "DeepSeek-R1",
        "provider": "selfhosted-vllm",
        "model": "deepseek-r1",
        "max_context_size": 65536,
    }
    payload.update(overrides)
    return LLMModelConfig(**payload)


# ---------------------------------------------------------------------------
# 一、端到端串通：用户配置真的能开到 client 上
# ---------------------------------------------------------------------------


def test_provider_level_tag_reaches_client() -> None:
    """provider 级配置能一路走到 OpenAIChatClient 实例上。

    这条是对上述事故的直接回归：修复前它会失败在最后一个断言（None != "think"），
    因为字段在两层配置模型上都不存在，值在 to_sdk_config 或 Pydantic 解析时就丢了。
    """
    sdk_config = _user_provider(reasoning_in_content_tag="think").to_sdk_config()
    assert sdk_config[_TAG_FIELD] == "think", "to_sdk_config 未转发该开关"

    runtime = LlmProviderConfig(**sdk_config)
    assert runtime.reasoning_in_content_tag == "think", "运行时模型未保留该开关"

    client = create_llm_client(runtime, "deepseek-r1")
    assert getattr(client, "_reasoning_in_content_tag", None) == "think"


def test_model_level_tag_overrides_provider_level() -> None:
    """model 级优先于 provider 级。

    这不是为了对称好看：同一个自建服务上常同时挂着普通模型与 R1 系，只有按模型
    单独开才能既剥离 R1 的推理，又不动普通模型输出里合法的 <think> 字样。
    """
    provider = LlmProviderConfig(**_user_provider(reasoning_in_content_tag="think").to_sdk_config())
    model_cfg = LlmModelConfig(**_user_model(reasoning_in_content_tag="reasoning").to_sdk_config())

    client = create_llm_client(provider, "deepseek-r1", model_config=model_cfg)
    assert getattr(client, "_reasoning_in_content_tag", None) == "reasoning"


def test_model_level_tag_alone_is_enough() -> None:
    """provider 不配、只在 model 上配，也要生效（否则等于强制全 provider 开）。"""
    provider = LlmProviderConfig(**_user_provider().to_sdk_config())
    model_cfg = LlmModelConfig(**_user_model(reasoning_in_content_tag="think").to_sdk_config())

    client = create_llm_client(provider, "deepseek-r1", model_config=model_cfg)
    assert getattr(client, "_reasoning_in_content_tag", None) == "think"


def test_default_is_disabled() -> None:
    """不配就是不剥离。

    默认必须关：用户正文里完全可能出现合法的 <think> 字样（讨论这个功能本身时就会），
    无条件剥离会吃掉用户内容。这条断言锁的是「零行为变更」这个前提。
    """
    provider = LlmProviderConfig(**_user_provider().to_sdk_config())
    sdk_config = _user_provider().to_sdk_config()

    assert _TAG_FIELD not in sdk_config, "未配置时不应出现该键"
    assert provider.reasoning_in_content_tag is None

    client = create_llm_client(provider, "qwen3")
    assert getattr(client, "_reasoning_in_content_tag", None) is None


def test_runtime_model_keeps_unknown_key_only_if_declared() -> None:
    """直接钉住事故机制本身：字段没声明时，dict 里的值会被 Pydantic 静默丢弃。

    这条测试的价值在于说明「为什么必须显式加字段」——它对当前实现是正向断言，
    但如果有人日后把字段删掉，它会指着真正的原因失败，而不是留下一个
    「client 上是 None」的表象。
    """
    assert _TAG_FIELD in LlmProviderConfig.model_fields
    assert _TAG_FIELD in LlmModelConfig.model_fields

    # 反面对照：未声明的键确实会被丢掉，这正是原缺陷的机制
    parsed = LlmProviderConfig(**{"base_url": "http://127.0.0.1:8000/v1", "totally_unknown": "x"})
    assert not hasattr(parsed, "totally_unknown")
    assert _get_provider_attr(parsed, "totally_unknown") is None


# ---------------------------------------------------------------------------
# 二、可泛化守卫：被读取的键必须是可配置的字段
# ---------------------------------------------------------------------------


def _keys_read_via_get_provider_attr() -> set[str]:
    """用 AST 扫出 `_get_provider_attr(x, "key")` 里的全部字面量 key。

    用 AST 而不是正则：正则会把注释与文档里出现的同名字符串一起算上，也扛不住换行。
    """
    module = inspect.getmodule(create_llm_client)
    assert module is not None and module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "_get_provider_attr" or len(node.args) < 2:
            continue
        key_node = node.args[1]
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            keys.add(key_node.value)
    return keys


def test_ast_extraction_is_actually_working() -> None:
    """先证明检索方法有效，再采信它的结果。

    没有这一条，下面那个参数化测试会在 AST 提取静默失效时收集到 0 个用例——
    pytest 报绿，而实际上一个键都没检查。空结果和「全部合规」在报告里长得一样。
    """
    keys = _keys_read_via_get_provider_attr()
    assert keys, "AST 未提取到任何 key，说明提取逻辑已失效（不是真的没有调用）"
    # 这几个键必然存在于 create_llm_client 中，用作检索式的阳性对照
    for expected in ("protocol", "api_key", "base_url", "reasoning_key", _TAG_FIELD):
        assert expected in keys, f"阳性对照 {expected!r} 未被提取到"


@pytest.mark.parametrize("key", sorted(_keys_read_via_get_provider_attr()))
def test_every_read_key_is_a_declared_field(key: str) -> None:
    """凡是运行时读取的配置键，必须能在某个运行时模型上声明式地配置。

    `_get_provider_attr` 用的是 `getattr(obj, key, None)`，读一个不存在的字段不会
    报错、只会永远拿到 None。所以「读了但没人能设」这件事在运行时是完全静默的，
    只有靠这条测试拦。
    """
    declared = set(LlmProviderConfig.model_fields) | set(LlmModelConfig.model_fields)
    assert key in declared, (
        f"create_llm_client 读取了 {key!r}，但 LlmProviderConfig 与 LlmModelConfig "
        f"都没有这个字段——该开关永远为 None，功能无法启用。"
        f"补字段时别忘了同步用户侧模型与 to_sdk_config 的转发。"
    )


# ---------------------------------------------------------------------------
# 三、转发守卫：用户配置模型上的开关必须被 to_sdk_config 带出去
# ---------------------------------------------------------------------------


def _reasoning_knobs(model_cls: type) -> list[str]:
    return sorted(f for f in model_cls.model_fields if f.startswith("reasoning"))


def test_reasoning_knob_sets_are_not_empty() -> None:
    """同上，先自证检索有效，避免下面两条在字段改名后静默变成空循环。"""
    assert _reasoning_knobs(LLMProviderConfig), "provider 侧未找到任何 reasoning* 开关"
    assert _reasoning_knobs(LLMModelConfig), "model 侧未找到任何 reasoning* 开关"


def test_provider_forwards_every_reasoning_knob() -> None:
    """用户侧 provider 的每个 reasoning* 开关都要出现在 to_sdk_config 结果里。

    原缺陷是两段式的：字段没声明，且序列化没转发。只补前者仍然到不了运行时，
    所以两段都要有守卫。
    """
    for knob in _reasoning_knobs(LLMProviderConfig):
        # reasoning_format 有 Literal 约束，给一个合法值；其余给普通字符串即可
        value = "general" if knob == "reasoning_format" else "probe-value"
        config = _user_provider(**{knob: value}).to_sdk_config()
        assert config.get(knob) == value, f"to_sdk_config 未转发 provider 侧开关 {knob!r}"


def test_model_forwards_every_reasoning_knob() -> None:
    for knob in _reasoning_knobs(LLMModelConfig):
        config = _user_model(**{knob: "probe-value"}).to_sdk_config()
        assert config.get(knob) == "probe-value", f"to_sdk_config 未转发 model 侧开关 {knob!r}"
