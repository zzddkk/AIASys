/**
 * 全局 Toast store（订阅式，零依赖）。
 *
 * 为什么不用现成库：现有 FileUploadToast 是页面级 useState，各组件各自维护，
 * 非组件代码（api 层、事件 handler）无法触发；全站还散落 20+ 个阻塞式 alert()。
 * 这里用极简订阅 store 统一四型（success/error/info/warning），组件外也能调。
 *
 * 设计参考：sonner 的命令式 API（toast.error(msg)），但不引入新依赖。
 */

export type ToastVariant = "success" | "error" | "info" | "warning";

export interface ToastItem {
  id: string;
  message: string;
  variant: ToastVariant;
  /** 毫秒；0 表示不自动关闭（用于需要用户确认的错误） */
  duration: number;
}

export type ToastListener = (toasts: ToastItem[]) => void;

const DEFAULT_DURATION: Record<ToastVariant, number> = {
  success: 3000,
  info: 3000,
  warning: 4000,
  error: 5000,
};

let toasts: ToastItem[] = [];
const listeners = new Set<ToastListener>();
let idCounter = 0;

function emit(): void {
  for (const listener of listeners) {
    listener(toasts);
  }
}

function remove(id: string): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export interface ToastOptions {
  duration?: number;
  /** 不自动关闭（错误默认 5s，需要用户看清的传这个） */
  persistent?: boolean;
}

function push(message: string, variant: ToastVariant, options?: ToastOptions): string {
  const id = `toast-${Date.now()}-${idCounter++}`;
  const duration = options?.persistent
    ? 0
    : options?.duration ?? DEFAULT_DURATION[variant];
  toasts = [...toasts, { id, message, variant, duration }];
  emit();
  return id;
}

export const toast = {
  success(message: string, options?: ToastOptions): string {
    return push(message, "success", options);
  },
  error(message: string, options?: ToastOptions): string {
    return push(message, "error", options);
  },
  info(message: string, options?: ToastOptions): string {
    return push(message, "info", options);
  },
  warning(message: string, options?: ToastOptions): string {
    return push(message, "warning", options);
  },
  dismiss(id: string): void {
    remove(id);
  },
  clear(): void {
    toasts = [];
    emit();
  },
  /** 仅供 Toaster 组件订阅；测试也用它拿快照。 */
  subscribe(listener: ToastListener): () => void {
    listeners.add(listener);
    listener(toasts);
    return () => {
      listeners.delete(listener);
    };
  },
  /** 测试辅助：读取当前快照。 */
  _snapshot(): ToastItem[] {
    return toasts;
  },
  /** 测试辅助：清空 store。 */
  _reset(): void {
    toasts = [];
    listeners.clear();
  },
};
