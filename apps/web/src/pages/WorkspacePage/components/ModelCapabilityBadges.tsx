/**
 * ModelCapabilityBadges - 模型能力小标签
 *
 * 在模型选择器中标注「图片 / 视频 / 思考」能力，让多模态支持在选模型时可见。
 * 设计决策（交互设计/model-capability-display.md）：文字标签不用图标，
 * 只显示正向能力，不显示「不支持」负向标签（避免标签墙）。
 */
import type { ModelCapability } from "@/lib/api/llm";

const CAPABILITY_LABELS: ReadonlyArray<{
  key: ModelCapability;
  label: string;
}> = [
  { key: "image_in", label: "图片" },
  { key: "video_in", label: "视频" },
  { key: "thinking", label: "思考" },
];

/** 从 capabilities 算出要显示的标签（always_thinking 并入「思考」，不单独显示） */
export function capabilityLabels(capabilities?: ModelCapability[]): string[] {
  if (!capabilities || capabilities.length === 0) return [];
  const labels: string[] = [];
  for (const { key, label } of CAPABILITY_LABELS) {
    if (capabilities.includes(key)) {
      labels.push(label);
    }
  }
  if (capabilities.includes("always_thinking") && !labels.includes("思考")) {
    labels.push("思考");
  }
  return labels;
}

export function ModelCapabilityBadges({
  capabilities,
}: {
  capabilities?: ModelCapability[];
}) {
  const labels = capabilityLabels(capabilities);
  if (labels.length === 0) return null;
  return (
    <span className="flex items-center gap-1 flex-shrink-0">
      {labels.map((label) => (
        <span
          key={label}
          className="rounded bg-muted px-1 py-0.5 text-nano leading-none text-muted-foreground"
        >
          {label}
        </span>
      ))}
    </span>
  );
}

export default ModelCapabilityBadges;
