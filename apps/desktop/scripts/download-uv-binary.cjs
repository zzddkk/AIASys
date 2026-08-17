const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawnSync } = require("child_process");
const {
  describeSpawnFailure,
  parseChecksumsTxt,
  parseSingleSha256,
} = require("./lib/vendor-download-utils.cjs");

const UV_VERSION = "0.11.21";
const REPO = "astral-sh/uv";

const PLATFORM_TRIPLES = {
  "linux-x64":    "uv-x86_64-unknown-linux-gnu.tar.gz",
  "linux-arm64":  "uv-aarch64-unknown-linux-gnu.tar.gz",
  "darwin-x64":   "uv-x86_64-apple-darwin.tar.gz",
  "darwin-arm64": "uv-aarch64-apple-darwin.tar.gz",
  "win-x64":      "uv-x86_64-pc-windows-msvc.zip",
};

/**
 * 镜像优先级：
 * 1. UV_DOWNLOAD_MIRROR 环境变量（完整 URL 前缀，如 https://ghfast.top/https://github.com）
 * 2. 空 = 直连 https://github.com
 */
function downloadBase() {
  const env = process.env.UV_DOWNLOAD_MIRROR;
  return env ? env.replace(/\/+$/, "") : "https://github.com";
}

function detectPlatform() {
  const platform = process.platform;
  const arch = process.arch;
  if (platform === "darwin" && arch === "arm64") return "darwin-arm64";
  if (platform === "darwin" && arch === "x64")   return "darwin-x64";
  if (platform === "linux" && arch === "arm64")  return "linux-arm64";
  if (platform === "linux" && arch === "x64")    return "linux-x64";
  if (platform === "win32")                       return "win-x64";
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
  console.log(`[download-uv] 下载: ${url}`);
  // 诊断必须区分「命令不在 PATH」和「命令跑了但请求失败」。curl -f 遇 404 退出码是 22，
  // 此时 curl 本身完好；旧版把两者一律报成「curl 不可用」，掩盖了 URL 拼错的真问题。
  let curlFail = null;
  let wgetFail = null;
  let result = spawnSync(
    "curl",
    ["-L", "-f", "--connect-timeout", "15", "--max-time", "300", "-o", dest, url],
    { encoding: "utf-8", stdio: "pipe", windowsHide: true }
  );
  if (result.status !== 0) {
    curlFail = describeSpawnFailure(result);
    console.log(
      curlFail.kind === "missing"
        ? `[download-uv] curl 不可用（${curlFail.detail}），尝试 wget...`
        : `[download-uv] curl 请求失败（${curlFail.detail}），尝试 wget...`
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
  console.log(`[download-uv] 已保存: ${dest} (${(stat.size / 1024 / 1024).toFixed(1)} MB)`);
}

function verifySha256(filePath, expectedHash) {
  let buf;
  try {
    buf = fs.readFileSync(filePath);
  } catch (err) {
    throw new Error(`[download-uv] 读取文件失败，无法完成 SHA256 校验: ${err.message}`);
  }
  const actualHash = crypto.createHash("sha256").update(buf).digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error(
      `SHA256 校验失败: 期望 ${expectedHash}, 实际 ${actualHash}`
    );
  }
  console.log("[download-uv] SHA256 校验通过");
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

function extractArchive(archivePath, targetDir, isZip) {
  console.log(`[download-uv] 解压: ${archivePath}`);
  fs.mkdirSync(targetDir, { recursive: true });
  let result;
  if (isZip) {
    const isWindows = process.platform === "win32";

    // Windows 上不能直接写 "tar"。PATH 里常先命中 Git for Windows 携带的 GNU tar，
    // 它把归档路径里的盘符冒号当成 rsh 的「主机:路径」语法，实测：
    //
    //   tar: Cannot connect to C: resolve failed
    //
    // 而 Windows 10+ 自带的 bsdtar（System32\tar.exe）能正常处理盘符路径。用绝对
    // 路径钉住它，不依赖 PATH 顺序——顺序取决于每台机器装了什么，不可控。
    const systemTar = path.join(
      process.env.SystemRoot || "C:\\Windows",
      "System32",
      "tar.exe"
    );
    const primaryCmd = isWindows
      ? fs.existsSync(systemTar)
        ? systemTar
        : "tar"
      : "unzip";
    const primaryArgs = isWindows
      ? ["-xf", archivePath, "-C", targetDir]
      : ["-q", "-o", archivePath, "-d", targetDir];
    const primaryResult = spawnSync(primaryCmd, primaryArgs, {
      encoding: "utf-8",
      stdio: "pipe",
      windowsHide: true,
    });
    if (primaryResult.status === 0) {
      result = primaryResult;
    } else {
      // 回退条件从「仅 ENOENT」放宽到「任何失败」。
      //
      // 原来只在命令不存在时才回退到 python zipfile，而本次故障恰恰是命令存在但
      // 执行失败（GNU tar 误解盘符），于是那条可用的回退被整个跳过、直接抛错。
      // 判据错了一格：要回退的理由是「这个解压器没干成」，不是「这个解压器不存在」。
      const why = primaryResult.error
        ? String(primaryResult.error.code || primaryResult.error.message)
        : `exit ${primaryResult.status}: ${(primaryResult.stderr || "").trim()}`;
      const pyCmd = resolvePython();
      if (!pyCmd) {
        throw new Error(
          `${primaryCmd} 解压失败（${why}），且未找到可回退的 Python 解释器（尝试 py/python3/python）`
        );
      }
      console.log(
        `[download-uv] ${primaryCmd} 解压失败（${why}），改用 ${pyCmd} zipfile 解压`
      );
      const pyResult = spawnSync(
        pyCmd,
        ["-m", "zipfile", "-e", archivePath, targetDir],
        { encoding: "utf-8", stdio: "pipe", windowsHide: true }
      );
      if (pyResult.status !== 0) {
        throw new Error(
          `${pyCmd} zipfile 解压失败: ${pyResult.stderr || pyResult.error}`
        );
      }
      result = pyResult;
    }
  } else {
    // 只有 Unix 会走到这里：PLATFORM_TRIPLES 里 Windows 用的是 .zip
    result = spawnSync("tar", ["-xzf", archivePath, "-C", targetDir], {
      encoding: "utf-8",
      stdio: "pipe",
      windowsHide: true,
    });
  }
  if (result.status !== 0) {
    throw new Error(`解压失败: ${result.stderr || result.error}`);
  }
}

async function downloadForPlatform(slug) {
  slug = slug || detectPlatform();
  const assetName = PLATFORM_TRIPLES[slug];
  if (!assetName) throw new Error(`未知平台 slug: ${slug}`);

  const repoRoot = resolveRepoRoot();
  const vendorDir = path.join(repoRoot, "apps", "backend", "vendor", "uv");
  const platformDirMap = {
    "linux-x64":    "linux-x64",
    "linux-arm64":  "linux-arm64",
    "darwin-x64":   "darwin-x64",
    "darwin-arm64": "darwin-arm64",
    "win-x64":      "windows-x64",
  };
  const platformDirName = platformDirMap[slug];
  const platformDir = path.join(vendorDir, platformDirName);
  const binaryName = slug.startsWith("win") ? "uv.exe" : "uv";
  const binaryPath = path.join(platformDir, binaryName);

  if (fs.existsSync(binaryPath)) {
    console.log(`[download-uv] ${slug} 已存在，跳过下载`);
    return binaryPath;
  }

  const isZip = assetName.endsWith(".zip");
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
    const url = `${base}/${REPO}/releases/download/${UV_VERSION}/${assetName}`;
    try {
      curlDownload(url, archivePath);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      console.warn(`[download-uv] 下载失败，尝试下一个源: ${err.message}`);
    }
  }
  if (lastErr) throw lastErr;

  // 下载 sha256 校验文件并校验（获取失败时降级为警告，不阻塞构建）
  let expectedSha = null;
  for (const base of bases) {
    const url = `${base}/${REPO}/releases/download/${UV_VERSION}/${shaName}`;
    try {
      curlDownload(url, shaPath);
      expectedSha = parseSingleSha256(fs.readFileSync(shaPath, "utf-8"));
      if (expectedSha) break;
      console.warn(`[download-uv] ${shaName} 内容里没有找到 sha256 字段，换下一个源`);
    } catch (err) {
      console.warn(`[download-uv] 获取 ${shaName} 失败: ${err.message}`);
    }
  }

  if (expectedSha) {
    verifySha256(archivePath, expectedSha);
  } else {
    console.warn(`[download-uv] 未取得 ${assetName} 的 sha256，本次下载未校验完整性`);
  }

  const extractDir = path.join(downloadDir, "_extracted");
  extractArchive(archivePath, extractDir, isZip);

  // 某些压缩包在根目录，有些有一层平台子目录，递归查找 uv/uv.exe
  function findBinary(dir) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        const found = findBinary(fullPath);
        if (found) return found;
      } else if (entry.name === binaryName) {
        return fullPath;
      }
    }
    return null;
  }

  const srcBinary = findBinary(extractDir);
  if (!srcBinary) {
    throw new Error(`解压后未找到 ${binaryName}，已解压目录: ${extractDir}`);
  }

  fs.mkdirSync(platformDir, { recursive: true });
  fs.cpSync(srcBinary, binaryPath);

  if (!slug.startsWith("win")) {
    fs.chmodSync(binaryPath, (fs.statSync(binaryPath).mode | 0o111) & 0o7777);
  }

  console.log(`[download-uv] 已放置: ${binaryPath}`);

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
用法: node download-uv-binary.cjs [平台 slug]

平台 slug:
  darwin-arm64    macOS Apple Silicon
  darwin-x64      macOS Intel
  linux-arm64     Linux ARM64
  linux-x64       Linux x64
  win-x64         Windows x64

不传参数时自动检测当前平台。
示例:
  node apps/desktop/scripts/download-uv-binary.cjs
  node apps/desktop/scripts/download-uv-binary.cjs linux-x64

镜像加速（网络不佳时）:
  UV_DOWNLOAD_MIRROR=https://ghfast.top/https://github.com node apps/desktop/scripts/download-uv-binary.cjs
`);
    process.exit(0);
  }

  (async () => {
    try {
      const slug = targetSlug || detectPlatform();
      console.log(`[download-uv] 平台: ${slug}, uv v${UV_VERSION}`);
      const result = await downloadForPlatform(slug);
      console.log(`[download-uv] 完成: ${result}`);
      process.exit(0);
    } catch (err) {
      console.error(`[download-uv] 失败: ${err.message}`);
      process.exit(1);
    }
  })();
}

module.exports = { downloadForPlatform, detectPlatform, UV_VERSION };

if (require.main === module) {
  main();
}
