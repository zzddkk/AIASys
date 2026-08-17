import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Download,
  Loader2,
  Terminal,
  User,
  X,
  Monitor,
  Server,
} from "lucide-react";
import { apiRequest } from "@/lib/api/httpClient";
import { formatVersionLabel } from "@/lib/version";

interface SidebarHeaderProps {
  sessionId?: string;
  sessionTitle?: string | null;
  isExporting: boolean;
  boundLeadSessionId?: string | null;
  onSwitchToLeadSession?: () => void;
  onExport: () => void;
  onClose: () => void;
}

export const SidebarHeader: React.FC<SidebarHeaderProps> = ({
  sessionId,
  sessionTitle: _sessionTitle,
  isExporting,
  boundLeadSessionId,
  onSwitchToLeadSession,
  onExport,
  onClose,
}) => {
  const canSwitchToLead = Boolean(boundLeadSessionId) && Boolean(onSwitchToLeadSession);
  void _sessionTitle;

  const [backendVersion, setBackendVersion] = useState<string>("-");
  const [frontendVersion, setFrontendVersion] = useState<string>("-");
  const [showVersionDetails, setShowVersionDetails] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const fetchVersion = async () => {
      try {
        const data = await apiRequest<{ version?: string }>("/health");
        if (!cancelled) {
          setBackendVersion(formatVersionLabel(data.version));
        }
      } catch {
        if (!cancelled) {
          setBackendVersion("-");
        }
      }
    };
    void fetchVersion();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const fetchFrontendVersion = async () => {
      try {
        const pkg = (await import("../../../../package.json")) as {
          version?: string;
        };
        if (!cancelled) {
          setFrontendVersion(formatVersionLabel(pkg.version));
        }
      } catch {
        if (!cancelled) {
          setFrontendVersion("-");
        }
      }
    };
    void fetchFrontendVersion();
    return () => {
      cancelled = true;
    };
  }, []);

  const toggleVersionDetails = useCallback(() => {
    setShowVersionDetails((prev) => !prev);
  }, []);

  return (
    <div className="border-b border-border bg-muted">
      <div className="flex items-center justify-between px-4 py-2.5 pl-6">
        <div className="flex items-center gap-2">
          <Terminal size={15} className="text-foreground" />
          <h2 className="text-sm font-semibold text-foreground">当前工作区</h2>
          <button
            onClick={toggleVersionDetails}
            className="text-micro text-muted-foreground hover:text-foreground transition-colors cursor-pointer px-1.5 py-0.5 rounded hover:bg-accent"
            title="查看版本信息"
          >
            {backendVersion}
          </button>
        </div>
        <div className="flex items-center gap-0.5">
          {sessionId ? (
            <button
              onClick={onExport}
              disabled={isExporting}
              className={`flex items-center gap-1 rounded-md p-1.5 transition-all ${
                isExporting
                  ? "cursor-not-allowed text-muted-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground"
              }`}
              title="导出当前工作区为 ZIP"
            >
              {isExporting ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <Download size={15} />
              )}
            </button>
          ) : null}
          <button
            onClick={onClose}
            className="p-1.5 text-muted-foreground transition-colors hover:text-foreground"
            aria-label="Close Sidebar"
          >
            <X size={18} />
          </button>
        </div>
      </div>

      {showVersionDetails ? (
        <div className="px-4 pb-3 pl-6 space-y-2 border-t border-border">
          <div className="flex items-center justify-between py-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Server className="w-3.5 h-3.5" />
              <span>服务端版本</span>
            </div>
            <code className="text-xs font-mono">{backendVersion}</code>
          </div>
          <div className="flex items-center justify-between py-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Monitor className="w-3.5 h-3.5" />
              <span>客户端版本</span>
            </div>
            <code className="text-xs font-mono">{frontendVersion}</code>
          </div>
        </div>
      ) : null}

      {sessionId && canSwitchToLead ? (
        <div
          data-testid="execution-space-session-summary"
          className="px-4 pb-3 pl-6"
        >
          <Button
            type="button"
            variant="outline"
            size="xs"
            className="gap-1.5 text-micro"
            onClick={() => {
              void onSwitchToLeadSession?.();
            }}
          >
            <User className="h-3.5 w-3.5" />
            <span>切换到主控会话</span>
          </Button>
        </div>
      ) : null}
    </div>
  );
};
