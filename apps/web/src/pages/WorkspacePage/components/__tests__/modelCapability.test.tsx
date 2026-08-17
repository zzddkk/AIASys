import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  isImageFilename,
  shouldWarnImageAttachment,
} from "../imageAttachmentWarning";
import {
  ModelCapabilityBadges,
  capabilityLabels,
} from "../ModelCapabilityBadges";

/**
 * 模型能力标识与非视觉模型发图警告的契约测试
 * （设计文档：AIASys-product-design/交互设计/model-capability-display.md）。
 *
 * 核心契约：警告只在「确知模型无 image_in 且有图片附件」时出现；
 * undefined（未解析出具体模型，如 system 默认）不警告——误报比漏报更伤信任。
 */

describe("isImageFilename", () => {
  it("原有四种格式", () => {
    for (const name of ["a.png", "b.jpg", "c.jpeg", "d.gif", "e.webp"]) {
      expect(isImageFilename(name)).toBe(true);
    }
  });

  it("扩展的三种格式（svg/bmp/avif）", () => {
    for (const name of ["icon.svg", "scan.bmp", "photo.avif"]) {
      expect(isImageFilename(name)).toBe(true);
    }
  });

  it("大小写不敏感", () => {
    expect(isImageFilename("A.PNG")).toBe(true);
  });

  it("非图片返回 false", () => {
    for (const name of ["a.txt", "b.pdf", "c.svg.exe", "noext"]) {
      expect(isImageFilename(name)).toBe(false);
    }
  });
});

describe("shouldWarnImageAttachment", () => {
  it("确知不支持 + 有图片 → 警告", () => {
    expect(
      shouldWarnImageAttachment({
        supportsImageInput: false,
        filenames: ["photo.png"],
      }),
    ).toBe(true);
  });

  it("确知不支持 + 只有非图片附件 → 不警告", () => {
    expect(
      shouldWarnImageAttachment({
        supportsImageInput: false,
        filenames: ["notes.txt", "data.csv"],
      }),
    ).toBe(false);
  });

  it("支持图片 → 不警告", () => {
    expect(
      shouldWarnImageAttachment({
        supportsImageInput: true,
        filenames: ["photo.png"],
      }),
    ).toBe(false);
  });

  it("undefined（未解析出模型）→ 不警告", () => {
    expect(
      shouldWarnImageAttachment({
        supportsImageInput: undefined,
        filenames: ["photo.png"],
      }),
    ).toBe(false);
  });

  it("无附件 → 不警告", () => {
    expect(
      shouldWarnImageAttachment({ supportsImageInput: false, filenames: [] }),
    ).toBe(false);
  });
});

describe("capabilityLabels", () => {
  it("image_in → 图片", () => {
    expect(capabilityLabels(["image_in"])).toEqual(["图片"]);
  });

  it("全能力", () => {
    expect(capabilityLabels(["image_in", "video_in", "thinking"])).toEqual([
      "图片",
      "视频",
      "思考",
    ]);
  });

  it("always_thinking 并入「思考」，不重复显示", () => {
    expect(capabilityLabels(["always_thinking"])).toEqual(["思考"]);
    expect(capabilityLabels(["thinking", "always_thinking"])).toEqual(["思考"]);
  });

  it("无能力 / 未声明 → 空", () => {
    expect(capabilityLabels([])).toEqual([]);
    expect(capabilityLabels(undefined)).toEqual([]);
  });
});

describe("ModelCapabilityBadges 渲染", () => {
  it("有能力时渲染对应标签", () => {
    render(<ModelCapabilityBadges capabilities={["image_in", "thinking"]} />);
    expect(screen.queryByText("图片")).not.toBeNull();
    expect(screen.queryByText("思考")).not.toBeNull();
    expect(screen.queryByText("视频")).toBeNull();
  });

  it("无能力时不渲染任何标签", () => {
    const { container } = render(<ModelCapabilityBadges capabilities={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
