import { describe, expect, it } from "vitest";

import {
  countAssetTreeEntries,
  countWorkspaceAssetEntries,
} from "@/utils/assetTreeCounts";

/**
 * 计数徽标的语义锁定。
 *
 * 这组测试来自一个真缺陷（2026-08-11 现场）：徽标有两套互不相同的定义，
 * 资源树加载完用 countAssetTreeEntries（什么都数），未加载用
 * countWorkspaceAssetEntries（数 files/list，而后者本就不返回点目录）。
 * 结果是刚建好的工作区徽标从「0 文件」跳成「10 文件」，那 10 个全是 .aiasys 内部管线。
 *
 * 它同时把 e2e 变成了看运气：同一个 spec 里断言早的用例通过、多点几下的用例失败，
 * 表面像 flaky，实则是两套定义在打架。所以这里的核心断言是**两套定义必须收敛**。
 *
 * 夹具用的是真实数据：下面这棵树抄自 2026-08-11 实测
 * GET /api/workspaces/{id}/resources/tree 对一个新建工作区的返回。
 */

/** 新建工作区自带的 .aiasys 脚手架：实测 10 文件 5 目录，全是内部管线。 */
const scaffoldNodes = [
  {
    name: ".aiasys",
    path: ".aiasys",
    node_type: "directory",
    children: [
      {
        name: "memory",
        path: ".aiasys/memory",
        node_type: "directory",
        children: [
          {
            name: "workspace_memory.md",
            path: ".aiasys/memory/workspace_memory.md",
            node_type: "resource",
          },
          {
            name: "workspace_memory.md.lock",
            path: ".aiasys/memory/workspace_memory.md.lock",
            node_type: "resource",
          },
        ],
      },
      {
        name: "skills",
        path: ".aiasys/skills",
        node_type: "directory",
        children: [
          {
            name: "aiasys-tool-usage-skill",
            path: ".aiasys/skills/aiasys-tool-usage-skill",
            node_type: "directory",
            children: [
              {
                name: ".aiasys-skill-meta.json",
                path: ".aiasys/skills/aiasys-tool-usage-skill/.aiasys-skill-meta.json",
                node_type: "resource",
              },
              {
                name: "SKILL.md",
                path: ".aiasys/skills/aiasys-tool-usage-skill/SKILL.md",
                node_type: "resource",
              },
            ],
          },
        ],
      },
      {
        name: "workspace",
        path: ".aiasys/workspace",
        node_type: "directory",
        children: [
          {
            name: "conversations.json",
            path: ".aiasys/workspace/conversations.json",
            node_type: "resource",
          },
          {
            name: "conversations.json.lock",
            path: ".aiasys/workspace/conversations.json.lock",
            node_type: "resource",
          },
          {
            name: "workspace.json",
            path: ".aiasys/workspace/workspace.json",
            node_type: "resource",
          },
          {
            name: "workspace.json.lock",
            path: ".aiasys/workspace/workspace.json.lock",
            node_type: "resource",
          },
        ],
      },
      {
        name: "capabilities.toml",
        path: ".aiasys/capabilities.toml",
        node_type: "resource",
      },
      {
        name: "project_profile.md",
        path: ".aiasys/project_profile.md",
        node_type: "resource",
      },
    ],
  },
];

/** 用户真正放进去的东西。 */
const userNodes = [
  {
    name: "browser-regression",
    path: "browser-regression",
    node_type: "directory",
    children: [
      {
        name: "readme.md",
        path: "browser-regression/readme.md",
        node_type: "resource",
      },
      {
        name: "summary.md",
        path: "browser-regression/summary.md",
        node_type: "resource",
      },
    ],
  },
];

