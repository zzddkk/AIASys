/**
 * 列表搜索谓词的看守测试（工作区 / 会话）。
 *
 * 看守点：
 * - 多字段匹配（标题 + 描述 / 最后一问）；
 * - 大小写不敏感；
 * - 空查询返回全部；
 * - 缺字段不崩。
 * 每个关键行为都配一条探针式断言（先断言缺陷版本会怎样），保证不是恒绿。
 */

import { describe, expect, it } from "vitest";
import { matchesWorkspace, matchesConversation } from "@/utils/listSearch";

describe("matchesWorkspace", () => {
  it("空查询匹配全部", () => {
    expect(matchesWorkspace({ title: "A" }, "")).toBe(true);
    expect(matchesWorkspace({ title: "A" }, "   ")).toBe(true);
  });

  it("标题命中", () => {
    expect(matchesWorkspace({ title: "回归测试" }, "回归")).toBe(true);
  });

  it("描述命中（标题不命中时）", () => {
    expect(
      matchesWorkspace({ title: "工作区A", description: "浏览器回归用例集" }, "回归"),
    ).toBe(true);
  });

  it("大小写不敏感", () => {
    expect(matchesWorkspace({ title: "AIASys" }, "aiasys")).toBe(true);
  });

  it("标题缺省用「未命名工作区」参与匹配", () => {
    expect(matchesWorkspace({}, "未命名")).toBe(true);
  });

  it("探针：只搜标题（丢掉描述匹配）会漏掉这条", () => {
    const onlyTitle = (w: { title?: string | null; description?: string | null }, q: string) =>
      (w.title || "未命名工作区").toLowerCase().includes(q.trim().toLowerCase());
    expect(onlyTitle({ title: "A", description: "回归" }, "回归")).toBe(false);
    expect(matchesWorkspace({ title: "A", description: "回归" }, "回归")).toBe(true);
  });
});

describe("matchesConversation", () => {
  it("标题命中", () => {
    expect(matchesConversation({ title: "排障会话" }, "排障")).toBe(true);
  });

  it("最后一问预览命中（标题不命中时）", () => {
    expect(
      matchesConversation({ title: "新会话", last_user_preview: "为什么 think 不显示" }, "think"),
    ).toBe(true);
  });

  it("空预览不崩", () => {
    expect(matchesConversation({ title: "新会话" }, "xyz")).toBe(false);
  });

  it("标题缺省用「未命名对话」参与匹配", () => {
    expect(matchesConversation({}, "未命名")).toBe(true);
  });

  it("探针：只搜标题会漏掉靠最后一问命中的会话", () => {
    const onlyTitle = (c: { title?: string | null; last_user_preview?: string | null }, q: string) =>
      (c.title || "未命名对话").toLowerCase().includes(q.trim().toLowerCase());
    expect(onlyTitle({ title: "会话1", last_user_preview: "think" }, "think")).toBe(false);
    expect(
      matchesConversation({ title: "会话1", last_user_preview: "think" }, "think"),
    ).toBe(true);
  });
});
