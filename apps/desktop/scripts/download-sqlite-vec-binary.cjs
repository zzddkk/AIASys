const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawnSync } = require("child_process");
const {
  describeSpawnFailure,
  parseChecksumsTxt,
  parseSingleSha256,
} = require("./lib/vendor-download-utils.cjs");

const SQLITE_VEC_VERSION = "0.1.6";
const REPO = "asg017/sqlite-vec";

const PLATFORM_ASSETS = {
  "linux-x64":    { slug: "linux-x86_64",   subdir: "linux-x86_64", filename: "vec0.so"    },
  "linux-arm64":  { slug: "linux-aarch64",  subdir: "linux-aarch64", filename: "vec0.so"    },
  "darwin-x64":   { slug: "macos-x86_64",   subdir: "macos-x86_64", filename: "vec0.dylib" },
  "darwin-arm64": { slug: "macos-aarch64",  subdir: "macos-aarch64", filename: "vec0.dylib" },
  "win-x64":      { slug: "windows-x86_64", subdir: "windows-x86_64", filename: "vec0.dll"  },
};

/**
 * 镜像优先级：
 * 1. SQLITE_VEC_DOWNLOAD_MIRROR 环境变量（完整 URL 前缀）
 * 2. 空 = 直连 https://github.com
 */
function downloadBase() {
  const env = process.env.SQLITE_VEC_DOWNLOAD_MIRROR;
  return env ? env.replace(/\/+$/, "") : "https://github.com";
}

function detectPlatform() {
  const platform = process.platform;
  const arch = process.arch;
  if (platform === "darwin" && arch === "arm64") return "darwin-arm64";
  if (platform === "darwin" && arch === "x64")   return "darwin-x64";
  if (platform === "linux" && arch === "arm64")  return "linux-arm64";
  if (platform === "linux" && arch === "x64")    return "linux-x64";
  if (platform === "win32")                         return "win-x64";
  throw new Error(`不支持的平台: ${platform}-${arch}`);
}

function resolveRepoRoot() {
  let dir = __dirname;
  for (let i = 0; i < 8; i++) {
    const candidate = path.join(dir, "apps", "backend");
    if (fs.existsSync(candidate)) {
      return path.resolve(dir);
    }
    dir = path.dirname(dir);
  }
  return path.resolve(__dirname, "..", "..", "..");
}

function curlDownload(url, dest) {
  console.log(`[download-sqlite-vec] 下载: ${url}`);
  // 诊断必须区分「命令不在 PATH」和「命令跑了但请求失败」。curl -f 遇 404 退出码是 22，
  // 此时 curl 本身完好；旧版把两者一律报成「curl 不可用」，掩盖了 URL 拼错的真问题。
  let curlFail = null;
  let wgetFail = null;
  let result = spawnSync(
    "curl",
    ["-L", "-f", "--connect-timeout", "15", "--max-time", "120", "-o", dest, url],
    { encoding: "utf-8", stdio: "pipe", windowsHide: true }
  );
  if (result.status !== 0) {
    curlFail = describeSpawnFailure(result);
    console.log(
      curlFail.kind === "missing"
        ? `[download-sqlite-vec] curl 不可用（${curlFail.detail}），尝试 wget...`
        : `[download-sqlite-vec] curl 请求失败（${curlFail.detail}），尝试 wget...`
    );
    result = spawnSync(
      "wget",
      ["-q", "--timeout=15", "--tries=3", "-O", dest, url],
      { encoding: "utf-8", stdio: "pipe", windowsHide: true }
    );
  }
  if (result.status !== 0) {
    wgetFail = describeSpawnFailure(result);
    const bothMissing = curlFail?.kind === "missing" && wgetFail.kind === "missing";
    throw new Error(
      bothMissing
        ? `下载失败 (${url}): curl 和 wget 均不在 PATH 上 (curl=${curlFail.detail}, wget=${wgetFail.detail})`
        : `下载失败 (${url}): curl=${curlFail?.detail ?? "n/a"}, wget=${wgetFail.detail}`
    );
  }
  const stat = fs.statSync(dest);
  console.log(`[download-sqlite-vec] 已保存: ${dest} (${(stat.size / 1024).toFixed(1)} KB)`);
}

