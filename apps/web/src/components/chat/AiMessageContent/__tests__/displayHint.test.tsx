import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AiMessageContent } from "../index";
import type { ChatSegment } from "@/pages/WorkspacePage/types";

// StreamingThoughtBlock 内部依赖 AuthProvider（useAuthContext 会在缺 Provider 时抛错），
// 而本文件测的是「哪些段落该进 DOM」这层判断，与推理块自身的实现无关。
// 换成一个把 content 原样渲染出来的替身：可见性判断若写错，泄露的文本照样会出现在
// DOM 里被下面的断言抓住，所以这个 mock 不会削弱断言强度。
vi.mock("../../StreamingThoughtBlock", () => ({
  StreamingThoughtBlock: ({ initialContent }: { initialContent?: string }) => (
    <div data-testid="thought-block">{initialContent}</div>
  ),
}));

/**
 * display_hint 的渲染契约测试。
 *
 * 这条链路是后端「哪些消息该给用户看」这个决定的最后一公里：后端按 origin 映射出
 * display_hint（system / compaction_summary → hidden，contextual_user / forked →
 * collapsed），前端在这里落实。后端侧已有 test_display_hint.py 覆盖映射表，
 * 但映射对了、渲染判断写错，用户照样会看到本该隐藏的注入消息——那类 bug 只能在
 * 这一层拦住。
 *
 * 一个实测得来的关键事实（不要凭直觉改这些断言）：collapsed 与 hidden 在「内容文本
 * 是否出现在 DOM 里」这一点上**无法区分**。Radix Collapsible 在收起状态下根本不挂载
 * 子内容，只留一个 hidden 的空容器。所以：
 *   - 判断 hidden：内容不在 DOM **且** 没有折叠触发器；
 *   - 判断 collapsed：内容不在 DOM **但** 有折叠触发器，点开后内容出现。
 * 只断言「内容不在 DOM」的话，hidden 与 collapsed 的实现互换也测不出来。
 */

const TEXT = "SEGMENT_BODY_MARKER";

function seg(overrides: Partial<ChatSegment> = {}): ChatSegment {
  return { type: "text", content: TEXT, ...overrides };
}

function renderSegments(segments: ChatSegment[]) {
  return render(<AiMessageContent isStreaming={false} segments={segments} />);
}

/** 折叠触发器：Radix 给它 aria-expanded，是 collapsed 与 hidden 唯一的可靠区分点。 */
function collapsedTriggers(container: HTMLElement): NodeListOf<HTMLElement> {
  return container.querySelectorAll<HTMLElement>("button[aria-expanded]");
}

describe("display_hint = visible", () => {
  it("内容直接出现在 DOM 里", () => {
    renderSegments([seg({ display_hint: "visible" })]);
    expect(screen.queryByText(TEXT)).not.toBeNull();
  });

  it("不套折叠容器", () => {
    const { container } = renderSegments([seg({ display_hint: "visible" })]);
    expect(collapsedTriggers(container)).toHaveLength(0);
  });
});

describe("display_hint 缺省", () => {
  it("按可见处理，不静默吞内容", () => {
    // 与后端 compute_display_hint 的兜底方向一致：宁可多显示，也不要让内容
    // 无声消失。前端这里若改成默认隐藏，旧版本后端（不发该字段）的所有消息
    // 都会整体空白。
    renderSegments([seg({ display_hint: undefined })]);
    expect(screen.queryByText(TEXT)).not.toBeNull();
  });
});

describe("display_hint = hidden", () => {
  it("内容不进 DOM", () => {
    renderSegments([seg({ display_hint: "hidden" })]);
    expect(screen.queryByText(TEXT)).toBeNull();
  });

  it("也不留折叠入口（与 collapsed 的区别就在这里）", () => {
    const { container } = renderSegments([seg({ display_hint: "hidden" })]);
    expect(collapsedTriggers(container)).toHaveLength(0);
  });
});

