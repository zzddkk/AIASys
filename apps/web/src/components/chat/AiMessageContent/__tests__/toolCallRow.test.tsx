import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ToolCallRow, summarizeToolParams } from "../ToolCallRow";

/**
 * ToolCallRow 的渲染契约测试。
 *
 * 覆盖两件事：
 * 1. summarizeToolParams 的参数摘要提取（优先级、截断、坏输入回退）——
 *    这是行内「对什么对象操作」的唯一信息来源，提取错了行内就只剩工具名；
 * 2. 三态视觉分支（running/ok/error）——error 必须一眼可辨（红色 + 失败标签），
 *    running 必须有进行中信标。这两态原先完全没有，是这次改造新增的契约。
 */

describe("summarizeToolParams", () => {
  it("按优先级提取 path 字段", () => {
    expect(
      summarizeToolParams('{"path":"/a/b.py","command":"ls"}'),
    ).toBe("/a/b.py");
  });

  it("没有 path 时回退到 command", () => {
    expect(summarizeToolParams('{"command":"ls -la"}')).toBe("ls -la");
  });

  it("超过 80 字符截断并加省略号", () => {
    const long = "x".repeat(100);
    const result = summarizeToolParams(`{"path":"${long}"}`);
    expect(result).toHaveLength(81);
    expect(result?.endsWith("…")).toBe(true);
  });

  it("非法 JSON 返回 undefined", () => {
    expect(summarizeToolParams("not json")).toBeUndefined();
  });

  it("空字符串 / undefined 返回 undefined", () => {
    expect(summarizeToolParams("")).toBeUndefined();
    expect(summarizeToolParams(undefined)).toBeUndefined();
  });

  it("对象里没有可摘要字段返回 undefined", () => {
    expect(summarizeToolParams('{"foo":1,"bar":2}')).toBeUndefined();
  });

  it("数字与布尔值字段可摘要", () => {
    expect(summarizeToolParams('{"name":42}')).toBe("42");
  });

  it("空字符串字段跳过，继续找后面的键", () => {
    expect(summarizeToolParams('{"path":"  ","command":"ls"}')).toBe("ls");
  });
});

describe("ToolCallRow 状态渲染", () => {
  it("error 态显示失败标签与红色样式", () => {
    const { container } = render(
      <ToolCallRow toolName="write_file" isError isComplete />,
    );
    expect(screen.queryByText("失败")).not.toBeNull();
    expect(container.querySelector("button")?.className).toContain("border-red-200");
  });

  it("running 态（消息流式中且调用未完成）显示运行中与 spinner", () => {
    const { container } = render(
      <ToolCallRow toolName="bash" isMessageStreaming isComplete={false} />,
    );
    expect(screen.queryByText("运行中")).not.toBeNull();
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  it("ok 态不显示状态标签，有参数摘要时显示摘要", () => {
    render(
      <ToolCallRow
        toolName="read_file"
        toolParams='{"path":"src/main.ts"}'
        isComplete
      />,
    );
    expect(screen.queryByText("src/main.ts")).not.toBeNull();
    expect(screen.queryByText("失败")).toBeNull();
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("无参数摘要时回退到「点击查看详情」", () => {
    render(<ToolCallRow toolName="bash" isComplete />);
    expect(screen.queryByText(/点击查看详情/)).not.toBeNull();
  });

  it("消息已结束但未标记完成的调用不算 running", () => {
    render(<ToolCallRow toolName="bash" isMessageStreaming={false} isComplete={false} />);
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("点击时回传触发元素位置", () => {
    const onClick = vi.fn();
    render(<ToolCallRow toolName="bash" isComplete onClick={onClick} />);
    screen.getByRole("button").click();
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onClick.mock.calls[0][0]).toHaveProperty("width");
  });
});
