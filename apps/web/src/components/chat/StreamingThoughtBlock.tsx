import { useAuthContext } from "@/contexts/AuthContext";
import type { ChatSegment } from "@/pages/WorkspacePage/types";
import { Brain, ChevronDown, Loader2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PreviewFile } from "@/components/layout/WorkspaceSidebar/preview";
import { ChartAwareMarkdown } from "./ChartAwareMarkdown";

/** 思考用时格式化：<60s 显示秒（一位小数），否则「Xm Ys」。对齐 grok-build 的 "Thought for 2.3s" */
export function formatThinkDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

/** 取最后一行非空内容作为折叠态预览（grok-build Truncated 模式的极简版） */
export function lastContentLine(content: string): string | undefined {
  const lines = content.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const trimmed = lines[i].trim();
    if (trimmed.length > 0) return trimmed;
  }
  return undefined;
}

/** streaming 中尾部预览的行数（对齐 step-code THINKING_PREVIEW_LINES = 3） */
export const TAIL_PREVIEW_LINES = 3;

/**
 * 取内容尾部 n 行作为 streaming 滚动预览。
 * 渲染为纯文本不走 markdown——预览区不需要格式，且半截 markdown 在
 * 流式中途必然出现，纯文本渲染天然规避（step-code 同款取舍：
 * transient=true 时关高亮只透传原文）。
 */
export function tailPreviewText(
  content: string,
  n: number = TAIL_PREVIEW_LINES,
): string {
  return content.split("\n").slice(-n).join("\n");
}

interface StreamingThoughtBlockProps {
  /**
   * 初始内容（用于非流式场景或恢复历史）
   */
  initialContent?: string;
  /**
   * 是否正在流式输出
   */
  isStreaming?: boolean;
  /**
   * 流式事件订阅器
   * 返回取消订阅的函数
   */
  subscribeToStream?: (
    onChunk: (chunk: string, isFinal: boolean) => void,
  ) => () => void;
  /**
   * 默认是否展开
   */
  defaultOpen?: boolean;
  onOpenInMainCanvas?: (file: PreviewFile) => void;
  onOpenInBrowserTab?: (path: string) => void;
}

const normalizeMarkdown = (value?: string) =>
  String(value ?? "")
    .replace(/[\0]/g, "")
    .replace(/\r\n/g, "\n");

/**
 * 流式思考过程组件
 *
 * 特点：
 * 1. 自己维护内部 state，不依赖外部频繁更新
 * 2. 通过订阅模式接收流式数据
 * 3. 内部节流渲染，避免过于频繁的 DOM 更新
 */