describe("display_hint = collapsed", () => {
  it("默认收起：内容不在 DOM，但留有折叠入口", () => {
    const { container } = renderSegments([seg({ display_hint: "collapsed" })]);
    expect(screen.queryByText(TEXT)).toBeNull();
    expect(collapsedTriggers(container)).toHaveLength(1);
  });

  it("触发器默认是关闭态", () => {
    const { container } = renderSegments([seg({ display_hint: "collapsed" })]);
    expect(collapsedTriggers(container)[0].getAttribute("aria-expanded")).toBe("false");
  });

  it("点开后内容出现——collapsed 是收起，不是丢弃", async () => {
    // 本文件里最重要的一条。前三条都只断言「看不到」，一个把 collapsed 实现成
    // 直接 return null 的坏改动能让它们全部通过。这条锁住的是内容仍然可达：
    // contextual_user / forked 这类注入消息默认不干扰阅读，但用户想查时必须查得到。
    const user = userEvent.setup();
    const { container } = renderSegments([seg({ display_hint: "collapsed" })]);

    await user.click(collapsedTriggers(container)[0]);

    expect(screen.queryByText(TEXT)).not.toBeNull();
  });

  it("折叠标题给出语义标签，而不是空按钮", () => {
    const { container } = renderSegments([seg({ display_hint: "collapsed" })]);
    const label = collapsedTriggers(container)[0].textContent?.trim() ?? "";
    // 只断言非空且不是纯符号：具体文案属产品措辞，钉死会让改文案必须改测试。
    expect(label.length).toBeGreaterThan(0);
    expect(/[\u4e00-\u9fa5A-Za-z]/.test(label)).toBe(true);
  });
});

describe("多段混合", () => {
  it("hidden 段被跳过，不影响相邻段渲染", () => {
    renderSegments([
      seg({ content: "FIRST_VISIBLE", display_hint: "visible" }),
      seg({ content: "MIDDLE_HIDDEN", display_hint: "hidden" }),
      seg({ content: "LAST_VISIBLE", display_hint: "visible" }),
    ]);

    expect(screen.queryByText("FIRST_VISIBLE")).not.toBeNull();
    expect(screen.queryByText("MIDDLE_HIDDEN")).toBeNull();
    expect(screen.queryByText("LAST_VISIBLE")).not.toBeNull();
  });

  it("可见段保持原有先后顺序", () => {
    // 顺序错乱不会让上面任何一条断言失败，但对话读起来会前后颠倒。
    const { container } = renderSegments([
      seg({ content: "ORDER_ONE", display_hint: "visible" }),
      seg({ content: "ORDER_TWO", display_hint: "hidden" }),
      seg({ content: "ORDER_THREE", display_hint: "visible" }),
    ]);

    const html = container.innerHTML;
    expect(html.indexOf("ORDER_ONE")).toBeGreaterThanOrEqual(0);
    expect(html.indexOf("ORDER_THREE")).toBeGreaterThan(html.indexOf("ORDER_ONE"));
  });

  it("多个相邻 collapsed 段合并成一个折叠块，展开后两段内容都在", async () => {
    // 相邻且可见性相同的段落被合并是有意行为（避免同一段话被切成几个折叠块），
    // 所以这里期望 1 个触发器而不是 2 个。要区分的是「合并同类」与「合并不同
    // 可见性」——后者是缺陷，由下面 display_hint 参与合并的那组用例守着。
    const user = userEvent.setup();
    const { container } = renderSegments([
      seg({ content: "COLLAPSED_A", display_hint: "collapsed" }),
      seg({ content: "COLLAPSED_B", display_hint: "collapsed" }),
    ]);

    const triggers = collapsedTriggers(container);
    expect(triggers).toHaveLength(1);

    await user.click(triggers[0]);

    // 合并后的文本是 "COLLAPSED_ACOLLAPSED_B"，精确文本匹配拿不到，查 innerHTML。
    expect(container.innerHTML).toContain("COLLAPSED_A");
    expect(container.innerHTML).toContain("COLLAPSED_B");
  });

  it("全部段落都是 hidden 时不渲染任何折叠入口或段落文本", () => {
    const { container } = renderSegments([
      seg({ content: "ALL_HIDDEN_1", display_hint: "hidden" }),
      seg({ content: "ALL_HIDDEN_2", display_hint: "hidden" }),
    ]);

    expect(screen.queryByText("ALL_HIDDEN_1")).toBeNull();
    expect(screen.queryByText("ALL_HIDDEN_2")).toBeNull();
    expect(collapsedTriggers(container)).toHaveLength(0);
  });
});

