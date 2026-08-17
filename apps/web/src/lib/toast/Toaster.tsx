/**
 * 全局 Toaster：订阅 toast store 并渲染四型提示。
 *
 * 挂在应用根部一次即可（WorkspacePage / App）。命令式调用：
 *   import { toast } from "@/lib/toast";
 *   toast.success("已保存"); toast.error("保存失败");
 */

import { useEffect, useState } from "react";
import { CheckCircle2, XCircle, Info, AlertTriangle, X } from "lucide-react";
import { toast, type ToastItem, type ToastVariant } from "@/lib/toast/store";
import { cn } from "@/lib/utils";

const VARIANT_STYLES: Record<ToastVariant, { ring: string; icon: typeof Info }> = {
  success: { ring: "border-emerald-500/40 bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100", icon: CheckCircle2 },
  error: { ring: "border-red-500/40 bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100", icon: XCircle },
  info: { ring: "border-sky-500/40 bg-sky-50 text-sky-900 dark:bg-sky-950 dark:text-sky-100", icon: Info },
  warning: { ring: "border-amber-500/40 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100", icon: AlertTriangle },
};

function ToastRow({ item }: { item: ToastItem }) {
  const styles = VARIANT_STYLES[item.variant];
  const Icon = styles.icon;
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    if (item.duration <= 0) return;
    const timer = setTimeout(() => setLeaving(true), item.duration);
    const remove = setTimeout(() => toast.dismiss(item.id), item.duration + 200);
    return () => {
      clearTimeout(timer);
      clearTimeout(remove);
    };
  }, [item.id, item.duration]);

  return (
    <div
      role={item.variant === "error" ? "alert" : "status"}
      className={cn(
        "pointer-events-auto flex items-start gap-2.5 rounded-lg border px-4 py-3 shadow-lg backdrop-blur transition-all duration-200",
        "min-w-[260px] max-w-[420px] text-sm",
        styles.ring,
        leaving ? "translate-x-4 opacity-0" : "translate-x-0 opacity-100",
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="flex-1 break-words leading-snug">{item.message}</span>
      <button
        type="button"
        onClick={() => toast.dismiss(item.id)}
        className="shrink-0 rounded p-0.5 opacity-60 transition-opacity hover:opacity-100"
        aria-label="关闭"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => toast.subscribe(setItems), []);

  if (items.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col gap-2">
      {items.map((item) => (
        <ToastRow key={item.id} item={item} />
      ))}
    </div>
  );
}
