"use strict";

/**
 * 测试发现机制自身的回归测试。
 *
 * 这组测试守的是一类特别隐蔽的失效：测试文件写了、内容也对，但 runner 压根没执行它，
 * CI 照样绿。本仓库 web 侧真实发生过——21 个断言从上线起从未被跑过，原因是 runner
 * 的扫描范围写死在单层目录，后来加测试的人没同步改那条 glob。
 *
 * 这里不测「测试是否通过」，测的是「测试是否会被发现」。
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const runner = require("../../scripts/run-unit-tests.cjs");
const { collect, PATTERN, SKIP_DIRS } = runner;

const DESKTOP_DIR = path.resolve(__dirname, "..", "..");

function withFixture(build, assertFn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aiasys-discovery-"));
  try {
    build(root);
    assertFn(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("collect 能发现深层嵌套目录里的测试文件（范围不得写死为单层）", () => {
  withFixture(
    (root) => {
      // 一层
      fs.writeFileSync(path.join(root, "shallow.test.cjs"), "");
      // 三层深
      const deep = path.join(root, "components", "layout", "__tests__");
      fs.mkdirSync(deep, { recursive: true });
      fs.writeFileSync(path.join(deep, "deep.test.cjs"), "");
    },
    (root) => {
      const found = collect(root).map((f) => path.basename(f)).sort();
      assert.deepStrictEqual(
        found,
        ["deep.test.cjs", "shallow.test.cjs"],
        "深层目录里的测试必须被发现，否则会重演 web 侧 21 个断言从未执行的事故"
      );
    }
  );
});

test("collect 跳过 node_modules / dist，不去跑依赖里的测试", () => {
  withFixture(
    (root) => {
      fs.writeFileSync(path.join(root, "mine.test.cjs"), "");
      for (const skip of ["node_modules", "dist"]) {
        const d = path.join(root, skip, "__tests__");
        fs.mkdirSync(d, { recursive: true });
        fs.writeFileSync(path.join(d, "theirs.test.cjs"), "");
      }
    },
    (root) => {
      const found = collect(root).map((f) => path.basename(f));
      assert.deepStrictEqual(found, ["mine.test.cjs"]);
    }
  );
});

test("collect 只认 .test.cjs，不误抓普通源文件", () => {
  withFixture(
    (root) => {
      fs.writeFileSync(path.join(root, "utils.cjs"), "");
      fs.writeFileSync(path.join(root, "utils.test.cjs"), "");
      fs.writeFileSync(path.join(root, "notes.test.txt"), "");
    },
    (root) => {
      assert.deepStrictEqual(
        collect(root).map((f) => path.basename(f)),
        ["utils.test.cjs"]
      );
    }
  );
});

test("collect 对不存在的目录返回空数组而不抛异常", () => {
  assert.deepStrictEqual(collect(path.join(os.tmpdir(), "definitely-not-here-8f3a")), []);
});

test("SKIP_DIRS 至少包含 node_modules（最低防线）", () => {
  assert.ok(SKIP_DIRS.has("node_modules"));
});

test("PATTERN 匹配 .test.cjs 且不匹配 .cjs", () => {
  assert.ok(PATTERN.test("a.test.cjs"));
  assert.ok(!PATTERN.test("a.cjs"));
  assert.ok(!PATTERN.test("test.cjs"));
});

test("runner 在真实 src/ 下能发现现存全部测试文件", () => {
  // 与「用 find 手工数一遍」对照：任何一方漏了都说明发现逻辑与实际布局脱节。
  const walk = (dir) => {
    const out = [];
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name)) continue;
        out.push(...walk(path.join(dir, e.name)));
      } else if (e.name.endsWith(".test.cjs")) {
        out.push(path.join(dir, e.name));
      }
    }
    return out;
  };
  const expected = walk(path.join(DESKTOP_DIR, "src")).sort();
  const actual = collect(runner.SRC_DIR).sort();

  assert.ok(expected.length >= 3, `src/ 下应至少有 3 个测试文件，实际 ${expected.length}`);
  assert.deepStrictEqual(actual, expected);
});

test("package.json 的 test:unit 不得退回依赖 shell 展开的 glob", () => {
  // 防回归：glob 在 Windows cmd/PowerShell 下不展开，会造成「CI 绿、本地跑不了」。
  const pkg = JSON.parse(
    fs.readFileSync(path.join(DESKTOP_DIR, "package.json"), "utf-8")
  );
  const cmd = pkg.scripts["test:unit"];
  assert.ok(cmd, "test:unit 脚本必须存在");
  assert.ok(
    !cmd.includes("*"),
    `test:unit 不应含 glob 通配符，当前为: ${cmd}（请走 scripts/run-unit-tests.cjs）`
  );
  assert.ok(
    cmd.includes("run-unit-tests"),
    `test:unit 应调用递归 runner，当前为: ${cmd}`
  );
});

test("runner 在发现不到测试时必须 exit 1（不能像 glob 那样假绿）", () => {
  // 对照实测：`node --test src/__tests__/*.nonexistent.cjs` 退出码是 0，
  // 也就是说 glob 方案下「一个测试都没跑」和「全部通过」在 CI 上长得一模一样。
  const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiasys-empty-src-"));
  try {
    const r = require("node:child_process").spawnSync(
      process.execPath,
      [path.join(DESKTOP_DIR, "scripts", "run-unit-tests.cjs")],
      {
        encoding: "utf-8",
        env: { ...process.env, AIASYS_DESKTOP_TEST_SRC_DIR: emptyDir },
        windowsHide: true,
      }
    );
    assert.strictEqual(r.status, 1, `空目录应 exit 1，实际 ${r.status}`);
    assert.match(r.stderr || "", /未发现任何匹配/);
  } finally {
    fs.rmSync(emptyDir, { recursive: true, force: true });
  }
});

test("runner 会把发现的文件清单打印出来（便于在 CI 日志核对跑了哪些）", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "aiasys-src-list-"));
  try {
    const nested = path.join(dir, "deep", "__tests__");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(
      path.join(nested, "sample.test.cjs"),
      'require("node:test")("ok", () => {});\n'
    );
    const r = require("node:child_process").spawnSync(
      process.execPath,
      [path.join(DESKTOP_DIR, "scripts", "run-unit-tests.cjs")],
      {
        encoding: "utf-8",
        env: { ...process.env, AIASYS_DESKTOP_TEST_SRC_DIR: dir },
        windowsHide: true,
      }
    );
    assert.match(r.stdout || "", /递归发现 1 个测试文件/);
    assert.match(r.stdout || "", /sample\.test\.cjs/);
    assert.strictEqual(r.status, 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
