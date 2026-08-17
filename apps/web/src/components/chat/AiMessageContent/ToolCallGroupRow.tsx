/**
 * ToolCallGroupRow - 连续同名工具调用的聚合分组行
 *
 * 借鉴 Kimi Code 的 ReadGroup / AgentGroup（streaming-ui.ts:794-908）：
 * 主控一次任务动辄十几次连续 read/grep，平铺渲染会把对话流刷成工具
 * 流水账，正文被淹没。聚合行收起时只显示「工具名 × N · 状态汇总」，
 * 展开后仍是逐条的 ToolCallRow，详情浮窗能力不丢。
 *
 * 分组是纯渲染层变换，不动 segments 数据本身。
 */
import { memo, useState } from "react";
import { ChevronDown, ChevronUp, Layers } from "lucide-react";

import type { ChatSegment } from "@/pages/WorkspacePage/types";

import { ToolCallRow } from "./ToolCallRow";

/** 连续同名 tool_call 达到这个数量才聚合，两条不值得折起来 */
export const TOOL_GROUP_MIN_SIZE = 3;

export type ToolCallGroup = {
  toolName: string;
  /** 组内的 tool_call 段（保持原顺序与原引用，展开时直接渲染） */
  calls: ChatSegment[];
};

/**
 * 把 segments 里「连续、同名、数量达标」的 tool_call 段收成组。
 * 返回与输入等长的渲染单元序列：未分组的段原样保留（unit 为 segment），
 * 分组的段替换为一个 group unit。tool_output 不参与分组——它和 tool_call
 * 交替出现时序列被打断，该组自然不成立。
 */
export type SegmentRenderUnit =
  | { kind: "segment"; segment: ChatSegment; index: number }
  | { kind: "group"; group: ToolCallGroup; index: number };

export function groupToolCallSegments(segments: ChatSegment[]): SegmentRenderUnit[] {
  const units: SegmentRenderUnit[] = [];
  let i = 0;
  while (i < segments.length) {
    const seg = segments[i];
    if (seg.type !== "tool_call" || !seg.toolName) {
      units.push({ kind: "segment", segment: seg, index: i });
      i += 1;
      continue;
    }
    let j = i;
    const run: ChatSegment[] = [];
    while (
      j < segments.length &&
      segments[j].type === "tool_call" &&
      segments[j].toolName === seg.toolName
    ) {
      run.push(segments[j]);
      j += 1;
    }
    if (run.length >= TOOL_GROUP_MIN_SIZE) {
      units.push({
        kind: "group",
        group: { toolName: seg.toolName, calls: run },
        index: i,
      });
    } else {
      run.forEach((item, offset) => {
        units.push({ kind: "segment", segment: item, index: i + offset });
      });
    }
    i = j;
  }
  return units;
}

interface ToolCallGroupRowProps {
  group: ToolCallGroup;
  /** 消息级流式状态，传给组内每行（最后一行可能还在 running） */
  isMessageStreaming?: boolean;
  onCallClick?: (seg: ChatSegment, groupIndex: number, rect: DOMRect) => void;
}

export const ToolCallGroupRow = memo(function ToolCallGroupRow({
  group,
  isMessageStreaming,
  onCallClick,
}: ToolCallGroupRowProps) {
  const [open, setOpen] = useState(false);
  const total = group.calls.length;
  const errorCount = group.calls.filter((c) => c.isError).length;
  const doneCount = group.calls.filter((c) => c.isComplete && !c.isError).length;
  const runningCount = total - errorCount - doneCount;

  const statusText =
    errorCount > 0
      ? `${doneCount} 完成 · ${errorCount} 失败`
      : runningCount > 0
        ? `${doneCount}/${total} 完成`
        : `${total} 全部完成`;

  return (
    <div className="my-1 rounded-md border border-border/60 bg-muted/20">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <Layers className="h-3.5 w-3.5 shrink-0" />
        <span className="font-medium">{group.toolName}</span>
        <span>× {total}</span>
        <span
          className={`ml-auto rounded px-1.5 py-0.5 text-nano ${
            errorCount > 0
              ? "bg-red-100 text-red-700"
              : runningCount > 0
                ? "bg-amber-100 text-amber-700"
                : "bg-green-100 text-green-700"
          }`}
        >
          {statusText}
        </span>
        {open ? (
          <ChevronUp className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        )}
      </button>
      {open && (
        <div className="border-t border-border/50 px-1.5 py-1">
          {group.calls.map((seg, idx) => (
            <ToolCallRow
              key={seg.toolCallId || `group-call-${idx}`}
              toolName={seg.toolName}
              toolParams={seg.toolParams}
              isComplete={seg.isComplete}
              isError={seg.isError}
              isMessageStreaming={isMessageStreaming}
              onClick={
                onCallClick ? (rect) => onCallClick(seg, idx, rect) : undefined
              }
            />
          ))}
        </div>
      )}
    </div>
  );
});
