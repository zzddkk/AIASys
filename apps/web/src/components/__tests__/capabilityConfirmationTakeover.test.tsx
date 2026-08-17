/**
 * 审批卡 takeover 行为的看守测试。
 *
 * 设计依据：参考资料/前端全域借鉴清单.md P0-4（对齐 codex 的审批交互）。
 * 看守点：
 * 1. Esc 映射为「拒绝」，绝不静默当作允许（dismissal ≠ approval）；
 * 2. 输入框内按 Esc 不触发拒绝（留给输入框自身）；
 * 3. 三档语义明确：拒绝 / 本会话内总是允许(session) / 仅本次允许(once)；
 * 4. 终态卡片不再响应 Esc。
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CapabilityConfirmationCard } from "@/components/CapabilityConfirmationCard";

type Status = "pending" | "approved" | "rejected" | "timeout";

function setup(status: Status = "pending") {
  const onApprove = vi.fn(async () => true);
  const onReject = vi.fn(async () => true);
  render(
    <CapabilityConfirmationCard
      tool_name="Shell"
      arguments={{ command: "rm -rf build" }}
      prompt="需要执行 Shell 命令"
      pattern_key="shell_command"
      status={status}
      onApprove={onApprove}
      onReject={onReject}
    />,
  );
  return { onApprove, onReject };
}

describe("审批卡 takeover", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    cleanup();
  });

  it("Esc 触发拒绝，不触发任何允许", async () => {
    const { onApprove, onReject } = setup("pending");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(onReject).toHaveBeenCalledTimes(1));
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("焦点在反馈输入框时，Esc 不触发拒绝", async () => {
    const { onReject } = setup("pending");
    const input = screen.getByPlaceholderText(/反馈|理由|说明/) as HTMLInputElement;
    input.focus();
    expect(document.activeElement).toBe(input);
    await userEvent.keyboard("{Escape}");
    // 给事件循环一次机会，确认没有迟到的调用
    await new Promise((r) => setTimeout(r, 20));
    expect(onReject).not.toHaveBeenCalled();
  });

  it("终态卡片不响应 Esc", async () => {
    const { onReject } = setup("approved");
    await userEvent.keyboard("{Escape}");
    await new Promise((r) => setTimeout(r, 20));
    expect(onReject).not.toHaveBeenCalled();
  });

  it("三档按钮语义明确：主按钮是「仅本次允许」，不是范围更大的会话档", async () => {
    const { onApprove } = setup("pending");
    await userEvent.click(screen.getByRole("button", { name: "仅本次允许" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith("once"));
  });

  it("「本会话内总是允许」传 session 档", async () => {
    const { onApprove } = setup("pending");
    await userEvent.click(screen.getByRole("button", { name: "本会话内总是允许" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith("session"));
  });

  it("拒绝按钮标注了 Esc 快捷键（可发现性）", () => {
    setup("pending");
    expect(screen.getByRole("button", { name: /拒绝（Esc）/ })).toBeTruthy();
  });

  it("范围档位不含模糊的「允许」——避免用户误选更大范围", () => {
    setup("pending");
    const names = screen
      .getAllByRole("button")
      .map((b) => b.textContent?.trim() ?? "")
      .filter(Boolean);
    expect(names).not.toContain("允许");
    expect(names).not.toContain("本会话允许");
  });
});