function verifySha256(filePath, expectedHash) {
  let buf;
  try {
    buf = fs.readFileSync(filePath);
  } catch (err) {
    throw new Error(`[download-sqlite-vec] 读取文件失败，无法完成 SHA256 校验: ${err.message}`);
  }
  const actualHash = crypto.createHash("sha256").update(buf).digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error(
      `SHA256 校验失败: 期望 ${expectedHash}, 实际 ${actualHash}`
    );
  }
  console.log("[download-sqlite-vec] SHA256 校验通过");
  return true;
}

function resolvePython() {
  const candidates = [process.env.PYTHON, process.env.PYTHON3, "py", "python3", "python"].filter(Boolean);
  for (const cmd of candidates) {
    const result = spawnSync(cmd, ["--version"], { encoding: "utf-8", stdio: "pipe", windowsHide: true });
    if (result.status === 0) return cmd;
  }
  return null;
}

function extractTarGz(archivePath, targetDir) {
  console.log(`[download-sqlite-vec] 解压: ${archivePath}`);
  fs.mkdirSync(targetDir, { recursive: true });

  // 与 download-uv-binary.cjs / download-fnm-binary.cjs 同款修复（三处各有一份复制
  // 粘贴的解压逻辑）。这里的 asset 是 tar.gz，Windows 平台也走这条路径，所以同样会
  // 撞上 Git for Windows 的 GNU tar 把盘符冒号当「主机:路径」的问题：
  //   tar: Cannot connect to C: resolve failed
  const systemTar = path.join(
    process.env.SystemRoot || "C:\\Windows",
    "System32",
    "tar.exe"
  );
  const tarCmd =
    process.platform === "win32" && fs.existsSync(systemTar) ? systemTar : "tar";
  const result = spawnSync(tarCmd, ["-xzf", archivePath, "-C", targetDir], {
    encoding: "utf-8",
    stdio: "pipe",
    windowsHide: true,
  });
  if (result.status === 0) return;

  // 回退条件从「仅 ENOENT」放宽到「任何失败」：判据应是「没干成」而非「不存在」。
  const why = result.error
    ? String(result.error.code || result.error.message)
    : `exit ${result.status}: ${(result.stderr || "").trim()}`;
  const pyCmd = resolvePython();
  if (!pyCmd) {
    throw new Error(
      `${tarCmd} 解压失败（${why}），且未找到可回退的 Python 解释器（尝试 py/python3/python）`
    );
  }
  console.log(`[download-sqlite-vec] ${tarCmd} 解压失败（${why}），改用 ${pyCmd} tarfile 解压`);
  // 通过 sys.argv 传参，避免 Windows 反斜杠路径被 Python 解释为转义序列
  const pyResult = spawnSync(
    pyCmd,
    ["-c", "import sys, tarfile, gzip; ar=sys.argv[1]; td=sys.argv[2]; f=tarfile.open(ar, 'r:gz' if ar.endswith('.gz') else 'r'); f.extractall(td)", archivePath, targetDir],
    { encoding: "utf-8", stdio: "pipe", windowsHide: true }
  );
  if (pyResult.status !== 0) {
    throw new Error(`${pyCmd} tarfile 解压失败: ${pyResult.stderr || pyResult.error}`);
  }
}

