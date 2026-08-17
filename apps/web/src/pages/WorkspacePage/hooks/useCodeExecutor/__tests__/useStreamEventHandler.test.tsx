/**
 * useStreamEventHandler 的 think/text 分流测试。
 *
 * 选它做第一个业务 hook 测试，是因为三个已知痛点在这里汇聚：thinking 内容泄露
 * 正文、think 段碎片化、display_hint 的渲染语义。后端已经用状态机
 * （ReasoningTagSplitter）把 <think> 从正文里切出来了，但前端这一层是第二道关：
 * 事件到了以后归到哪个 segment、什么时候合并、什么时候另起一段，全在这个 hook 里。
 *
 * 测试手法：handleStreamEvent 是同步直接改 slot.streamingSegments 的（拿的是
 * 引用），120ms 的 scheduleFlush 只负责把命令式数据推给 React 渲染。所以断言直接
 * 看 slot.streamingSegments，不需要 fake timer，也不依赖渲染结果——这样测的是分流
 * 逻辑本身，而不是渲染时序。
 *
 * slot 用真实的 createEmptySlot() 构造而不是手写字面量：结构变化时测试自动跟上，
 * 不会出现「mock 的 slot 和真 slot 早就不一样了，测试还在绿」的情况。
 */
import { renderHook } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createEmptySlot, type SessionSlot } from "../sessionRegistry";
import { useStreamEventHandler } from "../useStreamEventHandler";

vi.mock("@/lib/eventBus", () => ({
  eventBus: { emit: vi.fn(), on: vi.fn(), off: vi.fn() },
  EVENTS: {},
}));
vi.mock("@/lib/runtimeToolEvents", () => ({
  shouldTrackExecutionFlowTool: vi.fn(() => false),
}));

const SESSION = "s-1";

type HandleStreamEvent = ReturnType<typeof useStreamEventHandler>["handleStreamEvent"];
type StreamEventArg = Parameters<HandleStreamEvent>[1];

/** 构造 content 事件。字段形状跟随后端 SSE，故用 cast 而非补全整个 AgentEvent。 */
function contentEvent(payload: Record<string, unknown>): StreamEventArg {
  return { type: "content", ...payload } as unknown as StreamEventArg;
}

interface Harness {
  slot: SessionSlot;
  send: (event: StreamEventArg) => void;
  segments: () => SessionSlot["streamingSegments"];
}

/** hook 不再接收会话运行状态，见 think 合并条件的注释。 */
function setup(): Harness {
  const slot = createEmptySlot();
  slot.streamingMessageId = "msg-1";

  const { result } = renderHook(() =>
    useStreamEventHandler({
      getSessionSlot: () => slot,
      updateChatItems: vi.fn(),
      addStreamEventsForSession: vi.fn(),
    }),
  );

  return {
    slot,
    send: (event) => {
      act(() => {
        result.current.handleStreamEvent(SESSION, event);
      });
    },
    segments: () => slot.streamingSegments,
  };
}

describe("think 与 text 的分流（thinking 泄露正文的第二道关）", () => {
  let h: Harness;

  beforeEach(() => {
    h = setup();
  });

  it("think 增量只进 think 段，不产生任何 text 段", () => {
    h.send(contentEvent({ content_type: "think", think: "推理中" }));

    expect(h.segments()).toHaveLength(1);
    expect(h.segments()[0]).toMatchObject({ type: "think", content: "推理中" });
    expect(h.segments().filter((s) => s.type === "text")).toHaveLength(0);
  });

  it("text 增量只进 text 段，不被误判成推理", () => {
    h.send(contentEvent({ content_type: "text", text: "正文" }));

    expect(h.segments()).toHaveLength(1);
    expect(h.segments()[0]).toMatchObject({ type: "text", content: "正文" });
  });

  it("think 之后来 text：think 段被立即收尾，text 另起一段", () => {
    h.send(contentEvent({ content_type: "think", think: "先想" }));
    h.send(contentEvent({ content_type: "text", text: "再答" }));

    const segs = h.segments();
    expect(segs).toHaveLength(2);
    // isComplete 决定 spinner 停不停：不收尾的话思考块会一直转到整条消息结束
    expect(segs[0]).toMatchObject({ type: "think", content: "先想", isComplete: true });
    expect(segs[1]).toMatchObject({ type: "text", content: "再答" });
  });

  it("text 之后来 think：不会把推理追加进已有正文段", () => {
    h.send(contentEvent({ content_type: "text", text: "正文" }));
    h.send(contentEvent({ content_type: "think", think: "推理" }));

    const segs = h.segments();
    expect(segs).toHaveLength(2);
    expect(segs[0]).toMatchObject({ type: "text", content: "正文" });
    expect(segs[1]).toMatchObject({ type: "think", content: "推理" });
    // 关键断言：正文段不能被推理污染
    expect(segs[0].content).toBe("正文");
  });

  it("空 think / 空 text 事件被忽略，不产生空段", () => {
    h.send(contentEvent({ content_type: "think", think: "" }));
    h.send(contentEvent({ content_type: "text", text: "" }));

    expect(h.segments()).toHaveLength(0);
  });
});

