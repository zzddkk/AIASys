import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// 每个测试后卸载渲染树。不做这件事，上一个测试留下的 DOM 会被下一个测试的
// screen 查询命中，表现为「单独跑通过、连起来跑失败」，排查成本很高。
afterEach(() => {
  cleanup();
});
