"use strict";

/**
 * vendor 二进制下载脚本的共用纯函数。
 *
 * 抽出来的动机是两个实测出来的缺陷（2026-08-10）：
 *
 * 1. 原来三个 download-*.cjs 各自复制了一份 curlDownload，里面把「spawnSync 返回非 0」
 *    一律打印成「curl 不可用，尝试 wget...」。而 curl -f 遇到 404 的退出码是 22，
 *    工具本身好得很。实测：node spawnSync curl --version 返回 curl 8.14.1，
 *    但脚本照样报「curl 不可用」。这条误导性诊断把下面第 2 个真缺陷盖了三个月。
 *
 * 2. sqlite-vec 的 release 不提供 per-asset 的 `<asset>.sha256`（实测 HTTP 404），
 *    它提供的是一个汇总文件 checksums.txt。脚本一直在请求不存在的文件名，
 *    于是每次都「跳过校验」——校验形同虚设，但日志看起来只是网络不好。
 *
 * checksums.txt 的行格式有顺序陷阱，见 parseChecksumsTxt 的注释。
 */

const SHA256_HEX = /^[0-9a-f]{64}$/i;

/**
 * 判断 spawnSync 的失败到底是「命令不存在」还是「命令跑了但没成功」。
 *
 * 这两件事必须分开报，因为它们对应完全不同的处置：前者要装工具或换实现，
 * 后者要查 URL / 网络 / 权限。混为一谈会让人去修根本没坏的东西。
 *
 * @param {{status: number|null, error?: {code?: string, message?: string}, stderr?: string}} result
 * @returns {{kind: "missing"|"failed", detail: string}}
 */
function describeSpawnFailure(result) {
  if (!result) return { kind: "failed", detail: "no result" };

  const err = result.error;
  // ENOENT = 可执行文件根本不在 PATH 上，这才是「不可用」。
  // EACCES 同样属于「拿不到这个命令」，归入 missing。
  if (err && (err.code === "ENOENT" || err.code === "EACCES")) {
    return { kind: "missing", detail: String(err.code) };
  }
  if (err) {
    return { kind: "failed", detail: String(err.code || err.message) };
  }

  const stderr = (result.stderr || "").trim();
  const tail = stderr ? `: ${stderr.split("\n").slice(-1)[0].trim()}` : "";
  return { kind: "failed", detail: `exit ${result.status}${tail}` };
}

/**
 * 从汇总校验文件里取出某个 asset 的 sha256。
 *
 * 顺序陷阱：GNU sha256sum 的格式是 `<hash>  <filename>`，而 sqlite-vec 的
 * checksums.txt 是反的，`<filename> <hash>`（实测 v0.1.6）。写死任一顺序都会
 * 在另一种格式上静默返回 undefined，然后被上层当成「上游没提供校验」而跳过。
 * 所以这里按「哪一列长得像 64 位 hex」来判定，不依赖列序。
 *
 * @param {string} content 校验文件全文
 * @param {string} assetName 目标文件名
 * @returns {string|null} 小写 sha256，找不到返回 null
 */
function parseChecksumsTxt(content, assetName) {
  if (typeof content !== "string" || !assetName) return null;

  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const cols = line.split(/\s+/).filter(Boolean);
    if (cols.length < 2) continue;

    const hashCol = cols.find((c) => SHA256_HEX.test(c));
    if (!hashCol) continue;

    // 文件名列 = 除哈希列以外的部分。sha256sum 会给二进制模式加 `*` 前缀。
    const nameCols = cols.filter((c) => c !== hashCol).map((c) => c.replace(/^\*/, ""));
    const matched = nameCols.some((c) => c === assetName || c.endsWith(`/${assetName}`));
    if (matched) return hashCol.toLowerCase();
  }
  return null;
}

/**
 * 单文件 sha256 校验文件（`<hash>` 或 `<hash>  <filename>`）取哈希。
 * 同样不假定列序，避免与 parseChecksumsTxt 出现两套判定标准。
 *
 * @param {string} content
 * @returns {string|null}
 */
function parseSingleSha256(content) {
  if (typeof content !== "string") return null;
  for (const rawLine of content.split(/\r?\n/)) {
    const cols = rawLine.trim().split(/\s+/).filter(Boolean);
    const hashCol = cols.find((c) => SHA256_HEX.test(c));
    if (hashCol) return hashCol.toLowerCase();
  }
  return null;
}

module.exports = {
  SHA256_HEX,
  describeSpawnFailure,
  parseChecksumsTxt,
  parseSingleSha256,
};