describe("流式增量的合并", () => {
  it("streaming 中的连续 think 增量合并成一段", () => {
    const h = setup();
    h.send(contentEvent({ content_type: "think", think: "a" }));
    h.send(contentEvent({ content_type: "think", think: "b" }));
    h.send(contentEvent({ content_type: "think", think: "c" }));

    expect(h.segments()).toHaveLength(1);
    expect(h.segments()[0].content).toBe("abc");
  });

  it("连续 text 增量合并成一段", () => {
    const h = setup();
    h.send(contentEvent({ content_type: "text", text: "x" }));
    h.send(contentEvent({ content_type: "text", text: "y" }));

    expect(h.segments()).toHaveLength(1);
    expect(h.segments()[0].content).toBe("xy");
  });

  // 这条是本文件的调查起点，也是修复后的回归守护。原实现的 think 合并条件里多带
  // 一个 `&& isSessionRunning(sessionId)`，而 text 分支没有。实测三个 think 增量在
  // 非运行状态下会产出三个 segment（UI 上一串碎片思考块），同条件下 text 正常合并。
  // 触发窗口真实存在：isSessionRunning 读 `...?.state.isRunning ?? false`，流末尾
  // 与用户点中断后它已为 false，而缓冲里的 content 事件仍在到达。
  //
  // 修法是移除该条件，让判据只剩「上一段是未收尾的 think」，与 text 对称；段落边界
  // 本就由 closePendingThink 的四个调用点负责。因此现在 hook 已不接收会话运行状态，
  // 那个场景在类型层面就无法再构造——这条断言守的是合并本身不看任何外部状态。
  it("连续 think 增量始终合并成一段，不受会话运行状态影响", () => {
    const h = setup();
    h.send(contentEvent({ content_type: "think", think: "a" }));
    h.send(contentEvent({ content_type: "think", think: "b" }));
    h.send(contentEvent({ content_type: "think", think: "c" }));

    expect(h.segments()).toHaveLength(1);
    expect(h.segments()[0].content).toBe("abc");
  });

  it("被收尾的 think 段不再接受追加，新增量另起一段", () => {
    const h = setup();
    h.send(contentEvent({ content_type: "think", think: "第一轮" }));
    // text 到达会触发 closePendingThink，把上面那段标成 isComplete
    h.send(contentEvent({ content_type: "text", text: "答复" }));
    h.send(contentEvent({ content_type: "think", think: "第二轮" }));

    const segs = h.segments();
    expect(segs).toHaveLength(3);
    expect(segs[0]).toMatchObject({ type: "think", content: "第一轮", isComplete: true });
    expect(segs[2]).toMatchObject({ type: "think", content: "第二轮", isComplete: false });
  });
});

describe("display_hint 的消费（后端定语义，前端定排版）", () => {
  it.each(["visible", "collapsed", "hidden"] as const)(
    "text 段透传 display_hint=%s",
    (hint) => {
      const h = setup();
      h.send(contentEvent({ content_type: "text", text: "内容", display_hint: hint }));

      expect(h.segments()[0].display_hint).toBe(hint);
    },
  );

  it.each(["visible", "collapsed", "hidden"] as const)(
    "think 段透传 display_hint=%s",
    (hint) => {
      const h = setup();
      h.send(contentEvent({ content_type: "think", think: "推理", display_hint: hint }));

      expect(h.segments()[0].display_hint).toBe(hint);
    },
  );

  it("缺省 display_hint 时降级为 visible，不静默吞内容", () => {
    const h = setup();
    h.send(contentEvent({ content_type: "text", text: "内容" }));
    h.send(contentEvent({ content_type: "think", think: "推理" }));

    expect(h.segments()[0].display_hint).toBe("visible");
    expect(h.segments()[1].display_hint).toBe("visible");
  });
});
