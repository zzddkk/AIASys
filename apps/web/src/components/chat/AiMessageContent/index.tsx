/**
 * AiMessageContent - AI 消息内容组件
 *
 * 重构为 Explicit Variants + Context 模式：
 * - 拆分为独立的子组件
 * - 通过 Context 共享状态
 * - 避免 props 过多
 *
 * 设计原则：
 * 1. 三个区域独立：思考区、工具区、回答区
 * 2. 每种类型的内容累积显示，不会相互覆盖
 * 3. 支持流式更新和历史恢复
 */
import { memo, useMemo } from "react";
import { RotateCcw, ChevronRight } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

import type { ChatSegment, WorkerRecord } from "@/pages/WorkspacePage/types";
import type { PreviewFile } from "@/components/layout/WorkspaceSidebar/preview";
import {
  AiMessageContext,
  type AiMessageActions,
  type AiMessageMeta,
  type AiMessageState,
} from "./context";
import { LoadingPlaceholder } from "./LoadingPlaceholder";
import { StoppedIndicator } from "./StoppedIndicator";
import { WorkerIndicators } from "./WorkerIndicators";
import { ToolBlock } from "./ToolBlock";
import { ToolCallRow } from "./ToolCallRow";
import { ToolCallGroupRow, groupToolCallSegments } from "./ToolCallGroupRow";
import {
  FileOperationNotice,
  extractFilePathFromToolParams,
} from "./FileOperationNotice";
import { StreamingThoughtBlock } from "../StreamingThoughtBlock";
import { ChartAwareMarkdown } from "../ChartAwareMarkdown";

// 导出 Context 和子组件
export { AiMessageContext, useAiMessageContext } from "./context";
export { LoadingPlaceholder } from "./LoadingPlaceholder";
export { StoppedIndicator } from "./StoppedIndicator";
export { ToolBlock } from "./ToolBlock";
export { ToolCallRow, summarizeToolParams } from "./ToolCallRow";
export { FileOperationNotice } from "./FileOperationNotice";
export { FinalAnswerBlock } from "./FinalAnswerBlock";
export { WorkerIndicators } from "./WorkerIndicators";

export interface AiMessageContentProps {
  /**
   * 最终答案内容（来自后端 message.content）
   */
  content?: string;
  /**
   * 分段内容：思考、工具调用、最终回答
   */
  segments?: ChatSegment[];
  /**
   * 是否正在流式输出
   */
  isStreaming: boolean;
  /**
   * 是否已终止
   */
  isStopped?: boolean;
  /**
   * Worker 状态记录
   */
  workerRecords?: WorkerRecord[];
  /**
   * Worker 点击回调
   */
  onWorkerClick?: (workerName: string) => void;
  /**
   * 查看工具调用详情回调 - 包含触发元素位置用于悬浮窗定位
   */
  onViewToolDetails?: (
    toolCallId: string,
    taskId: string | undefined,
    triggerRect: DOMRect,
  ) => void;
  /**
   * 关联的任务 ID
   */
  taskId?: string;
  /**
   * 会话 ID（用于加载工作区图片）
   */
  sessionId?: string;
  /**
   * 在主画布打开工作区产物
   */
  onOpenWorkspaceArtifact?: (file: PreviewFile) => void;
  onOpenInBrowserTab?: (path: string) => void;
  /** 打开执行资源面板 */
  onOpenRuntimeTab?: () => void;
  /** 重试上一次失败的提交 */
  onRetryLastSubmit?: () => Promise<void> | void;
  /**
   * 是否在消息流中内联显示工具执行结果（默认 false，结果通过弹窗查看）
   */
  showToolOutputs?: boolean;
}

