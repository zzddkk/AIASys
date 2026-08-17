import { describe, expect, it } from "vitest";
import { render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";

/**
 * 测试环境自检（smoke）。
 *
 * 这个文件不测业务逻辑，只回答一个问题：组件测试的地基还在不在。
 *
 * 为什么值得单独存在：2026-08-09 之前 apps/web 有 249 个 React 组件、零组件测试，
 * 装 vitest + @testing-library/react 是从零搭的地基。地基一旦被改坏（jsdom 环境没
 * 生效、JSX 转换插件掉了、cleanup 没挂上），业务测试的报错会五花八门且指向错误的
 * 方向——先怀疑业务代码，最后才发现是环境问题。有这一条，环境坏了会第一个红。
 *
 * 灵感来自 step-code 的 tests/tui/firstFrameSmoke.test.ts：它 spawn 真实子进程只
 * 断言「输出字节数 > 0」，不断言画面内容。那条测试在 2068 个单测全绿的情况下，
 * 独自捕获了一次整屏空白事故。取舍是一样的：断言最干净的故障特征，不追求覆盖细节。
 *
 * ── 这里曾经有第四条测试，已删除，记下原因 ──────────────────────────
 * 原本还有一条「上一个测试的 DOM 已被 cleanup 清掉」，用来守护 vitest.setup.ts 的
 * cleanup。2026-08-09 用探针验证它是否真能失败：注释掉 setup 里的 cleanup() 后，
 * 那条测试依然通过——因为 vitest 开了 globals: true，@testing-library/react 会自己
 * 注册一份自动 cleanup 兜住。反过来用 RTL_SKIP_AUTO_CLEANUP=true 关掉自动清理，
 * setup 里的显式 cleanup 又会兜住。两者互为备份，那条断言只有在两套机制同时失效时
 * 才会红，实际上等于恒绿。
 * 恒绿的测试比没有测试更糟：它让人以为这块有守护。所以删掉，并保留 setup 里的显式
 * cleanup（它在 globals: false 或跳过自动清理时是唯一的清理点，不是冗余）。
 */
describe("测试环境自检", () => {
  it("jsdom 环境可用（有 document 与真实 DOM 操作）", () => {
    expect(typeof document).toBe("object");
    const el = document.createElement("div");
    el.textContent = "探针";
    document.body.appendChild(el);
    expect(document.body.textContent).toContain("探针");
    el.remove();
  });

  it("React 组件能渲染，且能按文本查到（JSX 转换 + testing-library 都在工作）", () => {
    render(<p>渲染探针文本</p>);
    expect(screen.getByText("渲染探针文本")).toBeTruthy();
    // 同时用「渲染结果字符串包含」这种断言方式验证一次，
    // 它是 step-code 里 lastFrame().toContain(...) 的 Web 对应物，
    // 不依赖 @testing-library/jest-dom 的自定义 matcher（本项目有意没装那个包）。
    expect(document.body.textContent).toContain("渲染探针文本");
  });

  it("hook 能被独立渲染与驱动（renderHook 可用）", () => {
    const { result } = renderHook(() => {
      const [n, setN] = useState(0);
      return { n, setN };
    });
    expect(result.current.n).toBe(0);
  });
});
