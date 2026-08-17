import { describe, expect, it } from "vitest";

import { restoreChatItemsFromHistory } from "../sessionManagementHistory";
import type { SessionHistoryMessage } from "../../types";

describe("restoreChatItemsFromHistory - think 段还原", () => {
  it("assistant 消息携带 reasoning_content 时还原为 think 段", () => {
    const messages: SessionHistoryMessage[] = [
      { role: "user", content: "你是谁？" },
      {
        role: "assistant",
        content: "我是 AIASys 的任务主控。",
        reasoning_content: "用户问“你是谁？”这是一个简单的身份询问。",
        turn_n: 1,
      },
    ];

    const items = restoreChatItemsFromHistory("s1", messages);

    const thinkSegments = items
      .filter((item) => item.type === "message")
      .flatMap((item) => (item.type === "message" ? item.segments ?? [] : []))
      .filter((seg) => seg.type === "think");

    expect(thinkSegments.length).toBe(1);
    expect(thinkSegments[0].content).toContain("简单的身份询问");
    expect(thinkSegments[0].isComplete).toBe(true);
  });

  it("content 块数组里的 think 块也还原为 think 段", () => {
    const messages: SessionHistoryMessage[] = [
      {
        role: "assistant",
        content: [
          { type: "think", think: "结构化思考内容" },
          { type: "text", text: "正式回答" },
        ],
      },
    ];

    const items = restoreChatItemsFromHistory("s1", messages);
    const segments = items
      .filter((item) => item.type === "message")
      .flatMap((item) => (item.type === "message" ? item.segments ?? [] : []));

    expect(segments.some((s) => s.type === "think" && s.content.includes("结构化思考内容"))).toBe(true);
    expect(segments.some((s) => s.type === "text" && s.content.includes("正式回答"))).toBe(true);
  });

  it("reasoning_content 为空字符串时不产生 think 段", () => {
    const messages: SessionHistoryMessage[] = [
      { role: "assistant", content: "回答", reasoning_content: "" },
    ];

    const items = restoreChatItemsFromHistory("s1", messages);
    const thinkSegments = items
      .filter((item) => item.type === "message")
      .flatMap((item) => (item.type === "message" ? item.segments ?? [] : []))
      .filter((seg) => seg.type === "think");

    expect(thinkSegments.length).toBe(0);
  });

  it("compaction_summary 消息的 compaction_stats 透传到段上", () => {
    const messages: SessionHistoryMessage[] = [
      {
        role: "user",
        origin: "compaction_summary",
        content: "Previous context has been compacted...",
        compaction_stats: {
          tokens_before: 45000,
          tokens_after: 12000,
          saved_tokens: 33000,
          compacted_count: 18,
        },
      },
    ];

    const items = restoreChatItemsFromHistory("s1", messages);
    const seg = items
      .filter((item) => item.type === "message")
      .flatMap((item) => (item.type === "message" ? item.segments ?? [] : []))
      .find((s) => s.type === "compaction_summary");

    expect(seg).toBeDefined();
    expect(seg?.compactionStats?.tokens_before).toBe(45000);
    expect(seg?.compactionStats?.saved_tokens).toBe(33000);
  });
});