export function StreamingThoughtBlock({
  initialContent = "",
  isStreaming = false,
  subscribeToStream,
  defaultOpen = false,
  onOpenInMainCanvas,
  onOpenInBrowserTab,
}: StreamingThoughtBlockProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [content, setContent] = useState(initialContent);
  const [streaming, setStreaming] = useState(isStreaming);
  const { session } = useAuthContext();
  const token = session?.token;

  // 用于累积内容的 ref，避免闭包问题
  const contentRef = useRef(initialContent);
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 用户手动折叠/展开过后，完成时不再替他自动折叠（grok-build 同款约定：
  // finished_display_mode 只作用于用户没有表态的条目）
  const userToggledRef = useRef(false);
  // 思考计时：进入 streaming 时打点，结束时定格
  const startTimeRef = useRef<number | null>(isStreaming ? Date.now() : null);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const prevStreamingRef = useRef(streaming);

  const markFinished = useCallback(() => {
    if (startTimeRef.current !== null) {
      setDurationMs(Date.now() - startTimeRef.current);
      startTimeRef.current = null;
    }
    if (!userToggledRef.current) {
      setIsOpen(false);
    }
  }, []);

  // streaming 边沿检测：false→true 打点；true→false 定格用时并按需自动折叠
  useEffect(() => {
    if (prevStreamingRef.current === streaming) return;
    prevStreamingRef.current = streaming;
    if (streaming) {
      if (startTimeRef.current === null) startTimeRef.current = Date.now();
    } else {
      markFinished();
    }
  }, [streaming, markFinished]);

  // 清理内容中的特殊标记
  // 只清理 <think> 标签，保留 <code> 标签内容（后者是合法 Markdown/HTML）
  const cleanedContent = useMemo(() => {
    return normalizeMarkdown(content)
      .replace(/<\/?think>/g, "")
      .trim();
  }, [content]);

  // 节流刷新到 state
  const scheduleFlush = useCallback(() => {
    if (flushTimerRef.current) return;
    flushTimerRef.current = setTimeout(() => {
      flushTimerRef.current = null;
      setContent(contentRef.current);
    }, 50);
  }, []);

  // 立即刷新
  const flushNow = useCallback(() => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setContent(contentRef.current);
  }, []);

  // 订阅流式事件
  useEffect(() => {
    if (!subscribeToStream) return;

    const handleChunk = (chunk: string, isFinal: boolean) => {
      contentRef.current += chunk;

      if (isFinal) {
        setStreaming(false);
        flushNow();
      } else {
        scheduleFlush();
      }
    };

    setStreaming(true);
    const unsubscribe = subscribeToStream(handleChunk);

    return () => {
      unsubscribe();
      if (flushTimerRef.current) {
        clearTimeout(flushTimerRef.current);
      }
    };
  }, [subscribeToStream, scheduleFlush, flushNow]);

  // 同步外部 isStreaming 状态
  useEffect(() => {
    setStreaming(isStreaming);
  }, [isStreaming]);

  // 同步初始内容
  useEffect(() => {
    if (initialContent && !subscribeToStream) {
      contentRef.current = initialContent;
      setContent(initialContent);
    }
  }, [initialContent, subscribeToStream]);

  if (!cleanedContent && !streaming) {
    return null;
  }

  // 折叠态预览：最后一行非空内容（grok-build Truncated 模式）。
  // 只在「结束后折叠」时显示——streaming 中的预览由下方尾部滚动区承担，
  // 标题行不再重复显示同一行内容
  const collapsedPreview =
    !isOpen && !streaming && cleanedContent
      ? lastContentLine(cleanedContent)
      : undefined;

  // streaming 中且未展开：尾部 N 行滚动预览（step-code ThinkingPreview 形态）
  const showTailPreview = streaming && !isOpen && cleanedContent.length > 0;

  const title = streaming
    ? "思考中…"
    : durationMs !== null
      ? `思考过程 · ${formatThinkDuration(durationMs)}`
      : "思考过程";

  return (
    <div className="mb-3 rounded-lg border border-border/60 bg-muted/20 overflow-hidden">
      <button
        onClick={() => {
          userToggledRef.current = true;
          setIsOpen(!isOpen);
        }}
        className="group relative flex w-full items-center gap-2.5 px-3.5 py-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
      >
        {streaming && <span className="aiasys-think-sweep" aria-hidden="true" />}
        <div
          className={`flex items-center justify-center w-5 h-5 rounded-md flex-shrink-0 transition-colors ${streaming ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground group-hover:bg-muted-foreground/10"}`}
        >
          <Brain size={12} className={streaming ? "animate-pulse" : ""} />
        </div>
        <span className="font-medium flex-shrink-0">{title}</span>
        {collapsedPreview ? (
          <span className="min-w-0 flex-1 truncate text-left text-micro font-normal text-muted-foreground/60">
            {collapsedPreview}
          </span>
        ) : (
          <span className="text-nano text-muted-foreground/50 ml-1 flex-1 text-left">
            {isOpen ? "点击折叠" : "点击展开"}
          </span>
        )}
        {streaming && (
          <Loader2 size={11} className="animate-spin text-primary ml-auto flex-shrink-0" />
        )}
        <div
          className={`flex items-center justify-center w-4 h-4 flex-shrink-0 transition-transform ${isOpen ? "rotate-0" : "-rotate-90"} ${streaming ? "" : "ml-auto"}`}
        >
          <ChevronDown size={12} />
        </div>
      </button>

      {isOpen && (
        <div
          className="prose prose-sm max-w-none min-w-0 max-h-80 break-words overflow-y-auto border-t border-border/40 bg-muted/10 px-4 pb-3.5 pt-2 text-body leading-relaxed text-muted-foreground/90 [overflow-wrap:anywhere] [&_p]:my-1.5"
        >
          <ChartAwareMarkdown
            content={cleanedContent || (streaming ? "..." : "")}
            token={token}
            paragraphClassName="my-1"
            onOpenInMainCanvas={onOpenInMainCanvas}
            onOpenInBrowserTab={onOpenInBrowserTab}
          />
        </div>
      )}

      {/* streaming 尾部滚动预览：只占 3 行高度，纯文本跟随最新思考位置，
          顶部渐隐提示上方还有内容。点标题行展开全文。 */}
      {showTailPreview && (
        <div
          data-testid="think-tail-preview"
          className="border-t border-border/40 bg-muted/10 px-4 py-2"
        >
          <div className="max-h-[3.9em] overflow-hidden whitespace-pre-wrap break-words text-caption italic leading-[1.3em] text-muted-foreground/70 [overflow-wrap:anywhere] [mask-image:linear-gradient(to_bottom,transparent,black_45%)]">
            {tailPreviewText(cleanedContent)}
          </div>
        </div>
      )}
    </div>
  );
}

interface StreamingSegmentsRendererProps {
  /**
   * 当前的 segments 列表
   */
  segments: ChatSegment[];
  /**
   * 是否正在流式输出
   */
  isStreaming: boolean;
}

/**
 * 流式 Segments 渲染器
 *
 * 优化：对于 thought 类型使用 StreamingThoughtBlock 独立渲染
 */
export function StreamingSegmentsRenderer({
  segments,
  isStreaming,
}: StreamingSegmentsRendererProps) {
  // 合并连续的 thought segments
  const mergedSegments = useMemo(() => {
    const result: ChatSegment[] = [];
    let currentThought: ChatSegment | null = null;

    for (const seg of segments) {
      if (seg.type === "thought") {
        if (currentThought) {
          currentThought = {
            type: currentThought.type,
            content: currentThought.content + seg.content,
            toolName: currentThought.toolName,
            toolParams: currentThought.toolParams,
            isComplete: currentThought.isComplete,
          };
        } else {
          currentThought = { ...seg };
        }
      } else {
        if (currentThought) {
          result.push(currentThought);
          currentThought = null;
        }
        result.push(seg);
      }
    }

    if (currentThought) {
      result.push(currentThought);
    }

    return result;
  }, [segments]);

  return (
    <div className="flex flex-col">
      {mergedSegments.map((seg, idx) => {
        const isLast = idx === mergedSegments.length - 1;

        if (seg.type === "thought") {
          return (
            <StreamingThoughtBlock
              key={`thought-${idx}`}
              initialContent={seg.content}
              isStreaming={isStreaming && isLast}
              defaultOpen={false}
            />
          );
        }

        // 其他类型由父组件处理
        return null;
      })}
    </div>
  );
}
