/**
 * config/auth.ts 的行为固化测试（characterization test）
 *
 * 写这组测试的起因不是「补覆盖率」，而是读代码时发现 isSingleUserAuthMode()
 * 在当前类型契约下恒为 true，而它的 4 个调用点全部把它当条件用：
 *
 *   components/auth/RouteGuard.tsx:38        isDirectAccessMode = isSingleUserAuthMode()      恒 true
 *   components/layout/design-sidebar/DesignSidebarFooter.tsx:25
 *                                            showLogout = isAuthenticated && !isSingleUser..  恒 false
 *   components/layout/DesignSidebar.tsx:40   isSingleUserAuthMode() ? "本地工作区" : "未登录"   恒左支
 *   pages/HomePage/Header.tsx:26             isSingleUserMode = isSingleUserAuthMode()        恒 true
 *
 * 推导链：getAuthMode() 的返回类型是 "local" | "none"，且实现对任何非 "none"
 * 的输入都归一化成 "local"（auth.ts:11），因此 authMode === "local" ||
 * authMode === "none" 穷尽了值域。于是「登出按钮」这一分支不可达。
 *
 * 单用户产品下这大概率是设计意图，不是缺陷，所以这里不改实现。但恒真的判断
 * 函数是有代价的：调用点看起来有分支保护，实际没有。所以把它钉成显式断言——
 * 将来若新增第三种 auth 模式（如 "remote"），下面的 it.each 会立刻变红，
 * 迫使改动者回到上面 4 个调用点重新审视，而不是让 UI 静默恢复出一个从未被
 * 测试过的登出流程。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthMode, isSingleUserAuthMode } from "../auth";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("getAuthMode", () => {
  it("显式设为 none 时返回 none", () => {
    vi.stubEnv("VITE_AUTH_MODE", "none");
    expect(getAuthMode()).toBe("none");
  });

  it("未设置时回落到 local", () => {
    vi.stubEnv("VITE_AUTH_MODE", "");
    expect(getAuthMode()).toBe("local");
  });

  // 这条是上面推导链的关键前提：未知值不透传，而是被归一化。
  // 一旦实现改成透传（return mode as AuthMode），此测试变红，
  // 同时意味着 isSingleUserAuthMode 不再恒真，需要重审 4 个调用点。
  it.each(["remote", "oauth", "LOCAL", "None", "0"])(
    "把未知值 %s 归一化为 local 而非透传",
    (mode) => {
      vi.stubEnv("VITE_AUTH_MODE", mode);
      expect(getAuthMode()).toBe("local");
    },
  );
});

describe("isSingleUserAuthMode 的恒真契约", () => {
  it.each(["none", "local", "remote", "", "oauth"])(
    "VITE_AUTH_MODE=%s 时仍返回 true（值域被 getAuthMode 收窄至 local|none）",
    (mode) => {
      vi.stubEnv("VITE_AUTH_MODE", mode);
      expect(isSingleUserAuthMode()).toBe(true);
    },
  );

  it("因此 DesignSidebarFooter 的 showLogout 表达式恒为 false", () => {
    // 复刻调用点原式：isAuthenticated && !isSingleUserAuthMode()
    // 即便已登录，登出按钮仍不渲染。这里断言的是「当前产品行为」，
    // 不是「期望行为」——若产品决定恢复登出入口，应先改 auth 模式设计，
    // 再来改这条测试，而不是反过来。
    const isAuthenticated = true;
    expect(isAuthenticated && !isSingleUserAuthMode()).toBe(false);
  });
});
