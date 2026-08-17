import { describe, expect, it } from "vitest";

import { splitMarkdownBlocks } from "../IncrementalMarkdown";

describe("splitMarkdownBlocks", () => {
  it("空行处切块", () => {
    const blocks = splitMarkdownBlocks("第一段\n\n第二段\n\n第三段");
    expect(blocks).toEqual(["第一段", "第二段", "第三段"]);
  });

  it("代码围栏内的空行不切", () => {
    const content = "前文\n\n```python\ndef f():\n    pass\n\n    return 1\n```\n\n后文";
    const blocks = splitMarkdownBlocks(content);
    expect(blocks.length).toBe(3);
    expect(blocks[1]).toContain("pass\n\n    return 1");
  });

  it("$$ 数学块内的空行不切", () => {
    const content = "前文\n\n$$\na = b\n\n+ c\n$$\n\n后文";
    const blocks = splitMarkdownBlocks(content);
    expect(blocks.length).toBe(3);
    expect(blocks[1]).toContain("a = b\n\n+ c");
  });

  it("下一行是有序列表项时不切（防止两个 ol 都从 1 重编号）", () => {
    const content = "1. 第一项\n\n2. 第二项\n\n普通段落";
    const blocks = splitMarkdownBlocks(content);
    // 「1. 第一项」与「2. 第二项」之间不切；与后面普通段落之间切
    expect(blocks.length).toBe(2);
    expect(blocks[0]).toContain("1. 第一项\n\n2. 第二项");
    expect(blocks[1]).toBe("普通段落");
  });

  it("无序列表允许切（两个 ul 视觉等价）", () => {
    const content = "- 第一项\n\n- 第二项";
    const blocks = splitMarkdownBlocks(content);
    expect(blocks.length).toBe(2);
  });

  it("整块内容拼接后与原文信息等价（切块不丢内容）", () => {
    const content = "# 标题\n\n正文 **粗体**\n\n```js\ncode\n```";
    const blocks = splitMarkdownBlocks(content);
    expect(blocks.join("\n\n")).toBe(content);
  });

  it("探针：若围栏检测失效，围栏内空行必然被切（看守不是恒绿）", () => {
    const content = "```\nA\n\nB\n```";
    // 模拟缺陷：忽略围栏状态，直接按空行切
    const brokenBlocks = content.split("\n\n");
    expect(brokenBlocks.length).toBeGreaterThan(1); // 缺陷版本会切
    const fixed = splitMarkdownBlocks(content);
    expect(fixed.length).toBe(1); // 正确版本不切
  });
});
