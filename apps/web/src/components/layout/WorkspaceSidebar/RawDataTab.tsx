import { Loader2 } from "lucide-react";

import { SqlQueryPanel } from "@/components/database/SqlQueryPanel";

/**
 * 资源预览面板「原始数据」Tab 的共享实现。
 *
 * GraphPreviewPanel 与 KnowledgeBasePreviewPanel 各有一份几乎逐行镜像的
 * 「原始数据」Tab（表列表 + SQL 编辑器 + 结果表格），两侧真实差异仅：
 * 状态变量命名、初始 SQL、`disabled` 依赖的资源 id。这里把结构参数化收敛为单源，
 * 结果表格渲染（列/行/NULL 处理/行数统计）两侧完全一致，是收敛的主要收益。
 *
 * 表与查询结果用结构类型刻画——graph 侧的 GraphTableInfo/GraphRawQueryResponse
 * 与 knowledge 侧的 KnowledgeBaseTableInfo/KnowledgeBaseRawQueryResponse 形状一致，
 * 满足结构兼容即可传入，不要求同一命名类型。
 */
export interface RawDataTableInfo {
  name: string;
  columns: Array<{ name: string }>;
}

export interface RawDataQueryResult {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
}

export interface RawDataTabProps {
  /** 表结构加载中 */
  loadingTables: boolean;
  /** 可查询的表列表 */
  tables: RawDataTableInfo[];
  /** 资源 id（图谱 kgId / 知识库 knowledgeBaseId），为空时禁用查询 */
  resourceId: string | null;
  sql: string;
  onSqlChange: (sql: string) => void;
  queryLimitInput: string;
  onLimitChange: (limit: string) => void;
  running: boolean;
  error: string | null;
  result: RawDataQueryResult | null;
  onRunQuery: () => void;
}

export function RawDataTab({
  loadingTables,
  tables,
  resourceId,
  sql,
  onSqlChange,
  queryLimitInput,
  onLimitChange,
  running,
  error,
  result,
  onRunQuery,
}: RawDataTabProps) {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* 表列表 */}
      {loadingTables ? (
        <div className="flex items-center gap-2 text-micro text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          加载表结构...
        </div>
      ) : tables.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {tables.map((table) => (
            <button
              key={table.name}
              type="button"
              onClick={() =>
                onSqlChange(`SELECT * FROM "${table.name.replace(/"/g, "\"\"")}" LIMIT 100;`)
              }
              className="rounded-md border border-border bg-muted/40 px-2 py-1 text-micro text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              title={`${table.name} (${table.columns.map((c) => c.name).join(", ")})`}
            >
              {table.name}
            </button>
          ))}
        </div>
      ) : null}

      {/* SQL 编辑器 */}
      <div className="flex-shrink-0">
        <SqlQueryPanel
          sql={sql}
          onSqlChange={onSqlChange}
          queryLimitInput={queryLimitInput}
          onLimitChange={onLimitChange}
          runningAction={running ? "query" : null}
          canExecute={false}
          disabled={!resourceId || loadingTables}
          error={error}
          onRunQuery={onRunQuery}
          onRunExecute={() => {}}
        />
      </div>

      {/* 结果表格 */}
      <div className="min-h-0 flex-1 overflow-hidden flex flex-col">
        {result ? (
          <>
            <div className="min-h-0 flex-1 overflow-auto">
              {result.columns.length === 0 ? (
                <div className="px-4 py-6 text-center text-micro text-muted-foreground">
                  查询已执行，但当前结果没有可展示的列。
                </div>
              ) : (
                <div className="overflow-auto">
                  <table className="w-full text-left text-micro" style={{ tableLayout: "auto" }}>
                    <thead className="sticky top-0 bg-muted/60">
                      <tr>
                        {result.columns.map((column, colIdx) => (
                          <th
                            key={`h-${colIdx}`}
                            className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground whitespace-nowrap"
                          >
                            {column}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, index) => (
                        <tr key={`${index}`} className="border-b border-border/60 last:border-b-0">
                          {result.columns.map((column, columnIndex) => {
                            const value = row[column];
                            const display =
                              value === null || value === undefined
                                ? "NULL"
                                : typeof value === "string"
                                  ? value
                                  : typeof value === "number" || typeof value === "boolean"
                                    ? String(value)
                                    : JSON.stringify(value);
                            return (
                              <td
                                key={`c${columnIndex}-r${index}`}
                                className="px-2 py-1.5 align-top font-mono text-nano text-foreground max-w-[240px] truncate"
                                title={display}
                              >
                                {display}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div className="flex-shrink-0 border-t border-border px-3 py-1.5 flex items-center gap-3 text-micro text-muted-foreground bg-muted/20">
              <span className="flex items-center gap-1">
                <span className="font-medium text-foreground">{result.row_count}</span>
                <span>行</span>
              </span>
            </div>
          </>
        ) : (
          <div className="flex h-full items-center justify-center px-6 text-center text-caption text-muted-foreground">
            执行 SQL 后结果将显示在这里。
          </div>
        )}
      </div>
    </div>
  );
}
