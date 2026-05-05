"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  CircleSlash,
  ExternalLink,
  FileWarning,
  Grid3x3,
  Loader2,
  MapPin,
  Receipt,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { Fragment, use, useMemo, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { CitationViewerModal } from "@/components/citation-viewer-modal";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type {
  BidCoverage,
  BidCoverageStatus,
  BidSummary,
  ScopeItem,
} from "@/lib/types";

function StatusPill({ status }: { status: BidCoverageStatus }) {
  const map: Record<BidCoverageStatus, [string, string]> = {
    covered: [
      "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
      "✓",
    ],
    partial: [
      "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
      "~",
    ],
    excluded: [
      "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200",
      "✗",
    ],
    not_covered: [
      "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-400",
      "—",
    ],
    not_applicable: ["bg-zinc-100 text-zinc-500 dark:bg-zinc-900", " "],
  };
  const [cls, glyph] = map[status];
  return (
    <span
      className={`inline-flex h-6 w-6 items-center justify-center rounded text-xs font-semibold ${cls}`}
    >
      {glyph}
    </span>
  );
}

function CoverageMatrix({
  projectId,
  scopeItems,
  bids,
  coverage,
}: {
  projectId: string;
  scopeItems: ScopeItem[];
  bids: BidSummary[];
  coverage: BidCoverage[];
}) {
  // Index coverage by (scope_item_id, bid_document_id) for O(1) lookup
  const coverageIndex = useMemo(() => {
    const m = new Map<string, BidCoverage>();
    for (const c of coverage) {
      m.set(`${c.scope_item_id}:${c.bid_document_id}`, c);
    }
    return m;
  }, [coverage]);

  const [selected, setSelected] = useState<{
    scopeItem: ScopeItem;
    bid: BidSummary;
    cov: BidCoverage | null;
  } | null>(null);

  // Group scope items by csi_division for sticky section headers
  const byDivision = useMemo(() => {
    const groups: Record<string, ScopeItem[]> = {};
    for (const it of scopeItems) {
      const k = `${it.csi_division} - ${it.division_label}`;
      if (!groups[k]) groups[k] = [];
      groups[k].push(it);
    }
    return groups;
  }, [scopeItems]);

  if (bids.length === 0) {
    return (
      <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
        No bids extracted yet. Run bid analysis to populate the matrix.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_360px]">
      <div className="overflow-auto rounded-lg border bg-card max-h-[70vh]">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 z-10 bg-card">
            <tr>
              <th className="sticky left-0 z-20 min-w-[280px] border-b bg-card p-2 text-left font-medium">
                Scope item
              </th>
              {bids.map((b) => (
                <th
                  key={b.bid_document_id}
                  className="border-b border-l p-2 text-left font-medium"
                  title={b.vendor_name ?? "(unknown vendor)"}
                >
                  <div className="max-w-[140px] truncate">
                    {b.vendor_name ?? "(unknown)"}
                  </div>
                  {b.bid_total_usd && (
                    <div className="font-mono text-[10px] text-muted-foreground">
                      ${b.bid_total_usd.toLocaleString()}
                    </div>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Object.entries(byDivision).map(([division, items]) => (
              <Fragment key={division}>
                <tr>
                  <td
                    colSpan={bids.length + 1}
                    className="bg-muted/40 px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
                  >
                    {division}
                  </td>
                </tr>
                {items.map((it) => (
                  <tr key={it.id} className="hover:bg-muted/20">
                    <td className="sticky left-0 max-w-[320px] border-b bg-card p-2 align-top">
                      <div className="truncate" title={it.description}>
                        {it.description}
                      </div>
                      <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                        <span className="font-mono">{it.csi_code}</span>
                        {it.quantity && (
                          <span>
                            {it.quantity}
                            {it.unit ? ` ${it.unit}` : ""}
                          </span>
                        )}
                      </div>
                    </td>
                    {bids.map((b) => {
                      const cov = coverageIndex.get(
                        `${it.id}:${b.bid_document_id}`,
                      );
                      const status: BidCoverageStatus = cov
                        ? cov.status
                        : "not_applicable";
                      return (
                        <td
                          key={b.bid_document_id}
                          className="cursor-pointer border-b border-l p-1 align-middle text-center hover:bg-muted/40"
                          onClick={() =>
                            setSelected({
                              scopeItem: it,
                              bid: b,
                              cov: cov ?? null,
                            })
                          }
                        >
                          <StatusPill status={status} />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      <aside className="rounded-lg border bg-card p-4">
        {selected ? (
          <CoverageDetail projectId={projectId} {...selected} />
        ) : (
          <div className="text-sm text-muted-foreground">
            Click any cell to see the coverage decision + reasoning.
          </div>
        )}
      </aside>
    </div>
  );
}

function CoverageDetail({
  projectId,
  scopeItem,
  bid,
  cov,
}: {
  projectId: string;
  scopeItem: ScopeItem;
  bid: BidSummary;
  cov: BidCoverage | null;
}) {
  const [showCitation, setShowCitation] = useState(false);
  // Prefer a citation that carries a bbox so the modal can highlight the source
  // region; fall back to the first available citation otherwise.
  const citation =
    scopeItem.citations.find((c) => c.bbox) ?? scopeItem.citations[0] ?? null;

  return (
    <div className="space-y-3 text-sm">
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          Scope item
        </p>
        <p className="font-medium">{scopeItem.description}</p>
        <p className="font-mono text-xs text-muted-foreground">
          {scopeItem.csi_code}
          {scopeItem.quantity ? ` · ${scopeItem.quantity} ${scopeItem.unit ?? ""}` : ""}
        </p>
      </div>
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          Bid
        </p>
        <p className="font-medium">{bid.vendor_name ?? "(unknown vendor)"}</p>
        {bid.bid_total_usd && (
          <p className="font-mono text-xs text-muted-foreground">
            Total ${bid.bid_total_usd.toLocaleString()}
          </p>
        )}
      </div>
      <div>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          Coverage
        </p>
        {cov ? (
          <>
            <p className="flex items-center gap-2">
              <StatusPill status={cov.status} />
              <span className="font-medium capitalize">
                {cov.status.replace("_", " ")}
              </span>
              <span className="font-mono text-xs text-muted-foreground">
                conf {Math.round(cov.confidence * 100)}%
              </span>
            </p>
            {cov.reasoning && (
              <p className="mt-2 text-sm text-muted-foreground">
                {cov.reasoning}
              </p>
            )}
            <p className="mt-2 text-[10px] text-muted-foreground">
              Judged by {cov.judge_model ?? "(unknown)"}
            </p>
          </>
        ) : (
          <p className="flex items-center gap-2 text-muted-foreground">
            <CircleSlash className="size-3.5" /> Not applicable — bid did not
            claim this CSI division.
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3 border-t pt-3 text-xs">
        {citation && (
          <button
            type="button"
            onClick={() => setShowCitation(true)}
            className="inline-flex items-center gap-1 rounded border bg-card px-2 py-1 font-medium hover:bg-muted"
          >
            <MapPin className="size-3 text-blue-600" />
            View scope source
          </button>
        )}
        <Link
          href={`/projects/${projectId}/bids?bid=${bid.bid_document_id}`}
          className="inline-flex items-center gap-1 font-medium text-violet-700 hover:underline dark:text-violet-400"
        >
          Open bid in detail tab <ExternalLink className="size-3" />
        </Link>
      </div>
      {citation && (
        <CitationViewerModal
          open={showCitation}
          onOpenChange={setShowCitation}
          projectId={projectId}
          scopeItem={scopeItem}
          citation={citation}
        />
      )}
    </div>
  );
}

function PerBidView({
  projectId,
  bids,
  initialBidId,
}: {
  projectId: string;
  bids: BidSummary[];
  initialBidId: string | null;
}) {
  const [bidId, setBidId] = useState<string | null>(
    initialBidId ?? bids[0]?.bid_document_id ?? null,
  );

  const detail = useQuery({
    queryKey: ["bid-detail", projectId, bidId],
    queryFn: () =>
      bidId ? api.getBidDetail(projectId, bidId) : Promise.resolve(null),
    enabled: !!bidId,
  });

  if (bids.length === 0) {
    return (
      <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
        No bids extracted yet.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
      <div className="space-y-1">
        {bids.map((b) => (
          <button
            type="button"
            key={b.bid_document_id}
            onClick={() => setBidId(b.bid_document_id)}
            className={`w-full rounded-md border px-3 py-2 text-left text-sm hover:bg-muted ${bidId === b.bid_document_id ? "bg-muted" : ""}`}
          >
            <div className="font-medium">{b.vendor_name ?? "(unknown)"}</div>
            <div className="font-mono text-[10px] text-muted-foreground">
              {b.bid_total_usd ? `$${b.bid_total_usd.toLocaleString()}` : "—"}{" "}
              · {b.line_item_count} lines · {b.exclusion_count} excl
            </div>
          </button>
        ))}
      </div>

      <div className="rounded-lg border bg-card p-4">
        {detail.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading bid…
          </div>
        ) : detail.data ? (
          <BidDetailView detail={detail.data} />
        ) : (
          <div className="text-sm text-muted-foreground">
            Pick a bid on the left.
          </div>
        )}
      </div>
    </div>
  );
}

function BidDetailView({
  detail,
}: {
  detail: NonNullable<Awaited<ReturnType<typeof api.getBidDetail>>>;
}) {
  const { summary, line_items, inclusions, exclusions } = detail;
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-base font-semibold">
          {summary.vendor_name ?? "(unknown vendor)"}
        </h3>
        <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground">
          {summary.bid_total_usd && (
            <span>
              Total{" "}
              <span className="font-mono font-semibold text-foreground">
                ${summary.bid_total_usd.toLocaleString()}
              </span>
            </span>
          )}
          {summary.primary_csi_divisions &&
            summary.primary_csi_divisions.length > 0 && (
              <span>CSI: {summary.primary_csi_divisions.join(", ")}</span>
            )}
          <span>
            {line_items.length} lines · {inclusions.length} incl ·{" "}
            {exclusions.length} excl
          </span>
        </div>
      </div>

      {line_items.length > 0 && (
        <section>
          <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Line items
          </h4>
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b">
                <th className="p-1.5 text-left font-medium">Description</th>
                <th className="p-1.5 text-right font-medium">Qty</th>
                <th className="p-1.5 text-right font-medium">Unit $</th>
                <th className="p-1.5 text-right font-medium">Total $</th>
              </tr>
            </thead>
            <tbody>
              {line_items.map((ln) => (
                <tr key={ln.id} className="border-b last:border-b-0">
                  <td className="p-1.5">
                    {ln.description}
                    {ln.csi_section_guess && (
                      <span className="ml-1.5 rounded bg-muted px-1 font-mono text-[10px] text-muted-foreground">
                        {ln.csi_section_guess}
                      </span>
                    )}
                  </td>
                  <td className="p-1.5 text-right font-mono">
                    {ln.quantity ?? "—"}
                    {ln.unit ? ` ${ln.unit}` : ""}
                  </td>
                  <td className="p-1.5 text-right font-mono">
                    {ln.unit_price_usd
                      ? `$${ln.unit_price_usd.toLocaleString()}`
                      : "—"}
                  </td>
                  <td className="p-1.5 text-right font-mono">
                    {ln.total_price_usd
                      ? `$${ln.total_price_usd.toLocaleString()}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {inclusions.length > 0 && (
        <section>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 className="size-3.5" /> Includes
          </h4>
          <ul className="space-y-1 text-sm">
            {inclusions.map((inc) => (
              <li key={inc.id} className="rounded bg-emerald-50 px-2 py-1 text-xs dark:bg-emerald-950/30">
                {inc.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {exclusions.length > 0 && (
        <section>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-red-700 dark:text-red-400">
            <XCircle className="size-3.5" /> Excludes
          </h4>
          <ul className="space-y-1 text-sm">
            {exclusions.map((exc) => (
              <li key={exc.id} className="rounded bg-red-50 px-2 py-1 text-xs dark:bg-red-950/30">
                {exc.text}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function GapsView({
  gaps,
}: {
  gaps: ScopeItem[];
}) {
  if (gaps.length === 0) {
    return (
      <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
        No gaps — every scope item is covered (or partially covered) by a bid.
      </p>
    );
  }
  // Group by division
  const byDivision: Record<string, ScopeItem[]> = {};
  for (const it of gaps) {
    const k = `${it.csi_division} - ${it.division_label}`;
    if (!byDivision[k]) byDivision[k] = [];
    byDivision[k].push(it);
  }
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
        <p className="flex items-center gap-2">
          <AlertTriangle className="size-4" />
          {gaps.length} scope item{gaps.length === 1 ? "" : "s"} are not covered
          by any uploaded bid. Solicit additional bids or document why these
          are excluded.
        </p>
      </div>
      {Object.entries(byDivision).map(([division, items]) => (
        <section key={division}>
          <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {division} ({items.length})
          </h4>
          <ul className="divide-y rounded-md border bg-card text-sm">
            {items.map((it) => (
              <li key={it.id} className="flex items-start justify-between gap-3 p-2">
                <div className="flex-1 min-w-0">
                  <div className="truncate font-medium">{it.description}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                    <span className="font-mono">{it.csi_code}</span>
                    {it.quantity && (
                      <span>
                        {it.quantity}
                        {it.unit ? ` ${it.unit}` : ""}
                      </span>
                    )}
                  </div>
                </div>
                <FileWarning className="size-4 shrink-0 text-amber-600" />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function BidAnalysisDashboard({ projectId }: { projectId: string }) {
  const overview = useQuery({
    queryKey: ["bid-analysis-overview", projectId],
    queryFn: () => api.getBidAnalysisOverview(projectId),
    refetchInterval: (q) => {
      const r = q.state.data?.latest_run;
      return r && r.status === "running" ? 4_000 : false;
    },
  });

  const scopeItems = useQuery({
    queryKey: ["scope-items", projectId],
    queryFn: () => api.listScopeItems(projectId),
  });

  const coverage = useQuery({
    queryKey: ["bid-coverage", projectId],
    queryFn: () => api.listBidCoverage(projectId),
  });

  const gaps = useQuery({
    queryKey: ["bid-gaps", projectId],
    queryFn: () => api.listBidGaps(projectId),
  });

  if (overview.isLoading) {
    return (
      <div className="text-sm text-muted-foreground">Loading bid analysis…</div>
    );
  }

  const data = overview.data;
  if (!data?.latest_run) {
    return (
      <div className="rounded-md border border-dashed p-6 text-sm text-muted-foreground">
        Bid analysis hasn't been run yet. Go back to the project page and click{" "}
        <strong>Run bid analysis</strong>.
      </div>
    );
  }

  const bids = data.bid_summaries;
  const initialBidId =
    typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("bid")
      : null;

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between">
        <div>
          <Link
            href={`/projects/${projectId}`}
            className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="size-4" /> Back to project
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Receipt className="size-6 text-violet-600" /> Bid Analysis
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {bids.length} bid{bids.length === 1 ? "" : "s"} · {scopeItems.data?.length ?? 0} scope items ·{" "}
            <span className="text-emerald-700 dark:text-emerald-400">
              {data.coverage_counts.covered} covered
            </span>{" "}
            ·{" "}
            <span className="text-amber-700 dark:text-amber-400">
              {data.coverage_counts.partial} partial
            </span>{" "}
            ·{" "}
            <span className="text-red-700 dark:text-red-400">
              {data.coverage_counts.excluded + data.coverage_counts.not_covered}{" "}
              gaps
            </span>{" "}
            · ${data.latest_run.total_cost_usd.toFixed(2)}
          </p>
        </div>
      </header>

      <Tabs defaultValue={initialBidId ? "per-bid" : "matrix"}>
        <TabsList>
          <TabsTrigger value="matrix">
            <Grid3x3 className="size-4" />
            <span className="ml-1.5">Coverage matrix</span>
          </TabsTrigger>
          <TabsTrigger value="per-bid">
            <Receipt className="size-4" />
            <span className="ml-1.5">Per bid</span>
          </TabsTrigger>
          <TabsTrigger value="gaps">
            <AlertTriangle className="size-4" />
            <span className="ml-1.5">
              Gaps ({gaps.data?.length ?? 0})
            </span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="matrix" className="pt-4">
          <CoverageMatrix
            projectId={projectId}
            scopeItems={scopeItems.data ?? []}
            bids={bids}
            coverage={coverage.data ?? []}
          />
        </TabsContent>
        <TabsContent value="per-bid" className="pt-4">
          <PerBidView
            projectId={projectId}
            bids={bids}
            initialBidId={initialBidId}
          />
        </TabsContent>
        <TabsContent value="gaps" className="pt-4">
          <GapsView gaps={gaps.data ?? []} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default function BidAnalysisPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl flex-1 p-6">
        <BidAnalysisDashboard projectId={id} />
      </main>
    </AuthGuard>
  );
}
