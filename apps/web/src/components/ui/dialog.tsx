"use client";

import * as React from "react";
import { X } from "lucide-react";
import { Dialog as BaseDialogNamespace } from "@base-ui/react/dialog";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const BaseDialogRoot = BaseDialogNamespace.Root;
const BaseDialogTrigger = BaseDialogNamespace.Trigger;
const BaseDialogClose = BaseDialogNamespace.Close;
const BaseDialogPortal = BaseDialogNamespace.Portal;

const DialogPortal = BaseDialogPortal;
const BaseDialogBackdrop = BaseDialogNamespace.Backdrop;
const BaseDialogPopup = BaseDialogNamespace.Popup;
const BaseDialogTitle = BaseDialogNamespace.Title;
const BaseDialogDescription = BaseDialogNamespace.Description;

function Dialog({
  ...props
}: React.ComponentProps<typeof BaseDialogRoot>) {
  return <BaseDialogRoot data-slot="dialog" {...props} />;
}

function DialogTrigger({
  asChild,
  children,
  ...props
}: React.ComponentProps<typeof BaseDialogTrigger> & { asChild?: boolean }) {
  if (asChild) {
    const child = React.isValidElement(children)
      ? children
      : React.Children.only(children);
    if (React.isValidElement(child)) {
      return (
        <BaseDialogTrigger
          data-slot="dialog-trigger"
          {...props}
          render={(triggerProps) =>
            React.cloneElement(child as React.ReactElement, triggerProps)
          }
        />
      );
    }
  }
  return (
    <BaseDialogTrigger data-slot="dialog-trigger" {...props}>
      {children}
    </BaseDialogTrigger>
  );
}

function DialogClose({
  asChild,
  children,
  ...props
}: React.ComponentProps<typeof BaseDialogClose> & { asChild?: boolean }) {
  if (asChild) {
    const child = React.isValidElement(children)
      ? children
      : React.Children.only(children);
    if (React.isValidElement(child)) {
      return (
        <BaseDialogClose
          data-slot="dialog-close"
          {...props}
          render={(closeProps) =>
            React.cloneElement(child as React.ReactElement, closeProps)
          }
        />
      );
    }
  }
  return (
    <BaseDialogClose data-slot="dialog-close" {...props}>
      {children}
    </BaseDialogClose>
  );
}

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof BaseDialogBackdrop>,
  React.ComponentPropsWithoutRef<typeof BaseDialogBackdrop>