// 从 segments 中分离出内容类型
function useSegmentData(segments?: ChatSegment[]) {
  return useMemo(() => {
    const thoughts: ChatSegment[] = [];
    const tools: ChatSegment[] = [];
    const texts: ChatSegment[] = [];
    const thinks: ChatSegment[] = [];
    const monitors: ChatSegment[] = [];
    const turns: ChatSegment[] = [];

    if (segments && segments.length > 0) {
      for (const seg of segments) {
        if (seg.type === "text") {
          texts.push(seg);
        } else if (seg.type === "thought") {
          thoughts.push(seg);
        } else if (seg.type === "think") {
          thinks.push(seg);
        } else if (seg.type === "tool_call" || seg.type === "tool_output") {
          tools.push(seg);
        } else if (seg.type === "monitor") {
          monitors.push(seg);
        } else if (seg.type === "turn") {
          turns.push(seg);
        }
      }
    }

    return { thoughts, tools, texts, thinks, monitors, turns };
  }, [segments]);
}

/** 获取 collapsed  segment 的折叠标题 */
function getCollapsedLabel(seg: ChatSegment): string {
  switch (seg.type) {
    case "text":
      return "背景上下文";
    case "think":
      return "推理过程（背景上下文）";
    case "thought":
      return "思考过程（背景上下文）";
    case "turn":
      return `Turn ${seg.turnN ?? "?"}（背景上下文）`;
    case "tool_call":
      return `工具调用：${seg.toolName ?? "未知"}（背景上下文）`;
    case "tool_output":
      return `工具结果：${seg.toolName ?? "未知"}（背景上下文）`;
    case "monitor":
      return `Monitor ${seg.monitorCommand ?? ""}（后台运行）`.trim();
    default:
      return "内容（背景上下文）";
  }
}

// Provider 组件
interface AiMessageProviderProps {
  children: React.ReactNode;
  state: AiMessageState;
  actions: AiMessageActions;
  meta: AiMessageMeta;
}

function AiMessageProvider({
  children,
  state,
  actions,
  meta,
}: AiMessageProviderProps) {
  return (
    <AiMessageContext value={{ state, actions, meta }}>
      {children}
    </AiMessageContext>
  );
}

