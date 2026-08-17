"use client";

import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const Select = SelectPrimitive.Root;
const SelectGroup = SelectPrimitive.Group;
const SelectValue = SelectPrimitive.Value;

/**
 * 尺寸档位与 Button / Input 共用同一套 control-* token。
 *
 * 默认 md(36px) 而不是 shadcn 原本的 h-10(40px)：Button 默认是 h-9(36px)，
 * Select 默认 40px 比同排 Button 高 4px，不写 size 时天然错位——这是全站
 * SelectTrigger 有 4 处专门覆写高度（h-7/h-8/h-9）的直接原因。
 * 默认值的职责是「不写 size 时三种控件天然对齐」，紧凑区显式写 size="sm"/"xs"。
 */
const selectTriggerVariants = cva(
  "flex w-full min-w-0 items-center justify-between overflow-hidden rounded-md border border-input bg-background ring-offset-background transition-[color,box-shadow] duration-100 ease-standard placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 [&>span]:min-w-0 [&>span]:truncate",
  {
    variants: {
      size: {
        xs: "h-control-xs px-2 text-micro",
        sm: "h-control-sm px-2.5 text-caption",
        md: "h-control-md px-3 text-sm",
        lg: "h-control-lg px-3 text-sm",
      },
    },
    defaultVariants: { size: "md" },
  },
);

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger> &
    VariantProps<typeof selectTriggerVariants>
>(({ className, children, size, ...props }, ref) => (
  <SelectPrimitive.Trigger
    ref={ref}
    className={cn(selectTriggerVariants({ size }), className)}
    {...props}
  >
    {children}
    <SelectPrimitive.Icon className="ml-2 size-4 shrink-0 opacity-50" />
  </SelectPrimitive.Trigger>
));
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Label
    ref={ref}
    className={cn("px-2 py-1.5 text-xs font-medium text-muted-foreground", className)}
    {...props}
  />
));
SelectLabel.displayName = SelectPrimitive.Label.displayName;

const selectContentVariants = cva(
  "relative z-50 max-h-[--radix-select-content-available-height] min-w-[8rem] overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-pop-3 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
  {
    variants: {
      /** 档位在 Content 上设一次，由 SelectItem 继承——Radix 的 Item 拿不到 Trigger 的 size。 */
      size: {
        xs: "text-micro [&_[role=option]]:py-1",
        sm: "text-caption [&_[role=option]]:py-1.5",
        md: "text-sm [&_[role=option]]:py-1.5",
        lg: "text-sm [&_[role=option]]:py-2",
      },
    },
    defaultVariants: { size: "md" },
  },
);

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content> &
    VariantProps<typeof selectContentVariants>
>(({ className, children, size, position = "popper", ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      className={cn(
        selectContentVariants({ size }),
        position === "popper" &&
          "data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1",
        className,
      )}
      position={position}
      {...props}
    >
      <SelectPrimitive.Viewport
        className={cn(
          "p-1",
          position === "popper" &&
            "h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)]",
        )}
      >
        {children}
      </SelectPrimitive.Viewport>
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
));
SelectContent.displayName = SelectPrimitive.Content.displayName;

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      // 不写 text-*：字号由 SelectContent 的档位下传，避免子项与触发器脱节。
      // 选中态靠左侧 check 标记表达，不额外填背景（背景色留给 focus 态，
      // 两者都用填充会导致「选中项」和「键盘游标位置」视觉上分不清）。
      "relative flex w-full min-w-0 cursor-pointer select-none items-center overflow-hidden rounded-sm pl-8 pr-2 outline-none focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
      className,
    )}
    {...props}
  >
    <span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
      <SelectPrimitive.ItemIndicator>
        <svg
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M5 13l4 4L19 7"
          />
        </svg>
      </SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText className="min-w-0 flex-1 overflow-hidden">
      {children}
    </SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

export {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectGroup,
  SelectLabel,
  SelectValue,
};
