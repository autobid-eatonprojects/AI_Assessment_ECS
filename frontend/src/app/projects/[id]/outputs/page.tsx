"use client";

import { use } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  FileText,
  FileType,
  Loader2,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import { toast } from "sonner";
import { AuthGuard } from "@/components/auth-guard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuthStore } from "@/lib/auth";
import { formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

interface OutputRow {
  audit_id: string;
  run_id: string | null;
  kind: "sow_docx" | "gap_report_pdf" | string | null;
  filename: string | null;
  size_bytes: number | null;
  item_count: number | null;
  page_count: number | null;
  package_label: string | null;
  created_at: string;
  actor: string;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function fmtSize(bytes: number | null): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

async function listOutputsRaw(projectId: string): Promise<OutputRow[]> {
  const token = useAuthStore.getState().token;
  const res = await fetch(`${API_URL}/api/projects/${projectId}/outputs`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`outputs list failed: ${res.status}`);
  return res.json();
}

async function generateOutputs(projectId: string): Promise<void> {
  const token = useAuthStore.getState().token;
  const res = await fetch(
    `${API_URL}/api/projects/${projectId}/outputs/generate`,
    {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    },
  );
  if (!res.ok) throw new Error(`generate failed: ${res.status}`);
}

function downloadHref(
  projectId: string,
  runId: string | null,
  filename: string | null,
): string | null {
  if (!runId || !filename) return null;
  return `${API_URL}/api/projects/${projectId}/outputs/download?run_id=${encodeURIComponent(runId)}&filename=${encodeURIComponent(filename)}`;
}

function OutputsView({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const { data: outputs, isLoading, refetch } = useQuery({
    queryKey: ["outputs", projectId],
    queryFn: () => listOutputsRaw(projectId),
    refetchInterval: (q) => {
      // Active polling while generation is in flight (no new rows yet)
      return 6_000;
    },
  });

  const generate = useMutation({
    mutationFn: () => generateOutputs(projectId),
    onSuccess: () => {
      toast.success("Output generation started");
      // Poll heavily for ~30 sec
      let count = 0;
      const interval = setInterval(() => {
        qc.invalidateQueries({ queryKey: ["outputs", projectId] });
        count++;
        if (count > 10) clearInterval(interval);
      }, 3_000);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const sows = (outputs ?? []).filter((o) => o.kind === "sow_docx");
  const reports = (outputs ?? []).filter((o) => o.kind === "gap_report_pdf");
  const latest = outputs && outputs.length > 0 ? outputs[0] : null;

  // Token-based auth: build a signed URL not directly clickable. Use a
  // fetch-then-blob approach for downloads so the auth header is applied.
  const downloadFile = async (row: OutputRow) => {
    const href = downloadHref(projectId, row.run_id, row.filename);
    if (!href) return;
    try {
      const token = useAuthStore.getState().token;
      const res = await fetch(href, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`download failed: ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = row.filename ?? "output";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Outputs</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Generate per-trade-package SOW Word documents and a project gap
            report PDF. Citations in the SOWs link back to the source PDF
            page in the backend viewer.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isLoading}
          >
            <RefreshCw
              className={cn("mr-1 size-3.5", isLoading && "animate-spin")}
            />
            Refresh
          </Button>
          <Button
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
          >
            {generate.isPending ? (
              <Loader2 className="mr-1 size-3.5 animate-spin" />
            ) : (
              <ScrollText className="mr-1 size-3.5" />
            )}
            Generate outputs
          </Button>
        </div>
      </header>

      {isLoading && !outputs ? (
        <p className="text-sm text-muted-foreground">Loading outputs…</p>
      ) : !outputs || outputs.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-10 text-center">
            <ScrollText className="mx-auto mb-2 size-8 text-muted-foreground" />
            <p className="text-sm font-medium">No outputs generated yet</p>
            <p className="text-xs text-muted-foreground">
              Click <strong>Generate outputs</strong> to create per-package
              SOW Word docs and a gap report PDF for the latest scope run.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          {latest && (
            <p className="text-xs text-muted-foreground">
              Latest generation: {formatRelativeTime(latest.created_at)} by {latest.actor}
            </p>
          )}

          {reports.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Gap & conflict report</CardTitle>
              </CardHeader>
              <CardContent>
                {reports.map((r) => (
                  <div
                    key={r.audit_id}
                    className="flex items-center justify-between border-b py-2 last:border-b-0 last:pb-0"
                  >
                    <div className="flex items-center gap-3">
                      <FileType className="size-4 text-rose-500" />
                      <div>
                        <div className="text-sm font-medium">{r.filename}</div>
                        <div className="text-xs text-muted-foreground">
                          {r.page_count ? `${r.page_count} pages` : ""} ·{" "}
                          {fmtSize(r.size_bytes)} ·{" "}
                          {formatRelativeTime(r.created_at)}
                        </div>
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => downloadFile(r)}
                    >
                      <Download className="mr-1 size-3.5" />
                      Download
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {sows.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  Per-trade SOW documents ({sows.length})
                </CardTitle>
              </CardHeader>
              <CardContent>
                {sows.map((s) => (
                  <div
                    key={s.audit_id}
                    className="flex items-center justify-between border-b py-2 last:border-b-0 last:pb-0"
                  >
                    <div className="flex items-center gap-3">
                      <FileText className="size-4 text-sky-500" />
                      <div>
                        <div className="text-sm font-medium">
                          {s.package_label ?? s.filename}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {s.filename} ·{" "}
                          {s.item_count
                            ? `${s.item_count} item${s.item_count === 1 ? "" : "s"}`
                            : "—"}{" "}
                          · {fmtSize(s.size_bytes)} ·{" "}
                          {formatRelativeTime(s.created_at)}
                        </div>
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => downloadFile(s)}
                    >
                      <Download className="mr-1 size-3.5" />
                      Download
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

export default function OutputsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <OutputsView projectId={id} />
    </AuthGuard>
  );
}
