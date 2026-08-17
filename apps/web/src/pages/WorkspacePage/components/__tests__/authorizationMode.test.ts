import { describe, it, expect } from "vitest";

import {
  AUTHORIZATION_MODES,
  authorizationModeMeta,
  needsRiskConfirmation,
  DEFAULT_AUTHORIZATION_MODE,
} from "@/lib/api/authorizationMode";

/**
 * 权限档位元数据与风险门控的契约测试
 * （设计文档：AIASys-product-design/交互设计/permission-mode-management.md）。
 *
 * 核心契约：
 * 1. 四档齐全且只有 full_auto 需要风险确认门；
 * 2. 后端下发的空值/未知值回落到默认档（full_auto，与后端 mixins/session.py 一致）；
 * 3. 每档必须有中文显示名和一句话说明（下拉菜单直接消费）。
 */

describe("AUTHORIZATION_MODES 元数据", () => {
  it("四档齐全", () => {
    expect(AUTHORIZATION_MODES.map((m) => m.id)).toEqual([
      "manual",
      "smart",
      "auto",
      "full_auto",
    ]);
  });

  it("每档都有显示名和说明", () => {
    for (const m of AUTHORIZATION_MODES) {
      expect(m.label.length).toBeGreaterThan(0);
      expect(m.description.length).toBeGreaterThan(0);
    }
  });

  it("只有 full_auto 需要风险确认", () => {
    for (const m of AUTHORIZATION_MODES) {
      expect(m.requiresRiskConfirmation).toBe(m.id === "full_auto");
    }
  });
});

describe("authorizationModeMeta", () => {
  it("已知档位返回对应元数据", () => {
    expect(authorizationModeMeta("smart").label).toBe("智能");
  });

  it("null / undefined 回落到默认档", () => {
    expect(authorizationModeMeta(null).id).toBe(DEFAULT_AUTHORIZATION_MODE);
    expect(authorizationModeMeta(undefined).id).toBe(DEFAULT_AUTHORIZATION_MODE);
  });

  it("未知值回落到默认档（后端新增档位时前端不崩）", () => {
    expect(authorizationModeMeta("some_future_mode").id).toBe(
      DEFAULT_AUTHORIZATION_MODE,
    );
  });
});

describe("needsRiskConfirmation", () => {
  it("full_auto 需要确认", () => {
    expect(needsRiskConfirmation("full_auto")).toBe(true);
  });

  it("其余档位不需要", () => {
    expect(needsRiskConfirmation("manual")).toBe(false);
    expect(needsRiskConfirmation("smart")).toBe(false);
    expect(needsRiskConfirmation("auto")).toBe(false);
  });
});
