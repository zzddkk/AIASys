/**
 * workspaceFiles unit tests (Node.js standalone).
 *
 * Run: node --experimental-strip-types src/utils/__tests__/workspaceFiles.test.mjs
 */

import assert from "node:assert";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

globalThis.window = {
  location: {
    origin: "http://localhost:13000",
    pathname: "/workspace/session-1",
    port: "13000",
    protocol: "http:",
    hostname: "localhost",
  },
};

globalThis.localStorage = {
  getItem: (key) => (key === "user_id" ? "local_default" : null),
};

const sourceUrl = new URL("../workspaceFiles.ts", import.meta.url);
const sourcePath = fileURLToPath(sourceUrl);
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiasys-workspace-files-test-"));

// 临时目录必须自带 package.json 声明 ESM。node 判定 .ts/.js 的模块类型时只看最近的
// package.json，系统 temp 目录下没有，于是被测源码被当成 CJS，import 语句直接报
// "Cannot use import statement outside a module"。项目目录内不会暴露这个问题——
// apps/web/package.json 的 "type":"module" 兜住了，一旦把文件搬到 temp 就露出来。
fs.writeFileSync(path.join(tmpDir, "package.json"), JSON.stringify({ type: "module" }));
const tmpPath = path.join(tmpDir, "workspaceFiles.ts");
const apiStubPath = path.join(tmpDir, "api.ts");
const httpClientStubPath = path.join(tmpDir, "httpClient.ts");
const previewRegistryStubPath = path.join(tmpDir, "filePreviewRegistry.ts");
const urlUtilsStubPath = path.join(tmpDir, "urlUtils.ts");

fs.writeFileSync(
  apiStubPath,
  `
export const API_BASE_URL = "";
export function encodePathPreservingSlashes(path: string): string {
  return path.split("/").map((segment) => encodeURIComponent(segment)).join("/");
}
export const API_ENDPOINTS = {
  FILES_DOWNLOAD: (userId: string, sessionId: string, filename: string) =>
    \`/api/files/download/\${userId}/\${sessionId}/\${encodePathPreservingSlashes(filename)}\`,
  WORKSPACE_FILE_DOWNLOAD: (workspaceId: string, filename: string) =>
    \`/api/workspaces/\${encodeURIComponent(workspaceId)}/files/download/\${encodePathPreservingSlashes(filename)}\`,
  GLOBAL_WORKSPACE_DOWNLOAD: (workspaceId: string, assetPath: string) =>
    \`/api/workspaces/\${encodeURIComponent(workspaceId)}/global-workspace/download/\${encodePathPreservingSlashes(assetPath)}\`,
};
export function getCurrentUserId(): string {
  return "local_default";
}
`,
);
fs.writeFileSync(
  httpClientStubPath,
  "export const apiFetch = (...args: unknown[]) => fetch(...(args as [RequestInfo | URL, RequestInit?]));\n",
);
fs.writeFileSync(
  previewRegistryStubPath,
  `
export function getPreviewUrlOptions(type: string | null) {
  return type === "pdf" ? { disposition: "inline" as const } : {};
}
export function inferPreviewType(path: string, declaredType?: string): string {
  return declaredType === "application/pdf" || path.toLowerCase().endsWith(".pdf") ? "pdf" : "unknown";
}
export function inferWorkspaceRenderableFileType() {
  return null;
}
export type PreviewFile = {
  name: string;
  type: string;
  url: string;
  downloadUrl?: string;
  size?: number;
  mtime?: string;
  absolute_path?: string | null;
  resource_type?: string;
  schema_kind?: string;
  preview_kind?: string;
  renderer_hint?: string;
  meta?: Record<string, unknown>;
};
export type WorkspaceRenderableFileType = string;
`,
);
fs.writeFileSync(
  urlUtilsStubPath,
  `
export const appendAccessToken = (url: string): string => url;
export function stripApiBaseUrl(url: string): string {
  return url;
}
`,
);

const source = fs
  .readFileSync(sourcePath, "utf-8")
  .replace(
    /from "@\/config\/api"/g,
    `from "${pathToFileURL(apiStubPath).href}"`,
  )
  .replace(
    /from "@\/lib\/api\/httpClient"/g,
    `from "${pathToFileURL(httpClientStubPath).href}"`,
  )
  .replace(
    /from "@\/utils\/filePreviewRegistry"/g,
    `from "${pathToFileURL(previewRegistryStubPath).href}"`,
  )
  .replace(
    /from "@\/utils\/urlUtils"/g,
    `from "${pathToFileURL(urlUtilsStubPath).href}"`,
  );
fs.writeFileSync(tmpPath, source);

const moduleUrl = pathToFileURL(tmpPath);
const { createWorkspacePreviewFile } = await import(moduleUrl.href);

const pdfPreview = createWorkspacePreviewFile(
  { name: "reports/demo file.pdf", type: "application/pdf" },
  "session-1",
  undefined,
  "workspace-1",
);

assert.strictEqual(
  pdfPreview.url,
  "/api/workspaces/workspace-1/files/download/reports/demo%20file.pdf?disposition=inline&preview_mode=embed_v1",
  "workspace-scoped preview should use the workspace file download API",
);

assert.strictEqual(
  pdfPreview.downloadUrl,
  "/api/workspaces/workspace-1/files/download/reports/demo%20file.pdf?disposition=attachment",
  "workspace-scoped download should use the workspace file download API",
);

const sessionPreview = createWorkspacePreviewFile(
  { name: "reports/demo file.pdf", type: "application/pdf" },
  "session-1",
);

assert.strictEqual(
  sessionPreview.url,
  "/api/files/download/local_default/session-1/reports/demo%20file.pdf?user_id=local_default&disposition=inline&preview_mode=embed_v1",
  "session-scoped preview should keep using the session file download API",
);

console.log("3 passed");
