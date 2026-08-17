/**
 * MessageTimestamp - 消息时间戳组件
 */
import { useChatAreaContext } from "./context";

export function MessageTimestamp() {
  const {
    state: { item, isUser },
    meta: { layout = "default" },
  } = useChatAreaContext();

  const date = item.timestamp ? new Date(item.timestamp) : null;
  const timeString =
    date && !Number.isNaN(date.getTime())
      ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "";

  return (
    <span
      className={`mt-1 text-muted-foreground ${
        layout === "compact" ? "px-0.5 text-nano" : "px-1 text-nano"
      } ${isUser ? "self-end" : "self-start"}`}
    >
      {timeString}
    </span>
  );
}
