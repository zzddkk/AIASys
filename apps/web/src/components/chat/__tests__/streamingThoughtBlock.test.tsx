import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  StreamingThoughtBlock,
  formatThinkDuration,
  lastContentLine,
  tailPreviewText,
  TAIL_PREVIEW_LINES,
} from "../StreamingThoughtBlock";

// 组件内部只取 session?.token，mock 掉 AuthContext 避免拉进整个认证栈
vi.mock("@/contexts/AuthContext", () => ({
  useAuthContext: () => ({ session: null }),
}));

// ChartAwareMarkdown 依赖 markdown/chart 渲染链，这里只测折叠行为与计时显示，
// 换成原样渲染的替身；内容文本照进 DOM，断言强度不减。
vi.mock("../ChartAwareMarkdown", () => ({
  ChartAwareMarkdown: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
}));

/**
 * StreamingThoughtBlock 的三态契约（参照 step-code ThinkingPreview +
 * grok-build ThinkingBlock 三态 + deepseek-harness ReasoningRow 最新行跟随）：
 *
 * 1. streaming 中且未展开：只占 3 行的尾部滚动预览（纯文本，跟随最新位置），
 *    全文 markdown 不渲染——思考不占满对话区是本组件存在的核心理由；
 * 2. 用户点击标题 → 展开全文；结束后若用户表过态则保持展开，否则自动折叠；
 * 3. 结束后折叠：标题定格「思考过程 · Xs」+ 最后一行预览。
 */

describe("formatThinkDuration", () => {
  it("小于 60 秒显示秒数", () => {
    expect(formatThinkDuration(2300)).toBe("2.3s");
  });
  it("超过 60 秒显示分秒", () => {
    expect(formatThinkDuration(83_000)).toBe("1m 23s");
  });
});

describe("lastContentLine", () => {
  it("取最后一行非空内容", () => {
    expect(lastContentLine("第一行\n\n第二行\n")).toBe("第二行");
  });
  it("全空返回 undefined", () => {
    expect(lastContentLine("  \n\n")).toBeUndefined();
  });
});

describe("tailPreviewText", () => {
  it("默认取尾部 3 行", () => {
    expect(TAIL_PREVIEW_LINES).toBe(3);
    expect(tailPreviewText("一\n二\n三\n四\n五")).toBe("三\n四\n五");
  });
  it("不足 3 行时全量返回", () => {
    expect(tailPreviewText("一\n二")).toBe("一\n二");
  });
  it("空内容返回空串", () => {
    expect(tailPreviewText("")).toBe("");
  });
});

describe("StreamingThoughtBlock 三态", () => {
  it("streaming 中显示尾部预览而非全文", () => {
    render(
      <StreamingThoughtBlock
        initialContent={"第一行思考\n第二行思考\n第三行思考\n最新一行"}
        isStreaming
      />,
    );
    const preview = screen.queryByTestId("think-tail-preview");
    expect(preview).not.toBeNull();
    // 预览只含尾部 3 行，首行不在其中
    expect(preview!.textContent).toContain("最新一行");
    expect(preview!.textContent).not.toContain("第一行思考");
    // 全文 markdown 不渲染
    expect(screen.queryByTestId("markdown")).toBeNull();
  });

  it("streaming 中点击标题展开全文", async () => {
    const user = userEvent.setup();
    render(
      <StreamingThoughtBlock initialContent="完整思考内容" isStreaming />,
    );
    expect(screen.queryByTestId("markdown")).toBeNull();
    await user.click(screen.getByRole("button"));
    expect(screen.queryByTestId("markdown")).not.toBeNull();
    // 展开后预览区让位
    expect(screen.queryByTestId("think-tail-preview")).toBeNull();
  });

  it("streaming 结束且用户未表态 → 自动折叠并定格用时", async () => {
    const { rerender } = render(
      <StreamingThoughtBlock initialContent="一些思考" isStreaming />,
    );
    rerender(<StreamingThoughtBlock initialContent="一些思考" isStreaming={false} />);

    await waitFor(() => {
      expect(screen.queryByTestId("think-tail-preview")).toBeNull();
      expect(screen.queryByTestId("markdown")).toBeNull();
    });
    expect(screen.queryByText(/思考过程 · \d/)).not.toBeNull();
    // 折叠态显示最后一行预览
    expect(screen.queryByText("一些思考")).not.toBeNull();
  });

  it("用户手动展开后，结束时保持展开", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <StreamingThoughtBlock initialContent="一些思考" isStreaming />,
    );
    await user.click(screen.getByRole("button"));
    expect(screen.queryByTestId("markdown")).not.toBeNull();

    rerender(<StreamingThoughtBlock initialContent="一些思考" isStreaming={false} />);

    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("markdown")).not.toBeNull();
  });

  it("历史恢复（无 streaming 经历）折叠态显示末行预览、无用时", () => {
    render(
      <StreamingThoughtBlock
        initialContent={"第一行思考\n最终的结论行"}
        isStreaming={false}
        defaultOpen={false}
      />,
    );
    expect(screen.queryByText("最终的结论行")).not.toBeNull();
    expect(screen.queryByText("思考过程")).not.toBeNull();
    expect(screen.queryByText(/思考过程 · /)).toBeNull();
    expect(screen.queryByTestId("markdown")).toBeNull();
  });

  it("无内容且不 streaming 时不渲染", () => {
    const { container } = render(
      <StreamingThoughtBlock initialContent="" isStreaming={false} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