// 主组件
export const AiMessageContent = memo(function AiMessageContent({
  content,
  segments,
  isStreaming,
  isStopped,
  workerRecords,
  onWorkerClick,
  onViewToolDetails,
  sessionId,
  taskId,
  onOpenWorkspaceArtifact,
  onOpenInBrowserTab,
  onOpenRuntimeTab,
  onRetryLastSubmit,
  showToolOutputs = false,
}: AiMessageContentProps) {
  const {
    thoughts: thoughtSegments,
    tools,
    texts,
    thinks,
    monitors,
    turns,
  } = useSegmentData(segments);
  const workerActivities = workerRecords ?? [];

  // 合并连续的思考内容
  const mergedThoughtContent = useMemo(() => {
    return thoughtSegments.map((t) => t.content).join("");
  }, [thoughtSegments]);

  // 合并回答内容
  const mergedFinalAnswer = useMemo(() => {
    return texts.map((t) => t.content).join("");
  }, [texts]);

  // 判断内容是否为空（考虑 content 字段和 segments）
  const hasContent = content && content.trim().length > 0;
  const isEmpty =
    !hasContent &&
    !mergedThoughtContent &&
    thinks.length === 0 &&
    tools.length === 0 &&
    !mergedFinalAnswer &&
    texts.length === 0 &&
    monitors.length === 0 &&
    turns.length === 0 &&
    (!segments || segments.length === 0);

  // 判断是否正在思考（没有最终回答时）
  const isThinking = isStreaming && !mergedFinalAnswer;

  // 构建 state
  const state: AiMessageState = {
    thoughts: mergedThoughtContent,
    thinks,
    isThinking,
    isStopped: isStopped || false,
    isEmpty,
    isStreaming,
    tools,
    finalAnswer: mergedFinalAnswer,
    workerActivities,
    monitors,
  };

  // 构建 actions
  const actions: AiMessageActions = {
    onWorkerClick,
  };

  // 构建 meta，当前不需要手动传递 token
  const meta: AiMessageMeta = {
    token: undefined,
    sessionId,
    onOpenWorkspaceArtifact,
    onOpenInBrowserTab,
    onOpenRuntimeTab,
    onRetryLastSubmit,
  };

  // 按顺序渲染 segments，保持 segments 原始到达/恢复顺序
  // 不再按类型排序，避免 multi-turn 场景下 turn 标记与对应内容被拆散堆叠
  // 使用 useMemo 缓存渲染结果，避免每次重渲染都重新合并和构建 JSX
  const renderSegments = useMemo(() => {
    if (!segments || segments.length === 0) {
      return null;
    }

    // 合并连续的同类型 segments
    // 仅合并 text / think，tool_call / tool_output / monitor / turn 保持独立
    //
    // display_hint 必须参与合并条件，否则相邻但可见性不同的两段会被拼成一段，
    // 而下面只按合并后那段的 hint 决定渲染，于是两个方向都出错：
    //   [visible, hidden] → 合并后按 visible 渲染，hidden 段的内容被显示出来；
    //   [hidden, visible] → 合并后按 hidden 渲染，visible 段的内容整段消失。
    // 前者是注入内容（system / compaction_summary / contextual_user）泄露到界面，
    // 后者是正常回答丢失。think 也在可合并类型里，所以这条同样是 think 内容
    // 泄露的一条路径。
    //
    // 缺省值归一到 "visible" 再比较：undefined 与 "visible" 语义相同，
    // 直接比原值会让老后端（不发该字段）的连续段落无法合并，白白退化成多段渲染。
    const MERGEABLE_TYPES = new Set<string>(["text", "think"]);
    const mergedSegments: ChatSegment[] = [];
    for (const seg of segments) {
      const lastSeg = mergedSegments[mergedSegments.length - 1];
      if (
        lastSeg &&
        lastSeg.type === seg.type &&
        MERGEABLE_TYPES.has(seg.type) &&
        (lastSeg.display_hint ?? "visible") === (seg.display_hint ?? "visible")
      ) {
        lastSeg.content += seg.content;
      } else {
        mergedSegments.push({ ...seg });
      }
    }

    // 构建 toolCallId → toolParams 映射，用于 tool_output 渲染时提取文件路径
    const toolParamsByCallId = new Map<string, string>();
    for (const seg of mergedSegments) {
      if (seg.type === "tool_call" && seg.toolCallId && seg.toolParams) {
        toolParamsByCallId.set(seg.toolCallId, seg.toolParams);
      }
    }

    // 连续同名 tool_call 聚合分组（Kimi Code ReadGroup 思路，纯渲染层变换）。
    // 分组的段替换成一个 group 单元，其余段保持原顺序原索引。
    const renderUnits = groupToolCallSegments(mergedSegments);

    // 按顺序渲染合并后的 segments
    return renderUnits.map((unit) => {
      if (unit.kind === "group") {
        return (
          <ToolCallGroupRow
            key={`tool-group-${unit.index}`}
            group={unit.group}
            isMessageStreaming={isStreaming}
            onCallClick={
              onViewToolDetails
                ? (callSeg, callIdx, rect) =>
                    onViewToolDetails(
                      callSeg.toolCallId ||
                        callSeg.toolName ||
                        `tool-${unit.index}-${callIdx}`,
                      taskId,
                      rect,
                    )
                : undefined
            }
          />
        );
      }
      const seg = unit.segment;
      const idx = unit.index;
      // hidden：不进 DOM
      if (seg.display_hint === "hidden") {
        return null;
      }

      // collapsed：用 Collapsible 包裹，默认收起，标题带语义描述
      const collapsedContent = (() => {
        if (seg.type === "think") {
          return (
            <StreamingThoughtBlock
              initialContent={seg.content}
              isStreaming={isStreaming && !seg.isComplete}
              defaultOpen={false}
              onOpenInMainCanvas={onOpenWorkspaceArtifact}
              onOpenInBrowserTab={onOpenInBrowserTab}
            />
          );
        }

        if (seg.type === "text") {
          const processedContent = seg.content
            .replace(
              /<img[^>]+src=["']([^"']+)["'][^>]*alt=["']([^"']*)["'][^>]*\/?>/gi,
              (_match, src, alt) => `![${alt}](${src})`,
            )
            .replace(
              /<img[^>]+alt=["']([^"']*)["'][^>]*src=["']([^"']+)["'][^>]*\/?>/gi,
              (_match, alt, src) => `![${alt}](${src})`,
            );
          return (
            <div
              className={`prose prose-sm max-w-none min-w-0 break-all ${seg.isError ? "rounded-md border border-red-200 bg-red-50/50 px-3 py-2" : ""}`}
            >
              <ChartAwareMarkdown
                content={processedContent}
                token={undefined}
                sessionId={sessionId}
                onOpenInMainCanvas={onOpenWorkspaceArtifact}
                onOpenInBrowserTab={onOpenInBrowserTab}
              />
            </div>
          );
        }

        if (seg.type === "tool_call") {
          return (
            <ToolCallRow
              toolName={seg.toolName}
              toolParams={seg.toolParams}
              isComplete={seg.isComplete}
              isError={seg.isError}
              isMessageStreaming={isStreaming}
              onClick={(rect) =>
                onViewToolDetails?.(
                  seg.toolCallId || seg.toolName || `tool-${idx}`,
                  taskId,
                  rect,
                )
              }
            />
          );
        }

        if (seg.type === "tool_output") {
          const toolParams = seg.toolCallId
            ? toolParamsByCallId.get(seg.toolCallId)
            : undefined;
          const filePath = extractFilePathFromToolParams(
            seg.toolName,
            toolParams,
          );
          const showFileNotice = filePath && !seg.isError;

          return (
            <div>
              {showFileNotice && (
                <FileOperationNotice
                  toolName={seg.toolName || ""}
                  filePath={filePath}
                />
              )}
              {showToolOutputs && (
                <ToolBlock
                  title={`${seg.toolName || "工具"} ${seg.isError ? "错误" : "执行结果"}`}
                  content={
                    seg.content && seg.content.trim().length > 0
                      ? seg.content
                      : "（无输出）"
                  }
                  defaultOpen={seg.isError || false}
                  isError={seg.isError}
                />
              )}
            </div>
          );
        }

        if (seg.type === "monitor") {
          const isRunning = seg.monitorStatus === "running";
          const statusColor = seg.isError
            ? "border-red-200 bg-red-50"
            : seg.isComplete
              ? "border-green-200 bg-green-50"
              : "border-amber-200 bg-amber-50";
          const statusText = seg.isError
            ? "失败"
            : seg.isComplete
              ? "完成"
              : "运行中";
          return (
            <div
              className={`my-2 mx-3 rounded-lg border ${statusColor} overflow-hidden`}
            >
              <div className="flex items-center gap-2 px-3 py-2 border-b border-black/5">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className={isRunning ? "animate-pulse text-amber-600" : seg.isError ? "text-red-600" : "text-green-600"}
                >
                  <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
                  <line x1="8" y1="21" x2="16" y2="21" />
                  <line x1="12" y1="17" x2="12" y2="21" />
                </svg>
                <span className="text-micro font-medium text-foreground/80 truncate">
                  Monitor {seg.monitorCommand}
                </span>
                <span className={`text-nano ml-auto px-1.5 py-0.5 rounded ${seg.isError ? "bg-red-100 text-red-700" : seg.isComplete ? "bg-green-100 text-green-700" : "bg-amber-100 text-amber-700"}`}>
                  {statusText}{seg.monitorExitCode !== null && seg.monitorExitCode !== undefined ? ` (${seg.monitorExitCode})` : ""}
                </span>
              </div>
              {seg.content && (
                <pre className="px-3 py-2 text-micro font-mono text-muted-foreground max-h-48 overflow-auto whitespace-pre-wrap break-all">
                  {seg.content}
                </pre>
              )}
              {isRunning && (
                <div className="px-3 py-1.5 border-t border-black/5">
                  <div className="flex items-center gap-1.5">
                    <div className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                    <span className="text-nano text-muted-foreground">后台运行中...</span>
                  </div>
                </div>
              )}
            </div>
          );
        }

        if (seg.type === "turn") {
          return (
            <div
              className="flex w-full items-center gap-3 my-4"
            >
              <div className="h-px flex-1 bg-border/70" />
              <span className="rounded-full bg-muted px-2.5 py-0.5 text-nano font-medium text-muted-foreground whitespace-nowrap">
                Turn {seg.turnN ?? "?"}
              </span>
              <div className="h-px flex-1 bg-border/70" />
            </div>
          );
        }

        return null;
      })();

      if (seg.display_hint === "collapsed") {
        return (
          <Collapsible key={`seg-${seg.type}-${idx}`} defaultOpen={false}>
            <CollapsibleTrigger className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors mb-1 select-none">
              <ChevronRight className="h-3 w-3 transition-transform duration-200" />
              {getCollapsedLabel(seg)}
            </CollapsibleTrigger>
            <CollapsibleContent>
              {collapsedContent}
            </CollapsibleContent>
          </Collapsible>
        );
      }

      // visible：原有渲染
      if (seg.type === "think") {
        return (
          <StreamingThoughtBlock
            key={`seg-think-${idx}`}
            initialContent={seg.content}
            isStreaming={isStreaming && !seg.isComplete}
            defaultOpen={false}
            onOpenInMainCanvas={onOpenWorkspaceArtifact}
            onOpenInBrowserTab={onOpenInBrowserTab}
          />
        );
      }

      if (seg.type === "text") {
        // 将 HTML <img> 标签转换为 Markdown 格式，以便统一处理
        const processedContent = seg.content
          .replace(
            /<img[^>]+src=["']([^"']+)["'][^>]*alt=["']([^"']*)["'][^>]*\/?>/gi,
            (_match, src, alt) => `![${alt}](${src})`,
          )
          .replace(
            /<img[^>]+alt=["']([^"']*)["'][^>]*src=["']([^"']+)["'][^>]*\/?>/gi,
            (_match, alt, src) => `![${alt}](${src})`,
          );
        return (
          <div
            key={`seg-text-${idx}`}
            className={`prose prose-sm max-w-none min-w-0 break-all ${seg.isError ? "rounded-md border border-red-200 bg-red-50/50 px-3 py-2" : ""}`}
          >
            <ChartAwareMarkdown
              content={processedContent}
              token={undefined}
              sessionId={sessionId}
              onOpenInMainCanvas={onOpenWorkspaceArtifact}
              onOpenInBrowserTab={onOpenInBrowserTab}
            />
            {seg.isError && onRetryLastSubmit ? (
              <button
                type="button"
                onClick={() => void onRetryLastSubmit()}
                className="mt-2 flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-micro font-medium text-foreground transition-colors hover:bg-muted"
              >
                <RotateCcw className="h-3 w-3" />
                重试
              </button>
            ) : null}
          </div>
        );
      }

      if (seg.type === "tool_call") {
        return (
          <ToolCallRow
            key={`seg-tool-${idx}`}
            toolName={seg.toolName}
            toolParams={seg.toolParams}
            isComplete={seg.isComplete}
            isError={seg.isError}
            isMessageStreaming={isStreaming}
            onClick={(rect) =>
              onViewToolDetails?.(
                seg.toolCallId || seg.toolName || `tool-${idx}`,
                taskId,
                rect,
              )
            }
          />
        );
      }

      if (seg.type === "tool_output") {
        // 检测文件写入类工具操作，成功时显示内联文件通知
        const toolParams = seg.toolCallId
          ? toolParamsByCallId.get(seg.toolCallId)
          : undefined;
        const filePath = extractFilePathFromToolParams(
          seg.toolName,
          toolParams,
        );
        const showFileNotice = filePath && !seg.isError;

        if (!showToolOutputs && !showFileNotice) return null;

        return (
          <div key={`seg-tool-output-${idx}`}>
            {showFileNotice && (
              <FileOperationNotice
                toolName={seg.toolName || ""}
                filePath={filePath}
              />
            )}
            {showToolOutputs && (
              <ToolBlock
                title={`${seg.toolName || "工具"} ${seg.isError ? "错误" : "执行结果"}`}
                content={
                  seg.content && seg.content.trim().length > 0
                    ? seg.content
                    : "（无输出）"
                }
                defaultOpen={seg.isError || false}
                isError={seg.isError}
              />
            )}
          </div>
        );
      }

      if (seg.type === "monitor") {
        const isRunning = seg.monitorStatus === "running";
        const statusColor = seg.isError
          ? "border-red-200 bg-red-50"
          : seg.isComplete
            ? "border-green-200 bg-green-50"
            : "border-amber-200 bg-amber-50";
        const statusText = seg.isError
          ? "失败"
          : seg.isComplete
            ? "完成"
            : "运行中";
        return (
          <div
            key={`seg-monitor-${seg.monitorId || idx}`}
            className={`my-2 mx-3 rounded-lg border ${statusColor} overflow-hidden`}
          >
            <div className="flex items-center gap-2 px-3 py-2 border-b border-black/5">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className={isRunning ? "animate-pulse text-amber-600" : seg.isError ? "text-red-600" : "text-green-600"}
              >
                <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
                <line x1="8" y1="21" x2="16" y2="21" />
                <line x1="12" y1="17" x2="12" y2="21" />
              </svg>
              <span className="text-micro font-medium text-foreground/80 truncate">
                Monitor {seg.monitorCommand}
              </span>
              <span className={`text-nano ml-auto px-1.5 py-0.5 rounded ${seg.isError ? "bg-red-100 text-red-700" : seg.isComplete ? "bg-green-100 text-green-700" : "bg-amber-100 text-amber-700"}`}>
                {statusText}{seg.monitorExitCode !== null && seg.monitorExitCode !== undefined ? ` (${seg.monitorExitCode})` : ""}
              </span>
            </div>
            {seg.content && (
              <pre className="px-3 py-2 text-micro font-mono text-muted-foreground max-h-48 overflow-auto whitespace-pre-wrap break-all">
                {seg.content}
              </pre>
            )}
            {isRunning && (
              <div className="px-3 py-1.5 border-t border-black/5">
                <div className="flex items-center gap-1.5">
                  <div className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                  <span className="text-nano text-muted-foreground">后台运行中...</span>
                </div>
              </div>
            )}
          </div>
        );
      }

      if (seg.type === "turn") {
        return (
          <div
            key={`seg-turn-${idx}`}
            className="flex w-full items-center gap-3 my-4"
          >
            <div className="h-px flex-1 bg-border/70" />
            <span className="rounded-full bg-muted px-2.5 py-0.5 text-nano font-medium text-muted-foreground whitespace-nowrap">
              Turn {seg.turnN ?? "?"}
            </span>
            <div className="h-px flex-1 bg-border/70" />
          </div>
        );
      }

      return null;
    });
  }, [
    segments,
    isStreaming,
    onOpenWorkspaceArtifact,
    onOpenInBrowserTab,
    sessionId,
    onRetryLastSubmit,
    onViewToolDetails,
    taskId,
    showToolOutputs,
  ]);

  return (
    <AiMessageProvider state={state} actions={actions} meta={meta}>
      <div className="min-w-0 px-0.5 py-2">
        {/* 加载中占位符 */}
        {isEmpty && isStreaming && !isStopped && <LoadingPlaceholder />}

        {/* 终止状态提示 */}
        {isStopped && <StoppedIndicator />}

        {/* 纯文本内容（没有任何 segments 时渲染 content 作为后备，兼容旧数据） */}
        {content && (!segments || segments.length === 0) && (
          <div className="prose prose-sm max-w-none min-w-0 break-all">
            <ChartAwareMarkdown
              content={content}
              token={undefined}
              sessionId={sessionId}
              onOpenInMainCanvas={onOpenWorkspaceArtifact}
              onOpenInBrowserTab={onOpenInBrowserTab}
            />
          </div>
        )}

        {/* 按顺序渲染的内容 */}
        {renderSegments}

        {/* Worker 状态指示器 */}
        <WorkerIndicators />
      </div>
    </AiMessageProvider>
  );
});

export default AiMessageContent;
