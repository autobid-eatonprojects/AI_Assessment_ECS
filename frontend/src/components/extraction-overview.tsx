"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ExtractionStatusBadge } from "@/components/extraction-status-badge";
import { api } from "@/lib/api";

interface Props {
  projectId: string;
  documentId: string;
  documentStatus: string;
}

export function ExtractionOverview({ projectId, documentId, documentStatus }: Props) {
  const { data } = useQuery({
    queryKey: ["extraction-overview", projectId, documentId],
    queryFn: () => api.getExtractionOverview(projectId, documentId),
    refetchInterval: () => (documentStatus === "extracting" ? 2_000 : false),
    enabled: ["extracting", "ready"].includes(documentStatus),
  });

  if (!data || data.total_pages === 0) return null;

  const allReady = data.pages_ready === data.total_pages && data.pages_failed === 0;

  return (
    <div className="rounded-md border bg-muted/30 p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium">Vision pre-pass</h3>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {!allReady && (
            <span>
              {data.pages_ready}/{data.total_pages} ready
              {data.pages_extracting > 0 && ` · ${data.pages_extracting} extracting`}
              {data.pages_failed > 0 && ` · ${data.pages_failed} failed`}
            </span>
          )}
          {allReady && <span>{data.total_pages} pages extracted</span>}
          <span>·</span>
          <span title="Total Anthropic cost for vision pre-pass">
            ${data.total_cost_usd.toFixed(3)}
          </span>
        </div>
      </div>
      {/* per-page mini status grid */}
      <div className="flex flex-wrap gap-1">
        {data.pages.map((p) => (
          <Link
            key={p.page_number}
            href={`/projects/${projectId}/documents/${documentId}/pages/${p.page_number}`}
            className="group inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] hover:bg-background"
            title={`${p.sheet_number ?? "page " + p.page_number}: ${p.schedule_count} schedules, ${p.note_count} notes, ${p.entity_count} entities`}
          >
            <span
              className={`inline-block size-1.5 rounded-full ${
                p.status === "ready"
                  ? "bg-emerald-500"
                  : p.status === "extracting"
                    ? "bg-blue-500 animate-pulse"
                    : p.status === "failed"
                      ? "bg-red-500"
                      : "bg-zinc-400"
              }`}
            />
            <span className="font-mono text-muted-foreground">
              {p.sheet_number ?? `p${p.page_number}`}
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