describe("display_hint 参与段落合并（2026-08-10 修的真缺陷）", () => {
  // 原实现的合并条件只比 type：
  //   if (lastSeg.type === seg.type && MERGEABLE_TYPES.has(seg.type)) lastSeg.content += seg.content
  // 于是相邻但可见性不同的两段被拼成一段，而渲染只看合并后那段的 hint，两个方向都错。
  // 实测原实现下 [visible, hidden, visible] 渲染出的文本是
  //   "FIRST_VISIBLEMIDDLE_HIDDENLAST_VISIBLE"
  // ——hidden 段的内容直接显示在界面上。
  //
  // 这不是理论风险：hidden 对应的 origin 是 system 与 compaction_summary，
  // 即系统提示与压缩摘要，本来就是不该给用户看的注入内容。

  it("hidden 段的内容不得被拼进相邻 visible 段", () => {
    const { container } = renderSegments([
      seg({ content: "VISIBLE_HEAD", display_hint: "visible" }),
      seg({ content: "LEAKY_HIDDEN_BODY", display_hint: "hidden" }),
    ]);

    expect(container.innerHTML).not.toContain("LEAKY_HIDDEN_BODY");
    expect(container.innerHTML).toContain("VISIBLE_HEAD");
  });

  it("反方向同样成立：hidden 在前时不得把后面的 visible 段一起吞掉", () => {
    // 这个方向的症状相反——正常回答整段消失，用户看到空白气泡。
    const { container } = renderSegments([
      seg({ content: "HIDDEN_HEAD", display_hint: "hidden" }),
      seg({ content: "VISIBLE_TAIL", display_hint: "visible" }),
    ]);

    expect(container.innerHTML).not.toContain("HIDDEN_HEAD");
    expect(container.innerHTML).toContain("VISIBLE_TAIL");
  });

  it("think 类型同样受保护（think 泄露的一条路径）", () => {
    // think 也在 MERGEABLE_TYPES 里，所以同一个缺陷会让 hidden 的推理内容
    // 被拼进可见的推理块。think 内容泄露是这个项目反复出现的问题，
    // 这条断言把其中一条机制钉死。
    const { container } = renderSegments([
      seg({ type: "think", content: "THINK_VISIBLE", display_hint: "visible" }),
      seg({ type: "think", content: "THINK_HIDDEN_LEAK", display_hint: "hidden" }),
    ]);

    expect(container.innerHTML).not.toContain("THINK_HIDDEN_LEAK");
  });

  it("collapsed 与 visible 相邻时不互相吞并", () => {
    const { container } = renderSegments([
      seg({ content: "PLAIN_VISIBLE", display_hint: "visible" }),
      seg({ content: "FOLDED_CONTEXT", display_hint: "collapsed" }),
    ]);

    // visible 段照常显示；collapsed 段收起（内容不在 DOM）但留有入口。
    expect(container.innerHTML).toContain("PLAIN_VISIBLE");
    expect(container.innerHTML).not.toContain("FOLDED_CONTEXT");
    expect(collapsedTriggers(container)).toHaveLength(1);
  });

  it("缺省 hint 与显式 visible 视为同一可见性，仍然合并", () => {
    // 归一化那一步的正向验证：undefined 与 "visible" 语义相同，不该因为
    // 字段有没有发而拆成两段渲染。若这里退化成两段，老后端的每个流式增量
    // 都会变成独立段落，界面会被切碎。
    const { container } = renderSegments([
      seg({ content: "MERGE_A", display_hint: undefined }),
      seg({ content: "MERGE_B", display_hint: "visible" }),
    ]);

    expect(container.innerHTML).toContain("MERGE_AMERGE_B");
  });
});

describe("display_hint 对不同 segment 类型一致生效", () => {
  // 后端的 display_hint 是按消息 origin 算的，与 segment 的内容类型无关。
  // 前端若把判断写进某个类型的分支里（例如只在 text 分支检查 hidden），
  // 其它类型的注入内容就会照常显示出来。
  const types: ChatSegment["type"][] = ["text", "thought", "think", "compaction_summary"];

  for (const type of types) {
    it(`${type}: hidden 时内容不进 DOM`, () => {
      renderSegments([seg({ type, content: `HIDDEN_${type}`, display_hint: "hidden" })]);
      expect(screen.queryByText(`HIDDEN_${type}`)).toBeNull();
    });
  }
});
