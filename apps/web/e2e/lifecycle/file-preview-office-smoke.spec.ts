import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { Document, Packer, Paragraph, TextRun } from "docx";
import { expect, test } from "@playwright/test";
import PptxGenJS from "pptxgenjs";
import * as XLSX from "xlsx";

import {
  createWorkspace,
  deleteWorkspace,
  getWorkspaceRoot,
  openWorkspaceFilesPanel,
  registerLifecycleUser,
} from "./support";


async function writeWorkspaceBinaryFile(options: {
  userId: string;
  workspaceId: string;
  fileName: string;
  content: Buffer;
}) {
  const targetPath = path.join(
    getWorkspaceRoot(options.userId, options.workspaceId),
    "workspace",
    options.fileName,
  );
  await mkdir(path.dirname(targetPath), { recursive: true });
  await writeFile(targetPath, options.content);
}

async function buildDocxFixture() {
  const doc = new Document({
    sections: [
      {
        children: [
          new Paragraph({
            children: [
              new TextRun({
                text: "Word preview browser smoke marker",
                bold: true,
              }),
            ],
          }),
          new Paragraph("这是浏览器驱动验收用的 docx 文件。"),
        ],
      },
    ],
  });

  return await Packer.toBuffer(doc);
}

function buildXlsxFixture() {
  const workbook = XLSX.utils.book_new();
  const worksheet = XLSX.utils.aoa_to_sheet([
    ["Name", "Value"],
    ["Excel preview browser smoke marker", "passed"],
  ]);
  XLSX.utils.book_append_sheet(workbook, worksheet, "SmokeSheet");

  return XLSX.write(workbook, {
    bookType: "xlsx",
    type: "buffer",
  }) as Buffer;
}

// 前端的 PDF 预览走 <iframe src=...>（PdfPreview.tsx:47），断言只到 toBeAttached，
// 不解析内容，所以一个结构合法的最小 PDF 就够，不必为此引入 pdf 库。
// 这是手写的单页空文档：header + 4 个对象 + xref + trailer。
function buildPdfFixture(): Buffer {
  const objects = [
    "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
    "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
    "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] " +
      "/Resources << >> /Contents 4 0 R >>\nendobj\n",
    "4 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n",
  ];

  const header = "%PDF-1.4\n";
  let body = "";
  const offsets: number[] = [];
  for (const object of objects) {
    offsets.push(header.length + body.length);
    body += object;
  }

  const xrefStart = header.length + body.length;
  let xref = `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) {
    xref += `${String(offset).padStart(10, "0")} 00000 n \n`;
  }
  const trailer =
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n` +
    `startxref\n${xrefStart}\n%%EOF\n`;

  return Buffer.from(header + body + xref + trailer, "latin1");
}

// PPTX 与 PDF 不同：PptPreview.tsx 用 pptx-preview 真解析，断言「PPT 预览加载失败」
// 计数为 0，所以夹具必须是合法 OOXML，不能糊一个 zip 骗过去。
// 用 pptxgenjs 生成，与 docx/xlsx 各用专用库的做法一致。
async function buildPptxFixture(): Promise<Buffer> {
  const deck = new PptxGenJS();
  const slide = deck.addSlide();
  slide.addText("PPT preview browser smoke marker", {
    x: 0.5,
    y: 1,
    w: 8,
    h: 1,
    fontSize: 20,
  });
  return (await deck.write({ outputType: "nodebuffer" })) as Buffer;
}

