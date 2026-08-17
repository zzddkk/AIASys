/**
 * ToolCallRow - 工具调用行
 *
 * 替代原先「图标 + 工具名 + 点击查看详情」的通用按钮。借鉴来源：
 * - step-code ToolCall.tsx 的 summarizeInput（按 path/pattern/command/skill
 *   优先级提取参数摘要，80 字符截断）；
 * - deepseek-harness ToolRow 的状态机（running/ok/error 三态视觉，
 *   running 加动效、error 红色标出）。
 *
 * 点击仍然打开详情浮窗（完整参数与结果在浮窗里看），行内只负责
 * 「哪个工具、对什么对象、现在什么状态」三件事。
 */
import { memo } from "react";
import { AlertCircle, Loader2, Wrench } from "lucide-react";

export type ToolCallStatus = "running" | "ok" | "error";

/** 参数摘要的字段优先级，对齐 step-code summarizeInput 的取舍 */
const SUMMARY_KEYS = [
  "path",
  "file_path",
  "filePath",
  "pattern",
  "command",
  "query",
  "url",
  "skill",
  "name",
] as const;

const MAX_SUMMARY_LENGTH = 80;

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/**
 * 从 toolParams（JSON 字符串）提取一行参数摘要。
 * 解析失败、没有可摘要字段时返回 undefined，调用方回退到占位文案。
 */
export function summarizeToolParams(toolParams?: string): string | undefined {
  if (!toolParams) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(toolParams);
  } catch {
    return undefined;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return undefined;
  }
  const record = parsed as Record<string, unknown>;
  for (const key of SUMMARY_KEYS) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return truncate(value.trim(), MAX_SUMMARY_LENGTH);
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return undefined;
}

interface ToolCallRowProps {
  toolName?: string;
  toolParams?: string;
  isComplete?: boolean;
  isError?: boolean;
  /** 消息级流式状态：只有消息还在流式且本调用未完成时才算 running */
  isMessageStreaming?: boolean;
  onClick?: (triggerRect: DOMRect) => void;
}

export const ToolCallRow = memo(function ToolCallRow({
  toolName,
  toolParams,
  isComplete,
  isError,
  isMessageStreaming,
  onClick,
}: ToolCallRowProps) {
  const status: ToolCallStatus = isError
    ? "error"
    : isMessageStreaming && !isComplete
      ? "running"
      : "ok";
  const summary = summarizeToolParams(toolParams);

  return (
    <button
      type="button"
      onClick={(e) => {
        const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
        onClick?.(rect);
      }}
      className={`mb-2 group/tool flex w-full items-center gap-3 px-3.5 py-2.5 rounded-lg border transition-all duration-200 hover:shadow-sm ${
        status === "error"
          ? "border-red-200 bg-red-50/50 hover:bg-red-50"
          : "border-border bg-muted/50 hover:bg-accent/70"
      }`}
    >
      <div
        className={`flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0 ${
          status === "error"
            ? "bg-red-100 text-red-600"
            : status === "running"
              ? "bg-primary/15 text-primary"
              : "bg-primary/10 text-primary"
        }`}
      >
        {status === "running" ? (
          <Loader2 size={13} className="animate-spin" />
        ) : status === "error" ? (
          <AlertCircle size={13} />
        ) : (
          <Wrench size={13} />
        )}
      </div>
      <div className="flex flex-col items-start gap-0.5 min-w-0 flex-1">
        <span className="text-xs font-semibold text-foreground truncate max-w-full">
          {toolName ?? "工具调用"}
        </span>
        <span
          className={`text-nano truncate max-w-full transition-colors ${
            status === "error"
              ? "text-red-600"
              : "text-muted-foreground group-hover/tool:text-foreground/70"
          }`}
        >
          {summary ?? "点击查看详情 →"}
        </span>
      </div>
      {status === "error" && (
        <span className="ml-auto flex-shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-nano font-medium text-red-700">
          失败
        </span>
      )}
      {status === "running" && (
        <span className="ml-auto flex-shrink-0 text-nano text-primary">运行中</span>
      )}
    </button>
  );
});

export default ToolCallRow;
