/**
 * 非视觉模型发图警告的纯逻辑（设计文档：交互设计/model-capability-display.md）。
 *
 * 决策：警告但放行。用户可能就是想让模型读图床 URL，拦截会误伤；
 * 但必须显式告知「模型看不到图」，消除静默降级带来的错误前提。
 */

/** 图片文件名判定。2026-08-14 起白名单扩展：svg/bmp/avif 一并按图片处理。 */
export function isImageFilename(filename: string): boolean {
  return /\.(png|jpe?g|gif|webp|svg|bmp|avif)$/i.test(filename);
}

/**
 * 是否显示「当前模型不支持图片输入」警告。
 *
 * 只在**确知**模型不具备 image_in 时警告：supportsImageInput 为 undefined
 * （如选中 "system" 默认、有效模型未解析出来）时不警告——误报警告比漏报
 * 更伤信任，且与 step-code「误剥离是静默的、误发送是显式可自愈的」
 * 的默认取向一致。
 */
export function shouldWarnImageAttachment(opts: {
  supportsImageInput?: boolean;
  filenames: string[];
}): boolean {
  if (opts.supportsImageInput !== false) return false;
  return opts.filenames.some(isImageFilename);
}
