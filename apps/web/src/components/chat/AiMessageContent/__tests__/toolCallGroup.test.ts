import { describe, expect, it } from "vitest";

import type { ChatSegment } from "@/pages/WorkspacePage/types";

import {
  groupToolCallSegments,
  TOOL_GROUP_MIN_SIZE,
} from "../ToolCallGroupRow";

function call(name: string, id: string, extra?: Partial<ChatSegment>): ChatSegment {
  return {
    type: "tool_call",
    content: "",
    toolName: name,
    toolCallId: id,
    isComplete: true,
    ...extra,
  };
}

describe("groupToolCallSegments", () => {
  it("连续同名调用达到阈值时聚成一组，保持原顺序", () => {
    const segments: ChatSegment[] = [
      { type: "text", content: "先读几个文件" },
      call("read_file", "a"),
      call("read_file", "b"),
      call("read_file", "c"),
      call("read_file", "d"),
      { type: "text", content: "读完了" },
    ];
    const units = groupToolCallSegments(segments);
    expect(units.length).toBe(3);
    expect(units[0]).toMatchObject({ kind: "segment", index: 0 });
    expect(units[1].kind).toBe("group");
    if (units[1].kind === "group") {
      expect(units[1].group.toolName).toBe("read_file");
      expect(units[1].group.calls.map((c) => c.toolCallId)).toEqual(["a", "b", "c", "d"]);
    }
    expect(units[2]).toMatchObject({ kind: "segment", index: 5 });
  });

  it("不足阈值不聚合", () => {
    const segments = [call("read_file", "a"), call("read_file", "b")];
    const units = groupToolCallSegments(segments);
    expect(units.every((u) => u.kind === "segment")).toBe(true);
    expect(units.length).toBe(2);
  });

  it("不同名打断分组：同名两段各自独立判定", () => {
    const segments = [
      call("read_file", "a"),
      call("read_file", "b"),
      call("read_file", "c"),
      call("grep", "x"),
      call("read_file", "d"),
      call("read_file", "e"),
      call("read_file", "f"),
    ];
    const units = groupToolCallSegments(segments);
    expect(units[0].kind).toBe("group");
    expect(units[1]).toMatchObject({ kind: "segment", index: 3 });
    expect(units[2].kind).toBe("group");
  });

  it("tool_output 穿插时不分组", () => {
    const segments: ChatSegment[] = [
      call("read_file", "a"),
      { type: "tool_output", content: "内容", toolName: "read_file", toolCallId: "a" },
      call("read_file", "b"),
      call("read_file", "c"),
    ];
    const units = groupToolCallSegments(segments);
    expect(units.every((u) => u.kind === "segment")).toBe(true);
  });

  it("探针：把阈值改大后分组必须消失（看守不是恒绿）", () => {
    const segments = [
      call("read_file", "a"),
      call("read_file", "b"),
      call("read_file", "c"),
    ];
    // 模拟缺陷：阈值被改成 4，三连 read_file 不再分组
    const broken = groupToolCallSegments(segments).map((u) =>
      u.kind === "group" && u.group.calls.length < TOOL_GROUP_MIN_SIZE + 1
        ? u.group.calls.map((segment, index) => ({ kind: "segment" as const, segment, index }))
        : u,
    );
    const hasGroup = broken.some((u) => !Array.isArray(u) && u.kind === "group");
    expect(hasGroup).toBe(false);
  });
});
