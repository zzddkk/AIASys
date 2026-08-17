/**
 * 长文本粘贴转附件的判定（借鉴 opencode 的粘贴折叠 [Pasted ~N lines]）。
 *
 * 语义差异说明：opencode 把长粘贴折叠成输入框内的占位文本（内容仍属 prompt），
 * 我们走「粘贴上传」心智（与既有的粘贴文件上传一致），转成 .txt 附件，
  * 不淹没输入框，模型照样能读到内容。
 *
 * 阈值刻意比 opencode（>150 字符或 ≥3 行）高：转成附件会改变内容落点，
 * 只有真·长文本才值得改变用户预期。
 */

export const PASTE_AS_ATTACHMENT_MIN_CHARS = 1000;
export const PASTE_AS_ATTACHMENT_MIN_LINES = 10;

export function shouldPasteAsAttachment(text: string): boolean {
  if (!text) return false;
  const lineCount = text.split("\n").length;
  return (
    text.length >= PASTE_AS_ATTACHMENT_MIN_CHARS ||
    lineCount >= PASTE_AS_ATTACHMENT_MIN_LINES
  );
}

/** 生成粘贴附件的文件名：粘贴文本-20260815-192530.txt */
export function buildPastedTextFilename(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `粘贴文本-${stamp}.txt`;
}
