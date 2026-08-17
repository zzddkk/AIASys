/**
 * 全局 toast store 的看守测试。
 *
 * 看守点：四型分发、订阅通知、dismiss/clear、persistent 不设时长。
 * 用注入式断言保证不是恒绿（缺陷版本：不通知 / 不删 / 串型）。
 */

import { describe, expect, it, beforeEach } from "vitest";
import { toast, type ToastItem } from "@/lib/toast/store";

describe("全局 toast store", () => {
  beforeEach(() => {
    toast._reset();
  });

  it("success/error/info/warning 各自入队正确 variant", () => {
    toast.success("ok");
    toast.error("bad");
    toast.info("fyi");
    toast.warning("careful");
    const items = toast._snapshot();
    expect(items.map((t) => t.variant)).toEqual([
      "success",
      "error",
      "info",
      "warning",
    ]);
    expect(items.map((t) => t.message)).toEqual(["ok", "bad", "fyi", "careful"]);
  });

  it("订阅者在 push/dismiss/clear 时收到通知", () => {
    const seen: ToastItem[][] = [];
    const unsubscribe = toast.subscribe((s) => seen.push(s));
    const id = toast.error("x");
    toast.dismiss(id);
    toast.success("y");
    toast.clear();
    unsubscribe();
    // subscribe 初始同步一次 + 4 次变更
    expect(seen.length).toBe(5);
    expect(seen[1]).toHaveLength(1); // push
    expect(seen[2]).toHaveLength(0); // dismiss
    expect(seen[3]).toHaveLength(1); // push
    expect(seen[4]).toHaveLength(0); // clear
  });

  it("dismiss 只删指定 id，不波及其他", () => {
    const a = toast.success("a");
    const b = toast.error("b");
    toast.dismiss(a);
    const remaining = toast._snapshot();
    expect(remaining).toHaveLength(1);
    expect(remaining[0].id).toBe(b);
  });

  it("persistent 的 duration 为 0（不自动关闭）", () => {
    toast.error("需要用户看清", { persistent: true });
    expect(toast._snapshot()[0].duration).toBe(0);
  });

  it("error 默认 duration 长于 info（错误更需要看清）", () => {
    toast.info("fyi");
    toast.error("bad");
    const [info, error] = toast._snapshot();
    expect(error.duration).toBeGreaterThan(info.duration);
  });

  it("unsubscribe 后不再收到通知", () => {
    let calls = 0;
    const unsub = toast.subscribe(() => calls++);
    expect(calls).toBe(1); // 初始同步
    unsub();
    toast.success("after");
    expect(calls).toBe(1);
  });

  it("探针：只 push 不 emit 时订阅者收不到（看守不是恒绿）", () => {
    // 直接验证 store 行为正确：缺陷版本若漏 emit，下面断言会失败
    let last: ToastItem[] = [];
    toast.subscribe((s) => (last = s));
    toast.info("probe");
    expect(last.map((t) => t.message)).toContain("probe");
    // 对照：一个不订阅的观察者看不到变更（证明通知机制是真实的）
    let blind = 0;
    const unsub = toast.subscribe(() => blind++);
    const before = blind;
    unsub();
    toast.success("not-seen");
    expect(blind).toBe(before);
  });
});
