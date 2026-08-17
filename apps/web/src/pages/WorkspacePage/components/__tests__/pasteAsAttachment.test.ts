import { describe, expect, it } from "vitest";

import {
  buildPastedTextFilename,
  PASTE_AS_ATTACHMENT_MIN_CHARS,
  PASTE_AS_ATTACHMENT_MIN_LINES,
  shouldPasteAsAttachment,
} from "../pasteAsAttachment";

describe("shouldPasteAsAttachment", () => {
  it("短文本不转附件", () => {
    expect(shouldPasteAsAttachment("你好，帮我看一下这个问题")).toBe(false);
  });

  it("达到字符阈值转附件", () => {
    expect(shouldPasteAsAttachment("x".repeat(PASTE_AS_ATTACHMENT_MIN_CHARS))).toBe(true);
    expect(shouldPasteAsAttachment("x".repeat(PASTE_AS_ATTACHMENT_MIN_CHARS - 1))).toBe(false);
  });

  it("达到行数阈值转附件", () => {
    const lines = Array.from({ length: PASTE_AS_ATTACHMENT_MIN_LINES }, (_, i) => `line${i}`);
    expect(shouldPasteAsAttachment(lines.join("\n"))).toBe(true);
    expect(shouldPasteAsAttachment(lines.slice(0, PASTE_AS_ATTACHMENT_MIN_LINES - 1).join("\n"))).toBe(false);
  });

  it("空文本不转附件", () => {
    expect(shouldPasteAsAttachment("")).toBe(false);
  });

  it("探针：阈值若被调小，短 prompt 会被误转（看守不是恒绿）", () => {
    // 模拟缺陷：阈值变成 opencode 的 150 字符
    const brokenMinChars = 150;
    const prompt = "x".repeat(200);
    const brokenDecision =
      prompt.length >= brokenMinChars || prompt.split("\n").length >= 3;
    // 缺陷版本会把 200 字符的单行 prompt 转附件，正确版本不会
    expect(brokenDecision).toBe(true);
    expect(shouldPasteAsAttachment(prompt)).toBe(false);
  });
});

describe("buildPastedTextFilename", () => {
  it("文件名带时间戳且为 txt", () => {
    const name = buildPastedTextFilename(new Date(2026, 7, 15, 19, 25, 30));
    expect(name).toBe("粘贴文本-20260815-192530.txt");
  });
});