test.describe("Workspace file preview browser smoke", () => {
  test.setTimeout(180_000);

  test("PDF, Excel, Word and PPT open in the workspace assets preview", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });

    const api = page.request;
    const user = await registerLifecycleUser(api);
    const workspace = await createWorkspace(api, {
      title: `浏览器回归-文件预览-${Date.now()}`,
      mode: "analysis",
      initialConversationTitle: "文件预览会话",
    });

    const files = {
      pdf: "browser-preview.pdf",
      xlsx: "browser-preview.xlsx",
      docx: "browser-preview.docx",
      pptx: "browser-preview.pptx",
    };

    try {
      await writeWorkspaceBinaryFile({
        userId: user.userId,
        workspaceId: workspace.workspaceId,
        fileName: files.docx,
        content: await buildDocxFixture(),
      });
      await writeWorkspaceBinaryFile({
        userId: user.userId,
        workspaceId: workspace.workspaceId,
        fileName: files.xlsx,
        content: buildXlsxFixture(),
      });
      await writeWorkspaceBinaryFile({
        userId: user.userId,
        workspaceId: workspace.workspaceId,
        fileName: files.pdf,
        content: buildPdfFixture(),
      });
      await writeWorkspaceBinaryFile({
        userId: user.userId,
        workspaceId: workspace.workspaceId,
        fileName: files.pptx,
        content: await buildPptxFixture(),
      });

      await page.goto(
        `/analysis?workspace_id=${workspace.workspaceId}&session_id=${workspace.currentConversationId}`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(page.locator("textarea")).toBeVisible();

      const panel = await openWorkspaceFilesPanel(page);

      for (const fileName of Object.values(files)) {
        await expect(panel.getByText(fileName, { exact: true })).toBeVisible();
      }

      const openInMainCanvas = async (fileName: string) => {
        await panel
          .getByRole("button", {
            name: `打开 ${fileName} 的文件操作菜单`,
            exact: true,
          })
          .click();
        await page.getByRole("menuitem", { name: "在主画布打开" }).click();
        await expect(
          page.getByRole("heading", { name: fileName }),
        ).toBeVisible();
      };

      await openInMainCanvas(files.pdf);
      // 主画布传给 PdfPreview 的 fileName 是工作区相对路径（如
      // workspace/browser-preview.pdf），iframe title 随之带前缀；用后缀匹配。
      const pdfFrame = page.locator(`iframe[title$="${files.pdf}"]`);
      await expect(pdfFrame).toBeAttached();
      await expect
        .poll(async () => await pdfFrame.getAttribute("src"))
        .toContain("disposition=inline");
      await page.screenshot({
        path: testInfo.outputPath("file-preview-pdf.png"),
        fullPage: true,
      });

      await openInMainCanvas(files.xlsx);
      await expect(
        page.getByText("Excel preview browser smoke marker", { exact: true }),
      ).toBeVisible();
      await page.screenshot({
        path: testInfo.outputPath("file-preview-excel.png"),
        fullPage: true,
      });

      await openInMainCanvas(files.docx);
      await expect(
        page.getByText("Word preview browser smoke marker", { exact: true }),
      ).toBeVisible();
      await page.screenshot({
        path: testInfo.outputPath("file-preview-word.png"),
        fullPage: true,
      });

      await openInMainCanvas(files.pptx);
      await expect(page.getByText("PPT 预览加载失败")).toHaveCount(0);
      await expect(page.getByText("文件过大，无法预览")).toHaveCount(0);
      await expect(
        page.locator(".pptx-preview-wrapper, .slide").first(),
      ).toBeVisible();
      // 文件信息面板已重设计：按钮叫「文件信息」（MainCanvasPreview），
      // 内容是通用字段（文件名/路径/类型…），不再是产物信息/来源运行/版本记录。
      await expect(page.getByText("文件名", { exact: true })).toHaveCount(0);
      // 非沉浸模式下入口在主画布「更多操作」菜单里（CanvasActionMenu 的
      // menuitem）；工具栏上的「文件信息」按钮只在沉浸预览里渲染。
      await page
        .getByTestId("main-canvas-action-menu")
        .getByRole("button", { name: "更多操作" })
        .click();
      await page.getByRole("menuitem", { name: "查看文件信息" }).click();
      await expect(page.getByText("文件名", { exact: true })).toBeVisible();
      await expect(page.getByText("路径", { exact: true })).toBeVisible();
      await expect(page.getByText("修改时间", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "关闭文件信息" }).click();
      await expect(page.getByText("文件名", { exact: true })).toHaveCount(0);
      await expect(
        page.locator(".pptx-preview-wrapper, .slide").first(),
      ).toBeVisible();
      await page.screenshot({
        path: testInfo.outputPath("file-preview-ppt.png"),
        fullPage: true,
      });
    } finally {
      await deleteWorkspace(api, workspace.workspaceId);
    }
  });
});