describe("countAssetTreeEntries：内部管线不计数", () => {
  it("只有 .aiasys 脚手架的新建工作区，计数为 0 —— 不是实测到的 10 文件 5 目录", () => {
    expect(countAssetTreeEntries(scaffoldNodes)).toEqual({
      fileCount: 0,
      directoryCount: 0,
    });
  });

  it("脚手架与用户文件共存时，只数用户文件", () => {
    expect(countAssetTreeEntries([...scaffoldNodes, ...userNodes])).toEqual({
      fileCount: 2,
      directoryCount: 1,
    });
  });

  it("点开头的文件被排除，且不因为父目录可见就被放进来", () => {
    const nodes = [
      {
        name: "visible",
        path: "visible",
        node_type: "directory",
        children: [
          { name: ".hidden.md", path: "visible/.hidden.md", node_type: "resource" },
          { name: "shown.md", path: "visible/shown.md", node_type: "resource" },
        ],
      },
    ];
    expect(countAssetTreeEntries(nodes)).toEqual({
      fileCount: 1,
      directoryCount: 1,
    });
  });

  it(".lock 文件被排除", () => {
    const nodes = [
      { name: "data.csv", path: "data.csv", node_type: "resource" },
      { name: "data.csv.lock", path: "data.csv.lock", node_type: "resource" },
    ];
    expect(countAssetTreeEntries(nodes).fileCount).toBe(1);
  });

  it("文件夹占位符被排除，与列表版口径一致", () => {
    const nodes = [
      {
        name: "empty-folder",
        path: "empty-folder",
        node_type: "directory",
        children: [
          {
            name: "__aiasys_folder__.md",
            path: "empty-folder/__aiasys_folder__.md",
            node_type: "resource",
          },
        ],
      },
    ];
    expect(countAssetTreeEntries(nodes)).toEqual({
      fileCount: 0,
      directoryCount: 1,
    });
  });

  it("没有 name 时退回用 path 的末段判断", () => {
    const nodes = [
      { path: ".aiasys", node_type: "directory", children: [] },
      { path: "a/b/keep.md", node_type: "resource" },
    ];
    expect(countAssetTreeEntries(nodes)).toEqual({
      fileCount: 1,
      directoryCount: 0,
    });
  });

  it("Windows 反斜杠路径同样能识别出内部节点", () => {
    const nodes = [{ path: "a\\b\\.hidden.md", node_type: "resource" }];
    expect(countAssetTreeEntries(nodes).fileCount).toBe(0);
  });
});

describe("两套计数定义必须收敛", () => {
  /**
   * 这是整组测试的核心。徽标取哪套定义取决于资源树是否加载完成，
   * 所以两者对「同一份逻辑内容」必须给出同一个答案，否则用户会看到数字跳变，
   * e2e 也会随加载时序随机红绿。
   */
  it("同一份用户内容：树版与列表版给出相同计数", () => {
    const fromTree = countAssetTreeEntries([...scaffoldNodes, ...userNodes]);
    const fromList = countWorkspaceAssetEntries([
      { name: "browser-regression/readme.md" },
      { name: "browser-regression/summary.md" },
    ]);
    expect(fromTree).toEqual(fromList);
  });

  it("空工作区：两版都是 0", () => {
    expect(countAssetTreeEntries(scaffoldNodes)).toEqual(
      countWorkspaceAssetEntries([]),
    );
  });

  it("含空文件夹（只有占位符）时两版仍一致", () => {
    const fromTree = countAssetTreeEntries([
      ...scaffoldNodes,
      {
        name: "browser-regression",
        path: "browser-regression",
        node_type: "directory",
        children: [
          {
            name: "empty",
            path: "browser-regression/empty",
            node_type: "directory",
            children: [
              {
                name: "__aiasys_folder__.md",
                path: "browser-regression/empty/__aiasys_folder__.md",
                node_type: "resource",
              },
            ],
          },
        ],
      },
    ]);
    const fromList = countWorkspaceAssetEntries([
      { name: "browser-regression/empty/__aiasys_folder__.md" },
    ]);
    // 2 个目录、0 个文件——这正是 e2e 里那条「创建空文件夹」用例的期望。
    expect(fromTree).toEqual({ fileCount: 0, directoryCount: 2 });
    expect(fromTree).toEqual(fromList);
  });
});
