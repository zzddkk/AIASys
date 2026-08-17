"use strict";

/**
 * vendor 下载脚本的回归测试。
 *
 * 这组测试针对 2026-08-10 实测出来的三个问题，每条都对应一个真实发生过的失效：
 *
 * 1. 「curl 不可用」误诊：curl -f 遇 404 退出码 22，旧代码把它报成工具不可用，
 *    于是 sqlite-vec 校验缺失被当成网络问题放过。→ describeSpawnFailure 分类测试
 * 2. checksums.txt 的列序与 sha256sum 相反（`<file> <hash>` vs `<hash>  <file>`），
 *    写死列序会静默返回空值，再次退化成「跳过校验」。→ parseChecksumsTxt 双格式测试
 * 3. 我在手改解压逻辑时留下多余的 `}`，脚本语法错误，靠人眼才发现——
 *    scripts/ 目录当时没有任何自动化门。→ 语法门测试
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  describeSpawnFailure,
  parseChecksumsTxt,
  parseSingleSha256,
} = require("../../scripts/lib/vendor-download-utils.cjs");

const SCRIPTS_DIR = path.resolve(__dirname, "..", "..", "scripts");

// ---------------------------------------------------------------------------
// 1. describeSpawnFailure：命令不存在 vs 命令跑了但失败
// ---------------------------------------------------------------------------

test("describeSpawnFailure: ENOENT 归类为 missing（命令真的不在 PATH）", () => {
  const r = describeSpawnFailure({ status: null, error: { code: "ENOENT" } });
  assert.strictEqual(r.kind, "missing");
  assert.match(r.detail, /ENOENT/);
});

test("describeSpawnFailure: EACCES 也归类为 missing（拿不到这个命令）", () => {
  assert.strictEqual(
    describeSpawnFailure({ status: null, error: { code: "EACCES" } }).kind,
    "missing"
  );
});

test("describeSpawnFailure: curl -f 的 404（exit 22）不能报成 missing", () => {
  // 这是本次缺陷的核心：exit 22 意味着 curl 正常工作、服务器返回 4xx。
  // 若这条挂了，说明误诊逻辑回来了，sqlite-vec 那类「校验静默失效」会再次被掩盖。
  const r = describeSpawnFailure({
    status: 22,
    stderr: "curl: (22) The requested URL returned error: 404",
  });
  assert.strictEqual(r.kind, "failed");
  assert.match(r.detail, /exit 22/);
  assert.match(r.detail, /404/);
});

test("describeSpawnFailure: 其他 error.code（如 ETIMEDOUT）归类为 failed", () => {
  const r = describeSpawnFailure({ status: null, error: { code: "ETIMEDOUT" } });
  assert.strictEqual(r.kind, "failed");
  assert.match(r.detail, /ETIMEDOUT/);
});

test("describeSpawnFailure: 只取 stderr 末行，避免把整段日志塞进错误消息", () => {
  const r = describeSpawnFailure({
    status: 1,
    stderr: "line one\nline two\nfinal cause here",
  });
  assert.match(r.detail, /final cause here/);
  assert.ok(!r.detail.includes("line one"), "不应包含首行噪声");
});

test("describeSpawnFailure: 空入参不抛异常", () => {
  assert.strictEqual(describeSpawnFailure(null).kind, "failed");
  assert.strictEqual(describeSpawnFailure(undefined).kind, "failed");
});

// ---------------------------------------------------------------------------
// 2. parseChecksumsTxt：列序不能写死
// ---------------------------------------------------------------------------

const HASH_A = "9576fd156a0bd9caa009f9bf8c15e69e69b1246c86607e5232ceafce1951f934";
const HASH_B = "3fe911095630a401d906facaed1dbec026f0abaec0d9807f607ee73f38191e7f";

test("parseChecksumsTxt: sqlite-vec 真实格式（文件名在前）", () => {
  // 取自 https://github.com/asg017/sqlite-vec/releases/download/v0.1.6/checksums.txt
  const content = [
    `sqlpkg.json 67a36efda8a74ce1ded852caaf6b8b057895b210ec681d11633378013c7baf90`,
    `sqlite-vec-0.1.6-loadable-android-aarch64.tar.gz ${HASH_A}`,
    `sqlite-vec-0.1.6-loadable-ios-aarch64.tar.gz ${HASH_B}`,
  ].join("\n");

  assert.strictEqual(
    parseChecksumsTxt(content, "sqlite-vec-0.1.6-loadable-android-aarch64.tar.gz"),
    HASH_A
  );
});

test("parseChecksumsTxt: GNU sha256sum 格式（哈希在前）同样能解析", () => {
  // 列序判定必须靠「哪一列是 64 位 hex」，不能靠位置，否则换一种上游格式就静默失效。
  const content = `${HASH_A}  sqlite-vec-0.1.6-loadable-windows-x86_64.tar.gz\n`;
  assert.strictEqual(
    parseChecksumsTxt(content, "sqlite-vec-0.1.6-loadable-windows-x86_64.tar.gz"),
    HASH_A
  );
});

test("parseChecksumsTxt: 二进制模式的 * 前缀不影响匹配", () => {
  const content = `${HASH_B} *my-asset.tar.gz\n`;
  assert.strictEqual(parseChecksumsTxt(content, "my-asset.tar.gz"), HASH_B);
});

test("parseChecksumsTxt: 目标 asset 不在文件里时返回 null（而非返回别人的哈希）", () => {
  // 返回错哈希比返回 null 危险得多：校验会以「失败」告终，让人去查下载损坏。
  const content = `other-asset.tar.gz ${HASH_A}\n`;
  assert.strictEqual(parseChecksumsTxt(content, "wanted-asset.tar.gz"), null);
});

test("parseChecksumsTxt: 前缀相同但不同名的 asset 不能误匹配", () => {
  const content = [
    `sqlite-vec-0.1.6-loadable-windows-x86_64.tar.gz ${HASH_A}`,
    `sqlite-vec-0.1.6-loadable-windows-x86_64.tar.gz.sha256 ${HASH_B}`,
  ].join("\n");
  assert.strictEqual(
    parseChecksumsTxt(content, "sqlite-vec-0.1.6-loadable-windows-x86_64.tar.gz"),
    HASH_A
  );
});

test("parseChecksumsTxt: 路径前缀（./dist/x.tar.gz）能按尾部匹配", () => {
  const content = `${HASH_A}  ./dist/x.tar.gz\n`;
  assert.strictEqual(parseChecksumsTxt(content, "x.tar.gz"), HASH_A);
});

test("parseChecksumsTxt: 注释行、空行、CRLF 都不影响解析", () => {
  const content = `# checksums for release\r\n\r\nmy.tar.gz ${HASH_A}\r\n`;
  assert.strictEqual(parseChecksumsTxt(content, "my.tar.gz"), HASH_A);
});

test("parseChecksumsTxt: 非法入参返回 null 而不是抛异常", () => {
  assert.strictEqual(parseChecksumsTxt(null, "a"), null);
  assert.strictEqual(parseChecksumsTxt("a b", ""), null);
  assert.strictEqual(parseChecksumsTxt("garbage-without-hash line", "a"), null);
});

test("parseChecksumsTxt: 结果统一小写（大写哈希也能与 crypto 输出比较）", () => {
  const content = `my.tar.gz ${HASH_A.toUpperCase()}\n`;
  assert.strictEqual(parseChecksumsTxt(content, "my.tar.gz"), HASH_A);
});

// ---------------------------------------------------------------------------
// 3. parseSingleSha256：单文件校验文件
// ---------------------------------------------------------------------------

test("parseSingleSha256: 裸哈希", () => {
  assert.strictEqual(parseSingleSha256(`${HASH_A}\n`), HASH_A);
});

test("parseSingleSha256: `<hash>  <file>` 与 `<file> <hash>` 都能取到", () => {
  assert.strictEqual(parseSingleSha256(`${HASH_A}  uv.zip\n`), HASH_A);
  assert.strictEqual(parseSingleSha256(`uv.zip ${HASH_A}\n`), HASH_A);
});

test("parseSingleSha256: 拿到的是 HTML 错误页时返回 null，不返回垃圾字符串", () => {
  // 镜像站返回 200 + HTML 是常见情况；若这里返回首个 token，
  // 后续校验会报「哈希不匹配」，把人引向「下载损坏」的错误结论。
  assert.strictEqual(parseSingleSha256("<html><body>404 Not Found</body></html>"), null);
});

// ---------------------------------------------------------------------------
// 4. 语法门：scripts/ 下所有 .cjs 必须能通过 node --check
// ---------------------------------------------------------------------------

test("scripts/ 目录下所有 .cjs 通过 node --check", () => {
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "node_modules") continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".cjs")) files.push(full);
    }
  };
  walk(SCRIPTS_DIR);

  assert.ok(files.length >= 3, `应至少发现 3 个 .cjs，实际 ${files.length}`);

  const broken = [];
  for (const file of files) {
    const r = spawnSync(process.execPath, ["--check", file], {
      encoding: "utf-8",
      windowsHide: true,
    });
    if (r.status !== 0) {
      broken.push(`${path.relative(SCRIPTS_DIR, file)}: ${(r.stderr || "").split("\n")[0]}`);
    }
  }
  assert.deepStrictEqual(broken, [], `存在语法错误的脚本:\n${broken.join("\n")}`);
});

test("三个 vendor 下载脚本可被 require 且不产生副作用", () => {
  // require.main === module 守卫必须在位，否则单测一 require 就会真去下载。
  for (const name of [
    "download-uv-binary.cjs",
    "download-fnm-binary.cjs",
    "download-sqlite-vec-binary.cjs",
  ]) {
    const mod = require(path.join(SCRIPTS_DIR, name));
    assert.strictEqual(
      typeof mod.downloadForPlatform,
      "function",
      `${name} 应导出 downloadForPlatform`
    );
    assert.strictEqual(typeof mod.detectPlatform, "function", `${name} 应导出 detectPlatform`);
  }
});

test("vendor 下载脚本不再各自复制 curl 失败判定（统一走 lib）", () => {
  // 防止后来者再复制一份带误诊的 curlDownload 进来。
  for (const name of [
    "download-uv-binary.cjs",
    "download-fnm-binary.cjs",
    "download-sqlite-vec-binary.cjs",
  ]) {
    const src = fs.readFileSync(path.join(SCRIPTS_DIR, name), "utf-8");
    assert.ok(
      src.includes("vendor-download-utils"),
      `${name} 应引入 lib/vendor-download-utils.cjs`
    );
    assert.ok(
      !/curl 不可用，尝试 wget/.test(src.replace(/curl 不可用（\$\{[^}]+\}）/g, "")),
      `${name} 不应保留无条件的「curl 不可用」文案`
    );
  }
});
