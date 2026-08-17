import { describe, it, expect } from "vitest";

import { modelThinkingMode } from "../../hooks/useModelSelection";
import type { LLMModelConfig } from "@/lib/api/llm";

/**
 * 思考能力三态判定的契约测试。
 *
 * 为什么值得测：always_thinking 模型的思考关不掉（后端
 * _resolve_request_options 强制 thinking_enabled=True，无视前端传的 false）。
 * 如果三态判定写错（把 always 判成 switchable），UI 就会显示一个可点的
 * 「关闭」——用户点关，UI 显示已关，模型实际一直在思考：假控件。
 * 判定逻辑是 InputArea 开关和 ModelSelector 面板两个表面的共同上游。
 */

function model(capabilities?: string[]): LLMModelConfig {
  return {
    id: "m1",
    name: "测试模型",
    provider: "p",
    model: "m",
    max_context_size: 128000,
    is_default: false,
    capabilities: capabilities as LLMModelConfig["capabilities"],
  };
}

describe("modelThinkingMode 三态", () => {
  it("always_thinking → always", () => {
    expect(modelThinkingMode(model(["always_thinking"]))).toBe("always");
  });

  it("always_thinking 与 thinking 并存 → always 优先", () => {
    expect(modelThinkingMode(model(["thinking", "always_thinking"]))).toBe(
      "always",
    );
  });

  it("仅 thinking → switchable", () => {
    expect(modelThinkingMode(model(["thinking"]))).toBe("switchable");
  });

  it("无思考能力 → none", () => {
    expect(modelThinkingMode(model(["image_in"]))).toBe("none");
    expect(modelThinkingMode(model([]))).toBe("none");
    expect(modelThinkingMode(model(undefined))).toBe("none");
  });

  it("模型未解析（system 默认等）→ none（不显示开关）", () => {
    expect(modelThinkingMode(undefined)).toBe("none");
  });
});
