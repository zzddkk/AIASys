/**
 * IncrementalMarkdown - 流式场景的增量 Markdown 渲染
 *
 * 问题：ChartAwareMarkdown 对每个流式 delta 都全量重跑 react-markdown
 * （content 引用变化 → memo 失效 → remarkGfm 全量解析），长回复必卡。
 *
 * 方案（DeepSeek Harness StreamingRenderer 与 gemini-cli findLastSafeSplitPoint
 * 的同一思路）：把内容在安全边界切成块，每块用 memo 包裹，已定稿的前缀块
 * content 不变就不重解析，流式期只有尾部块反复解析。
 *
 * 安全边界规则（splitMarkdownBlocks）：
 * - 代码围栏（``` / ~~~）内不切；
 * - $$ 数学块内不切；
 * - 空行处切，但下一行是有序列表项（`1.` / `2)`）时不切——
 *   切开会让两个 <ol> 都从 1 重新编号，是肉眼可见的回归；
 * - 无序列表切开两个 <ul> 视觉等价，允许切。
 */
import { memo } from "react";
import type { Components } from "react-markdown";

import { MarkdownRenderer } from "./MarkdownRenderer";
import { MathMarkdownRenderer } from "./MathMarkdownRenderer";

const FENCE_PATTERN = /^(```|~~~)/;
const MATH_FENCE_PATTERN = /^\$\$/;
const ORDERED_LIST_ITEM_PATTERN = /^\s*\d+[.)]\s/;

/** 与 ChartAwareMarkdown 原 containsMathSyntax 同口径：含任何数学语法就走 KaTeX 渲染器 */
function containsMathSyntax(content: string): boolean {
  return (
    content.includes("$") ||
    content.includes("\\(") ||
    content.includes("\\[") ||
    content.includes("\\begin{") ||
    /(^|[^\\])\$(?!\s)([\s\S]*?)(?<!\s)\$/.test(content)
  );
}

export function splitMarkdownBlocks(content: string): string[] {
  const lines = content.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  let inFence = false;
  let inMath = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!inMath && FENCE_PATTERN.test(trimmed)) {
      inFence = !inFence;
    } else if (!inFence && MATH_FENCE_PATTERN.test(trimmed)) {
      inMath = !inMath;
    }

    if (!inFence && !inMath && trimmed === "") {
      const hasContent = current.some((l) => l.trim() !== "");
      const nextNonBlank = lines.slice(i + 1).find((l) => l.trim() !== "");
      const nextIsOrderedList = nextNonBlank
        ? ORDERED_LIST_ITEM_PATTERN.test(nextNonBlank)
        : false;
      if (hasContent && !nextIsOrderedList) {
        blocks.push(current.join("\n"));
        current = [];
        continue;
      }
    }
    current.push(line);
  }
  if (current.length > 0 && current.some((l) => l.trim() !== "")) {
    blocks.push(current.join("\n"));
  }
  return blocks;
}

interface MarkdownBlockProps {
  content: string;
  components?: Components;
}

/** 单块渲染：memo 生效的关键——content 不变就跳过一次 react-markdown 全量解析 */
const MarkdownBlock = memo(function MarkdownBlock({
  content,
  components,
}: MarkdownBlockProps) {
  const Renderer = containsMathSyntax(content)
    ? MathMarkdownRenderer
    : MarkdownRenderer;
  return (
    <div className="mb-4 last:mb-0">
      <Renderer content={content} components={components} />
    </div>
  );
});

interface IncrementalMarkdownProps {
  content: string;
  components?: Components;
}

export const IncrementalMarkdown = memo(function IncrementalMarkdown({
  content,
  components,
}: IncrementalMarkdownProps) {
  const blocks = splitMarkdownBlocks(content);
  return (
    <>
      {blocks.map((block, index) => (
        <MarkdownBlock
          key={`md-block-${index}`}
          content={block}
          components={components}
        />
      ))}
    </>
  );
});
