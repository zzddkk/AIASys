import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// 必须在任何模块被加载前把 NODE_ENV 掰到 test，否则组件测试根本跑不起来。
//
// 本机把 NODE_ENV=production 设成了全局环境变量。vitest 只在 NODE_ENV 未定义时才
// 替你设成 "test"，已定义就沿用——于是 react 的 CJS 入口按 production 分支加载
// react.production.js，而 React.act 只在 development build 里导出。
// 症状是 @testing-library/react 报 "React.act is not a function"，并 fallback 到
// React 19 已废弃的 react-dom-test-utils，报错方向完全指不到真正的原因。
//
// 同一个环境变量此前已经害过一次：npm 因它自动 omit=dev，跳过全部 17 个
// devDependencies（typescript / eslint 都没装），当时被误判成前端缺类型声明。
// apps/web/.npmrc 的 include=dev 压住了 npm 那一侧，这里压住 Node 运行时这一侧。
//
// 下面 test.env 是给测试进程（worker）用的，这行 process.env 是给配置加载阶段用的，
// 两处都要，缺一个都会在某个环节漏回 production。
if (process.env.NODE_ENV === "production") {
  process.env.NODE_ENV = "test";
}

// 前端组件/hook 测试配置。
//
// 为什么单独一份而不复用 vite.config.ts：那份里有 dev server（strictPort 13000）、
// SPA 历史回退中间件、tailwind 插件，测试一个都不需要，还会带来端口占用和多余开销。
// 这里只保留跑组件测试真正必需的两样：react 插件（JSX 转换）与 @ 别名。
//
// include 有意只匹配 .test.ts / .test.tsx，不含 .mjs：
// src/**/__tests__/ 下现存 4 个 .mjs 是「import 即执行断言」的自执行脚本（没有
// describe/it 结构），由 scripts/committed/run-unit-tests.mjs 单独跑。vitest 加载
// 这类文件会因为找不到 test suite 而报错，所以两套并行、各管各的，不去改动那 4 个
// 已经在跑且通过的文件。新写的测试一律用 vitest（.test.ts/.test.tsx）。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
    // 见文件顶部注释：本机全局 NODE_ENV=production 会让 react 走 production build，
    // React.act 缺失导致所有 render/renderHook 直接抛错。这里显式钉住测试进程的值。
    env: {
      NODE_ENV: "test",
    },
    // 10 秒。参考 step-code 的取值理由：给调度抖动留余量，但别大到让真实的死循环
    // 卡满 CI。它那边因为 Ink TUI 有大量硬等待才设到 20 秒，Web 端用 waitFor
    // 条件等待，不需要那么宽。
    testTimeout: 10_000,
    // 组件测试不验证样式，跳过 CSS 处理省时间；组件里 import "*.css" 不会因此报错。
    css: false,
    clearMocks: true,
    restoreMocks: true,
  },
});
