"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Download, Loader2, MapPin, Play, RefreshCw } from "lucide-react";
import Link from "next/link";
import { use, useMemo, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { CitationViewerModal } from "@/components/citation-viewer-modal";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import type { ScopeCitation, ScopeItem } from "@/lib/types";

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const cls =
    value >= 0.85
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200"
      : value >= 0.6
        ? "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200"
        : "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
      {pct}%
    </span>
  );
}

function ScopeItemDetail({
  projectId,
  item,
  onCitationClick,
}: {
  projectId: string;
  item: ScopeItem;
  onCitationClick: (c: ScopeCitation) => void;
}) {
  return (
    <div className="space-y-4">
      <div>
        <div className="mb-1 flex items-center gap-2 text-xs">
          <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono dark:bg-zinc-800">
            {item.csi_code}
          </span>
          {item.section_title && (
            <span className="text-muted-foreground">{item.section_title}</span>
          )}
          <ConfidenceBadge value={item.confidence} />
        </div>
        <h3 className="text-base font-semibold">{item.description}</h3>
      </div>

      <dl className="space-y-2 text-sm">
        {item.specification && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Specification
            </dt>
            <dd className="mt-0.5">{item.specification}</dd>
          </div>
        )}
        {item.quantity && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Quantity
            </dt>
            <dd className="mt-0.5 font-mono">
              {item.quantity} {item.unit}
            </dd>
          </div>
        )}
        {item.location && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Location
            </dt>
            <dd className="mt-0.5">{item.location}</dd>
          </div>
        )}
        {item.extraction_method && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              Source kind
            </dt>
            <dd className="mt-0.5 text-xs text-muted-foreground">
              {item.extraction_method}
            </dd>
          </div>
        )}
      </dl>

      <div>
        <h4 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
          Citations ({item.citations.length})
        </h4>
        <ul className="space-y-2">
          {item.citations.map((c) => {
            const hasBbox = !!c.bbox;
            return (
              <li key={c.id} className="rounded-md border bg-muted/30 p-2 text-xs">
                <div className="mb-1 flex items-center gap-2">
                  {c.sheet_number && (
                    <span className="rounded bg-blue-100 px-1.5 py-0.5 font-mono font-medium text-blue-900 dark:bg-blue-900/40 dark:text-blue-200">
                      {c.sheet_number}
                    </span>
                  )}
                  {c.page_number != null && (
                    <span className="text-muted-foreground">
                      page {c.page_number}
                    </span>
                  )}
                  {c.extraction_query && (
                    <span className="text-muted-foreground">
                      via {c.extraction_query}
                    </span>
                  )}
                  {c.rerank_score != null && (
                    <span className="ml-auto text-muted-foreground">
                      rerank {c.rerank_score.toFixed(2)}
                    </span>
                  )}
                </div>
                {c.excerpt && (
                  <p className="line-clamp-3 text-muted-foreground">{c.excerpt}</p>
                )}
                {c.document_id && c.page_number != null && (
                  <button
                    type="button"
                    onClick={() => onCitationClick(c)}
                    className="mt-2 inline-flex items-center gap-1 rounded border bg-card px-2 py-0.5 text-[11px] font-medium hover:bg-muted"
                    title={
                      hasBbox
                        ? "Open the source page with this citation's bbox highlighted"
                        : "Open the source page (no bbox available for this chunk type)"
                    }
                  >
                    <MapPin className="size-3 text-blue-600" />
                    {hasBbox ? "View on drawing" : "View page"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function exportToCSV(items: ScopeItem[]): string {
  const header = [
    "CSI Code",
    "Division",
    "Section",
    "Description",
    "Specification",
    "Quantity",
    "Unit",
    "Location",
    "Confidence",
    "Method",
    "Citations",
  ];
  const escape = (v: string | number | null | undefined) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const rows = items.map((i) =>
    [
      i.csi_code,
      i.division_label,
      i.section_title,
      i.description,
      i.specification,
      i.quantity,
      i.unit,
      i.location,
      i.confidence.toFixed(2),
      i.extraction_method,
      i.citations
        .map(
          (c) =>
            `${c.sheet_number ?? ""}${c.page_number ? ` p${c.page_number}` : ""}`,
        )
        .filter(Boolean)
        .join("; "),
    ]
      .map(escape)
      .join(","),
  );
  return [header.join(","), ...rows].join("\n");
}

function ScopeView({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null); // csi_division
  const [selectedItem, setSelectedItem] = useState<string | null>(null);
  const [openCitation, setOpenCitation] = useState<{
    item: ScopeItem;
    citation: ScopeCitation;
  } | null>(null);

  const overviewQuery = useQuery({
    queryKey: ["scope-overview", projectId],
    queryFn: () => api.getScopeOverview(projectId),
    refetchInterval: (q) => {
      const r = q.state.data?.latest_run;
      return r && r.status === "running" ? 4_000 : false;
    },
  });

  const itemsQuery = useQuery({
    queryKey: ["scope-items", projectId, selected],
    queryFn: () => api.listScopeItems(projectId, selected ?? undefined),
    enabled: !!overviewQuery.data?.latest_run,
  });

  const start = useMutation({
    mutationFn: () => api.startScopeRun(projectId),
    onSuccess: () => {
      toast.success("Scope extraction started — polling for progress");
      qc.invalidateQueries({ queryKey: ["scope-overview", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const overview = overviewQuery.data;
  const run = overview?.latest_run;
  const isRunning = run?.status === "running";

  const items = itemsQuery.data ?? [];
  const itemDetail = useMemo(
    () => items.find((i) => i.id === selectedItem),
    [items, selectedItem],
  );

  return (
    <>
      <Link
        href={`/projects/${projectId}`}
        className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="size-4" /> Back to project
      </Link>

      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Scope of Work</h1>
          {run ? (
            <p className="mt-1 text-sm text-muted-foreground">
              {run.status === "running"
                ? `Extracting… ${run.sections_completed}/${run.sections_total} divisions complete`
                : run.status === "complete"
                  ? `${overview?.total_items ?? 0} items across ${overview?.by_division.length ?? 0} divisions · ${formatRelativeTime(run.completed_at ?? run.started_at)} · $${run.total_cost_usd.toFixed(2)}`
                  : `Last run failed${run.error ? ": " + run.error : ""}`}
            </p>
          ) : (
            <p className="mt-1 text-sm text-muted-foreground">
              No extraction yet. Click <strong>Generate Scope of Work</strong> to start.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {items.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const csv = exportToCSV(items);
                const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = "scope-of-work.csv";
                a.click();
                URL.revokeObjectURL(url);
              }}
            >
              <Download className="size-4" />
              <span className="ml-1.5">Export CSV</span>
            </Button>
          )}
          <Button
            onClick={() => start.mutate()}
            disabled={start.isPending || isRunning}
          >
            {isRunning ? (
              <Loader2 className="size-4 animate-spin" />
            ) : run ? (
              <RefreshCw className="size-4" />
            ) : (
              <Play className="size-4" />
            )}
            <span className="ml-1.5">
              {isRunning
                ? "Extracting…"
                : run
                  ? "Re-generate"
                  : "Generate Scope of Work"}
            </span>
          </Button>
        </div>
      </div>

      {isRunning && run && (
        <div className="mb-6 rounded-lg border bg-blue-50 p-3 text-sm dark:bg-blue-950/30">
          <div className="mb-2 flex items-center justify-between">
            <span>
              {run.sections_completed}/{run.sections_total} divisions ·{" "}
              {run.items_after_dedupe} items · ${run.total_cost_usd.toFixed(3)}
            </span>
            <Loader2 className="size-4 animate-spin text-blue-600" />
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-blue-200 dark:bg-blue-900">
            <div
              className="h-full bg-blue-600 transition-all"
              style={{
                width: `${(run.sections_completed / Math.max(1, run.sections_total)) * 100}%`,
              }}
            />
          </div>
        </div>
      )}

      {overview && overview.by_division.length === 0 && !isRunning ? (
        <div className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
          {run
            ? "Run completed but produced no items — check the run config or re-run."
            : "Click Generate Scope of Work above to start."}
        </div>
      ) : (
        <div className="grid grid-cols-12 gap-4">
          {/* Left: division tree */}
          <aside className="col-span-3 space-y-1">
            <button
              type="button"
              onClick={() => setSelected(null)}
              className={`w-full rounded-md px-3 py-2 text-left text-sm hover:bg-muted ${
                selected === null ? "bg-muted font-medium" : ""
              }`}
            >
              All divisions ({overview?.total_items ?? 0})
            </button>
            {(overview?.by_division ?? []).map((d) => (
              <button
                key={d.csi_division}
                type="button"
                onClick={() => setSelected(d.csi_division)}
                className={`w-full rounded-md px-3 py-2 text-left text-sm hover:bg-muted ${
                  selected === d.csi_division ? "bg-muted font-medium" : ""
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="truncate">{d.division_label}</span>
                  <span className="ml-2 shrink-0 rounded bg-muted-foreground/20 px-1.5 py-0.5 text-[11px]">
                    {d.count}
                  </span>
                </div>
              </button>
            ))}
          </aside>

          {/* Middle: item list */}
          <div className="col-span-5 space-y-2">
            {itemsQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading items…</p>
            ) : items.length === 0 ? (
              <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
                No items in this division.
              </p>
            ) : (
              items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setSelectedItem(item.id)}
                  className={`block w-full rounded-md border bg-card p-3 text-left transition-colors hover:border-primary/50 hover:bg-muted/30 ${
                    selectedItem === item.id ? "border-primary bg-primary/5" : ""
                  }`}
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] dark:bg-zinc-800">
                      {item.csi_code}
                    </span>
                    <ConfidenceBadge value={item.confidence} />
                    {item.quantity && (
                      <span className="ml-auto font-mono text-xs text-muted-foreground">
                        {item.quantity} {item.unit}
                      </span>
                    )}
                  </div>
                  <p className="line-clamp-2 text-sm">{item.description}</p>
                </button>
              ))
            )}
          </div>

          {/* Right: detail */}
          <aside className="col-span-4 space-y-2">
            <div className="sticky top-4 rounded-md border bg-card p-4">
              {itemDetail ? (
                <ScopeItemDetail
                  projectId={projectId}
                  item={itemDetail}
                  onCitationClick={(c) =>
                    setOpenCitation({ item: itemDetail, citation: c })
                  }
                />
              ) : (
                <p className="text-sm text-muted-foreground">
                  Select an item to see details and citations.
                </p>
              )}
            </div>
          </aside>
        </div>
      )}

      {openCitation && (
        <CitationViewerModal
          open={!!openCitation}
          onOpenChange={(o) => !o && setOpenCitation(null)}
          projectId={projectId}
          scopeItem={openCitation.item}
          citation={openCitation.citation}
        />
      )}
    </>
  );
}

export default function ScopePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl flex-1 p-6">
        <ScopeView projectId={id} />
      </main>
    </AuthGuard>
  );
}