>(({ className, ...props }, ref) => (
  <BaseDialogBackdrop
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-black/80 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = BaseDialogBackdrop.displayName;

/**
 * 尺寸档位。此前 DialogContent 只有硬编码的 max-w-lg，导致 44 处调用各自覆写，
 * 实测长出 max-w-sm/md/lg/xl/2xl/3xl/4xl/5xl/6xl 与 [600px]/[620px]/[680px]/
 * [720px]/[1100px]/[1280px]/[1320px]/[1480px] 共十七种宽度，
 * 配 80/85/86/88/90/92vh 六种高度。
 *
 * 宽度与「是否定高」是两个正交维度，不能压成一维档位：实测有 6 处需要
 * 「中等宽度 + 定高」（能力面板、外部市场面板）。若把定高绑进 lg/xl 档，
 * 这些地方就只能回到 className 里手写 h-[90vh]，等于绕开档位体系。
 * 所以 size 只管宽度，tall 管高度与内部布局模式。
 */
const dialogContentVariants = cva(
  "fixed left-[50%] top-[50%] z-50 w-full translate-x-[-50%] translate-y-[-50%] border bg-card shadow-pop-4 duration-200 ease-standard data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%] data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%] sm:rounded-2xl",
  {
    variants: {
      /** 宽度档位。数值取自现有 44 处调用的实际分布，不是照抄别处。 */
      size: {
        /**
         * 384px 单句确认框。这一档不能并进 sm：384 → 512 是 +33%，
         * 一句话的确认框撑到 512 会显得空，像内容没加载完。实测 3 处在用。
         */
        xs: "max-w-sm",
        /** 512px 表单、单字段编辑 */
        sm: "max-w-lg",
        /** 672px 详情、中等列表、多字段表单 */
        md: "max-w-2xl",
        /** 1024px 设置面板、左导航 + 右内容 */
        lg: "max-w-5xl",
        /** 1152px 市场、双栏浏览 */
        xl: "max-w-6xl",
        /**
         * 工作区级沉浸式面板（知识库工作台、数据库资源、会话画布）。
         * 用 min(px, vw) 而不是纯 max-w：这类面板在 2K 屏要铺开，在 13 寸
         * 笔电上又必须留边距，单一 max-w 两头照顾不到。实测 4 处各自手写
         * 1280/1320/1480 配 94/96vw，收敛到这一处。
         */
        full: "max-w-[min(1440px,94vw)]",
      },
      /**
       * 是否定高。
       * false（默认）= 内容决定高度、上限 90vh，内部 grid + p-6 常规排版；
       * true = 固定 88vh + flex 列布局 + 零内边距，内部自己排
       *        （这类面板通常是 header / 滚动区 / footer 三段结构）。
       */
      tall: {
        false: "grid max-h-[90vh] gap-4 p-6",
        true: "flex h-[88vh] flex-col gap-0 p-0",
      },
    },
    defaultVariants: { size: "sm", tall: false },
  },
);

const DialogContent = React.forwardRef<
  React.ElementRef<typeof BaseDialogPopup>,
  React.ComponentPropsWithoutRef<typeof BaseDialogPopup> &
    VariantProps<typeof dialogContentVariants> & {
      onEscapeKeyDown?: (event: KeyboardEvent) => void;
      onPointerDownOutside?: (event: PointerEvent) => void;
      onOpenAutoFocus?: (event: Event) => void;
    }
>(({ className, children, size, tall, onEscapeKeyDown, onPointerDownOutside, onOpenAutoFocus, ...props }, ref) => {
  const internalRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!onEscapeKeyDown && !onPointerDownOutside) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && onEscapeKeyDown) {
        onEscapeKeyDown(event);
      }
    };

    const handlePointerDownOutside = (event: PointerEvent) => {
      if (onPointerDownOutside) {
        onPointerDownOutside(event);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDownOutside);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDownOutside);
    };
  }, [onEscapeKeyDown, onPointerDownOutside]);

  React.useEffect(() => {
    if (!onOpenAutoFocus) return;

    const handleFocus = (event: Event) => {
      onOpenAutoFocus(event);
    };

    const popup = internalRef.current;
    if (popup) {
      popup.addEventListener("focusin", handleFocus as EventListener);
      return () => {
        popup.removeEventListener("focusin", handleFocus as EventListener);
      };
    }
  }, [onOpenAutoFocus]);

  return (
    <BaseDialogPortal>
      <DialogOverlay />
      <BaseDialogPopup
        ref={(node) => {
          internalRef.current = node;
          if (typeof ref === "function") {
            ref(node);
          } else if (ref) {
            ref.current = node;
          }
        }}
        className={cn(
          dialogContentVariants({ size, tall }),
          className,
        )}
        {...props}
      >
        {children}
        <DialogClose className="absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none data-[state=open]:bg-accent data-[state=open]:text-muted-foreground">
          <X className="h-4 w-4" />
          <span className="sr-only">Close</span>
        </DialogClose>
      </BaseDialogPopup>
    </BaseDialogPortal>
  );
});
DialogContent.displayName = BaseDialogPopup.displayName;

const DialogHeader = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col space-y-1.5 text-center sm:text-left",
      className,
    )}
    {...props}
  />
);
DialogHeader.displayName = "DialogHeader";

const DialogFooter = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col-reverse sm:flex-row sm:justify-end sm:space-x-2",
      className,
    )}
    {...props}
  />
);
DialogFooter.displayName = "DialogFooter";

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof BaseDialogTitle>,
  React.ComponentPropsWithoutRef<typeof BaseDialogTitle>
>(({ className, ...props }, ref) => (
  <BaseDialogTitle
    ref={ref}
    className={cn(
      "text-lg font-semibold leading-none tracking-tight",
      className,
    )}
    {...props}
  />
));
DialogTitle.displayName = "DialogTitle";

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof BaseDialogDescription>,
  React.ComponentPropsWithoutRef<typeof BaseDialogDescription>
>(({ className, ...props }, ref) => (
  <BaseDialogDescription
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
));
DialogDescription.displayName = "DialogDescription";

export {
  Dialog,
  DialogTrigger,
  DialogClose,
  DialogPortal,
  DialogOverlay,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
};