async function downloadForPlatform(platformSlug) {
  platformSlug = platformSlug || detectPlatform();
  const cfg = PLATFORM_ASSETS[platformSlug];
  if (!cfg) throw new Error(`未知平台 slug: ${platformSlug}`);

  const repoRoot = resolveRepoRoot();
  const vendorDir = path.join(repoRoot, "apps", "backend", "vendor", "sqlite-vec");
  const platformDir = path.join(vendorDir, cfg.subdir);
  const binaryPath = path.join(platformDir, cfg.filename);

  if (fs.existsSync(binaryPath)) {
    console.log(`[download-sqlite-vec] ${platformSlug} 已存在，跳过下载`);
    return binaryPath;
  }

  const assetName = `sqlite-vec-${SQLITE_VEC_VERSION}-loadable-${cfg.slug}.tar.gz`;
  const shaName = `${assetName}.sha256`;

  const bases = [downloadBase()];
  if (bases[0] === "https://github.com") {
    bases.push("https://ghfast.top/https://github.com");
  }

  const downloadDir = path.join(vendorDir, ".download");
  fs.mkdirSync(downloadDir, { recursive: true });
  const archivePath = path.join(downloadDir, assetName);
  const shaPath = path.join(downloadDir, shaName);

  // 下载压缩包
  let lastErr;
  for (const base of bases) {
    const url = `${base}/${REPO}/releases/download/v${SQLITE_VEC_VERSION}/${assetName}`;
    try {
      curlDownload(url, archivePath);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      console.warn(`[download-sqlite-vec] 下载失败，尝试下一个源: ${err.message}`);
    }
  }
  if (lastErr) throw lastErr;

  // sqlite-vec 的 release 不提供 per-asset 的 `<asset>.sha256`（实测 v0.1.6 为 HTTP 404），
  // 提供的是汇总文件 checksums.txt。旧版一直在请求不存在的 `.sha256`，于是每次都静默
  // 「跳过校验」——日志看着像网络不好，实际是文件名从来就不对，校验从未真正执行过。
  const checksumsPath = path.join(downloadDir, "checksums.txt");
  let expectedSha = null;
  for (const base of bases) {
    const url = `${base}/${REPO}/releases/download/v${SQLITE_VEC_VERSION}/checksums.txt`;
    try {
      curlDownload(url, checksumsPath);
      const content = fs.readFileSync(checksumsPath, "utf-8");
      expectedSha = parseChecksumsTxt(content, assetName);
      if (expectedSha) break;
      console.warn(
        `[download-sqlite-vec] checksums.txt 里没有 ${assetName} 的条目，换下一个源`
      );
    } catch (err) {
      console.warn(`[download-sqlite-vec] 获取 checksums.txt 失败: ${err.message}`);
    }
  }

  if (expectedSha) {
    verifySha256(archivePath, expectedSha);
  } else {
    // 拿不到校验值就明确说清是哪种情况，不要让人以为「校验通过了」。
    console.warn(
      `[download-sqlite-vec] 未取得 ${assetName} 的 sha256，本次下载未校验完整性`
    );
  }

  extractTarGz(archivePath, platformDir);

  if (!fs.existsSync(binaryPath)) {
    throw new Error(`解压后未找到 ${cfg.filename}，期望在 ${binaryPath}`);
  }

  if (!platformSlug.startsWith("win")) {
    fs.chmodSync(binaryPath, (fs.statSync(binaryPath).mode | 0o111) & 0o7777);
  }

  console.log(`[download-sqlite-vec] 已放置: ${binaryPath}`);

  fs.rmSync(downloadDir, { recursive: true, force: true });

  return binaryPath;
}

function main() {
  let targetSlug = null;
  let help = false;

  for (const arg of process.argv.slice(2)) {
    if (arg === "--help" || arg === "-h") {
      help = true;
    } else {
      targetSlug = arg;
    }
  }

  if (help) {
    console.log(`
用法: node download-sqlite-vec-binary.cjs [平台 slug]

平台 slug:
  darwin-arm64    macOS Apple Silicon
  darwin-x64      macOS Intel
  linux-arm64     Linux ARM64
  linux-x64       Linux x64
  win-x64         Windows x64

不传参数时自动检测当前平台。
示例:
  node apps/desktop/scripts/download-sqlite-vec-binary.cjs
  node apps/desktop/scripts/download-sqlite-vec-binary.cjs linux-x64

镜像加速（网络不佳时）:
  SQLITE_VEC_DOWNLOAD_MIRROR=https://ghfast.top/https://github.com node apps/desktop/scripts/download-sqlite-vec-binary.cjs
`);
    process.exit(0);
  }

  (async () => {
    try {
      const slug = targetSlug || detectPlatform();
      console.log(`[download-sqlite-vec] 平台: ${slug}, sqlite-vec v${SQLITE_VEC_VERSION}`);
      const result = await downloadForPlatform(slug);
      console.log(`[download-sqlite-vec] 完成: ${result}`);
      process.exit(0);
    } catch (err) {
      console.error(`[download-sqlite-vec] 失败: ${err.message}`);
      process.exit(1);
    }
  })();
}

module.exports = { downloadForPlatform, detectPlatform, SQLITE_VEC_VERSION };

if (require.main === module) {
  main();
}
