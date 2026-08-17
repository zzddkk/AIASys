"""强制思考模型（force-thinking）能力归一化的测试。

依据：step-code（阶跃官方）2026-08-02 实测——Step 的 effort 取值域为
low/medium/high，无 off/none 档，非法值静默忽略，不传则服务端默认深度思考。
即 step 系列模型的思考无法关闭，必须声明 always_thinking，
否则 UI 显示假「关闭」开关。
"""

from __future__ import annotations

from app.models.llm_provider import (
    is_force_thinking_model,
    normalize_force_thinking_capabilities,
)


class TestIsForceThinkingModel:
    def test_stepfun_endpoint(self) -> None:
        assert is_force_thinking_model("https://api.stepfun.com/v1", "any-model") is True

    def test_step_model_name(self) -> None:
        assert is_force_thinking_model(None, "step-3.7-flash") is True
        assert is_force_thinking_model("https://api.example.com/v1", "Step-3.5") is True

    def test_non_step(self) -> None:
        assert is_force_thinking_model("https://api.openai.com/v1", "gpt-5") is False
        assert is_force_thinking_model(None, None) is False
        assert is_force_thinking_model("", "") is False


class TestNormalizeForceThinkingCapabilities:
    def test_step_model_with_thinking_gets_always_thinking(self) -> None:
        caps = normalize_force_thinking_capabilities(
            ["thinking", "image_in"], "https://api.stepfun.com/v1", "step-3.7-flash"
        )
        assert "always_thinking" in caps
        assert "thinking" in caps
        assert "image_in" in caps

    def test_no_duplicate(self) -> None:
        caps = normalize_force_thinking_capabilities(
            ["thinking", "always_thinking"], None, "step-3.7-flash"
        )
        assert caps.count("always_thinking") == 1

    def test_step_model_without_thinking_untouched(self) -> None:
        # 没声明 thinking 的模型不补——always_thinking 的前提是模型会思考
        caps = normalize_force_thinking_capabilities(["image_in"], None, "step-3.7-flash")
        assert "always_thinking" not in caps

    def test_non_step_untouched(self) -> None:
        caps = normalize_force_thinking_capabilities(
            ["thinking"], "https://api.openai.com/v1", "gpt-5"
        )
        assert caps == ["thinking"]

    def test_bad_input(self) -> None:
        assert normalize_force_thinking_capabilities(None, None, "step-x") == []
        assert normalize_force_thinking_capabilities("junk", None, "gpt") == []
