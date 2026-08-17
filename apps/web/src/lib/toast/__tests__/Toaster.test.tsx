/**
 * 全局 Toaster 渲染的看守测试。
 *
 * 看守点：
 * - 空 store 不渲染；
 * - push 后按 variant 渲染对应内容；
 * - 点击关闭可移除；
 * - error 用 role=alert（屏幕阅读器即时播报），非 error 用 status。
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor, act } from "@testing-library/react";
import { Toaster } from "@/lib/toast/Toaster";
import { toast } from "@/lib/toast/store";

describe("Toaster 渲染", () => {
  beforeEach(() => toast._reset());
  afterEach(() => {
    cleanup();
    toast._reset();
  });

  it("空状态不渲染任何内容", () => {
    const { container } = render(<Toaster />);
    expect(container.textContent).toBe("");
  });

  it("push success 后渲染消息", () => {
    render(<Toaster />);
    act(() => {
      toast.success("保存成功");
    });
    expect(screen.getByText("保存成功")).toBeTruthy();
  });

  it("error 用 role=alert，可被屏幕阅读器即时播报", () => {
    render(<Toaster />);
    act(() => {
      toast.error("保存失败");
    });
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("非 error 用 role=status（不打断用户）", () => {
    render(<Toaster />);
    act(() => {
      toast.info("提示");
    });
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("点击关闭按钮移除该条 toast", async () => {
    render(<Toaster />);
    act(() => {
      toast.success("可关闭");
    });
    const closeButton = screen.getByRole("button", { name: "关闭" });
    await act(async () => {
      fireEvent.click(closeButton);
    });
    await waitFor(() => expect(screen.queryByText("可关闭")).toBeNull());
  });

  it("支持堆叠多条", () => {
    render(<Toaster />);
    act(() => {
      toast.success("一");
      toast.error("二");
    });
    expect(screen.getByText("一")).toBeTruthy();
    expect(screen.getByText("二")).toBeTruthy();
  });
});
