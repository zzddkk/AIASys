/**
 * 工作区 / 会话列表的搜索谓词。
 *
 * 抽成纯函数有两个原因：
 * 1. 过滤逻辑跨 DesignSidebar（工作区）和 WorkspaceConversationPanel（会话）两处，
 *    行为必须一致（标题 + 描述/最后一问多字段匹配，大小写不敏感）；
 * 2. 内联在组件里无法单测，抽出后可用探针验证不是恒绿。
 *
 * 设计依据：参考资料/前端全域借鉴清单.md P0-1。
 */

export interface WorkspaceSearchable {
  title?: string | null;
  description?: string | null;
}

export interface ConversationSearchable {
  title?: string | null;
  last_user_preview?: string | null;
}

function normalize(query: string): string {
  return query.trim().toLowerCase();
}

function fieldIncludes(haystack: string | null | undefined, needle: string): boolean {
  if (!needle) return true;
  if (!haystack) return false;
  return haystack.toLowerCase().includes(needle);
}

/** 工作区搜索：标题（缺省「未命名工作区」）或描述命中即返回。 */
export function matchesWorkspace(workspace: WorkspaceSearchable, query: string): boolean {
  const needle = normalize(query);
  if (!needle) return true;
  return (
    fieldIncludes(workspace.title || "未命名工作区", needle) ||
    fieldIncludes(workspace.description, needle)
  );
}

/** 会话搜索：标题（缺省「未命名对话」）或最后一问预览命中即返回。 */
export function matchesConversation(conversation: ConversationSearchable, query: string): boolean {
  const needle = normalize(query);
  if (!needle) return true;
  return (
    fieldIncludes(conversation.title || "未命名对话", needle) ||
    fieldIncludes(conversation.last_user_preview, needle)
  );
}
