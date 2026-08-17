import { WORKSPACE_FOLDER_MARKER_FILENAME } from "@/utils/fileTreeUtils";

interface WorkspaceAssetFileLike {
  name: string;
}

interface AssetTreeNodeLike {
  name?: string;
  path?: string;
  node_type?: string;
  children?: AssetTreeNodeLike[];
}

function normalizeAssetPath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\/+/, "").trim();
}

function isFolderMarkerPath(path: string): boolean {
  return (
    normalizeAssetPath(path).split("/").filter(Boolean).pop() ===
    WORKSPACE_FOLDER_MARKER_FILENAME
  );
}

/**
 * 内部管线节点：不该计入用户可见的计数徽标。
 *
 * 为什么需要它（2026-08-11 实测的一个真缺陷）——计数徽标此前有两套互不相同的定义：
 *
 *   资源树加载完成 → countAssetTreeEntries：什么都数
 *   资源树尚未加载 → countWorkspaceAssetEntries：数 files/list 的结果
 *
 * 而 files/list 本就不返回点目录，countWorkspaceAssetEntries 还会排除文件夹占位符。
 * 于是同一个刚建好的工作区，徽标会先显示「0 文件」，等资源树加载完再跳成「10 文件」，
 * 那 10 个全是内部管线：
 *   .aiasys/memory/workspace_memory.md(.lock)
 *   .aiasys/workspace/conversations.json(.lock)、workspace.json(.lock)
 *   .aiasys/skills/…/SKILL.md、.aiasys-skill-meta.json
 *   .aiasys/capabilities.toml、.aiasys/project_profile.md
 *
 * 这既是用户可见的跳变，也让 e2e 用例的成败取决于「断言跑得比树加载快不快」——
 * 同一个文件里断言早的那条通过、操作多几步的那条失败，看起来像 flaky，实则是定义分裂。
 * 这里让树版对齐列表版：点开头、.lock 结尾、文件夹占位符一律不计，内部目录整棵跳过。
 */
function isInternalAssetNode(node: AssetTreeNodeLike): boolean {
  const nameFromPath = normalizeAssetPath(node.path || "")
    .split("/")
    .filter(Boolean)
    .pop();
  const name = (node.name || "").trim() || nameFromPath || "";
  if (!name) {
    return false;
  }
  return (
    name.startsWith(".") ||
    name.endsWith(".lock") ||
    name === WORKSPACE_FOLDER_MARKER_FILENAME
  );
}

export function countWorkspaceAssetEntries(files: WorkspaceAssetFileLike[]) {
  const directoryPaths = new Set<string>();
  let fileCount = 0;

  files.forEach((file) => {
    const normalizedPath = normalizeAssetPath(file.name);
    if (!normalizedPath) {
      return;
    }

    const parts = normalizedPath.split("/").filter(Boolean);
    const isMarker = isFolderMarkerPath(normalizedPath);
    const folderDepth = parts.length - 1;

    for (let index = 1; index <= folderDepth; index += 1) {
      directoryPaths.add(parts.slice(0, index).join("/"));
    }

    if (!isMarker) {
      fileCount += 1;
    }
  });

  return {
    fileCount,
    directoryCount: directoryPaths.size,
  };
}

export function countAssetTreeEntries(nodes: AssetTreeNodeLike[]) {
  let fileCount = 0;
  let directoryCount = 0;

  const visit = (node: AssetTreeNodeLike) => {
    // 内部管线整棵跳过：不计数，也不递归进去。
    if (isInternalAssetNode(node)) {
      return;
    }

    if (node.node_type === "directory") {
      directoryCount += 1;
      node.children?.forEach(visit);
      return;
    }

    fileCount += 1;
  };

  nodes.forEach(visit);

  return {
    fileCount,
    directoryCount,
  };
}
