"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  Building2,
  CheckCircle2,
  ChevronLeft,
  CircleSlash,
  ExternalLink,
  FileText,
  GitCompare,
  Receipt,
  Shield,
  Users,
  X,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { use, useMemo, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type {
  BidCoverageStatus,
  BidLevelingRow,
  VendorCoverageStats,
  VendorProfile,
  VendorSummary,
} from "@/lib/types";

function fmtUSD(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function CoveragePill({
  stats,
}: {
  stats: VendorCoverageStats | null | undefined;
}) {
  if (!stats) {
    return (
      <span className="text-[11px] text-muted-foreground">no coverage data</span>
    );
  }
  const total =
    stats.covered + stats.partial + stats.excluded + stats.not_covered;
  if (total === 0) {
    return (
      <span className="text-[11px] text-muted-foreground">no coverage data</span>
    );
  }
  const rate = stats.covered / total;
  return (
    <div className="flex items-center gap-1.5 text-[11px]">
      <span className="text-emerald-700 dark:text-emerald-400">
        ✓ {stats.covered}
      </span>
      <span className="text-amber-700 dark:text-amber-400">
        ~ {stats.partial}
      </span>
      <span className="text-red-700 dark:text-red-400">✗ {stats.excluded}</span>
      <span className="text-muted-foreground">— {stats.not_covered}</span>
      <span className="ml-1 rounded bg-muted px-1 font-mono text-[10px]">
        {(rate * 100).toFixed(0)}%
      </span>
    </div>
  );
}

function QualificationDot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium ${
        ok
          ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200"
          : "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-500"
      }`}
      title={
        ok ? `${label} uploaded` : `${label} not uploaded for this vendor`
      }
    >
      {ok ? <CheckCircle2 className="size-3" /> : <X className="size-3" />}
      {label}
    </span>
  );
}

function VendorCard({
  v,
  projectId,
  onClick,
}: {
  v: VendorSummary;
  projectId: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group block w-full rounded-lg border bg-card p-4 text-left transition-colors hover:border-primary/50 hover:bg-muted/30"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 truncate text-sm font-semibold">
            <Building2 className="size-4 shrink-0 text-violet-600" />
            {v.canonical_vendor}
          </h3>
          {v.primary_csi_divisions.length > 0 && (
            <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
              CSI: {v.primary_csi_divisions.join(", ")}
            </p>
          )}
          {v.aliases.length > 1 && (
            <p
              className="mt-0.5 truncate text-[10px] italic text-muted-foreground"
              title={v.aliases.join(" · ")}
            >
              also seen as: {v.aliases.filter((a) => a !== v.canonical_vendor).slice(0, 2).join(", ")}
              {v.aliases.length > 3 ? ` +${v.aliases.length - 3}` : ""}
            </p>
          )}
        </div>
        <div className="text-right">
          <p className="font-mono text-base font-semibold">
            {fmtUSD(v.bid_total_usd)}
          </p>
          {v.has_priced_bid && (
            <p className="text-[10px] text-muted-foreground">
              {v.line_item_count} lines · {v.exclusion_count} excl
            </p>
          )}
        </div>
      </div>

      <div className="mb-2">
        <CoveragePill stats={v.coverage} />
      </div>

      <div className="flex flex-wrap gap-1">
        <QualificationDot ok={v.qualifications.has_license_or_insurance} label="License" />
        <QualificationDot ok={v.qualifications.has_safety_manual} label="Safety" />
        <QualificationDot ok={v.qualifications.has_contractor_info} label="Info" />
      </div>

      <p className="mt-2 text-[10px] text-muted-foreground">
        {v.document_count} document{v.document_count === 1 ? "" : "s"}
        {!v.has_priced_bid && " · no priced bid (qualification only)"}
      </p>
    </button>
  );
}

function VendorProfileDrawer({
  projectId,
  vendor,
  onClose,
}: {
  projectId: string;
  vendor: string;
  onClose: () => void;
}) {
  const profile = useQuery({
    queryKey: ["vendor-profile", projectId, vendor],
    queryFn: () => api.getVendorProfile(projectId, vendor),
  });

  return (
    <div className="fixed inset-y-0 right-0 z-40 flex w-full max-w-2xl flex-col border-l bg-background shadow-2xl">
      <header className="flex items-start justify-between gap-2 border-b p-4">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <Building2 className="size-5 text-violet-600" />
            {vendor}
          </h2>
          {profile.data && profile.data.aliases.length > 1 && (
            <p
              className="mt-1 text-xs italic text-muted-foreground"
              title={profile.data.aliases.join(" · ")}
            >
              auto-grouped from: {profile.data.aliases.join(" · ")}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Close"
        >
          <X className="size-5" />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {profile.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : profile.data ? (
          <ProfileContent p={profile.data} />
        ) : (
          <p className="text-sm text-destructive">Failed to load profile.</p>
        )}
      </div>
    </div>
  );
}

function ProfileContent({ p }: { p: VendorProfile }) {
  return (
    <div className="space-y-6">
      <section>
        <div className="mb-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">CSI divisions</span>
          <span className="font-mono">
            {p.primary_csi_divisions.length > 0
              ? p.primary_csi_divisions.join(", ")
              : "—"}
          </span>
        </div>
        <div className="mb-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">Bid total</span>
          <span className="font-mono font-semibold">
            {fmtUSD(p.bid_total_usd)}
          </span>
        </div>
        <div className="mb-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">
            Lines · inclusions · exclusions
          </span>
          <span className="font-mono">
            {p.line_item_count} · {p.inclusion_count} · {p.exclusion_count}
          </span>
        </div>
        {p.coverage && (
          <div className="mt-2 flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Scope coverage</span>
            <CoveragePill stats={p.coverage} />
          </div>
        )}
      </section>

      {p.line_items.length > 0 && (
        <section>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            <Receipt className="size-3.5" /> What they DO ({p.line_items.length})
          </h4>
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b">
                <th className="p-1.5 text-left font-medium">Description</th>
                <th className="p-1.5 text-right font-medium">Qty</th>
                <th className="p-1.5 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {p.line_items.map((ln) => (
                <tr key={ln.id} className="border-b last:border-b-0">
                  <td className="p-1.5">{ln.description}</td>
                  <td className="p-1.5 text-right font-mono text-muted-foreground">
                    {ln.quantity ?? "—"}
                    {ln.unit ? ` ${ln.unit}` : ""}
                  </td>
                  <td className="p-1.5 text-right font-mono">
                    {fmtUSD(ln.total_price_usd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {p.inclusions.length > 0 && (
        <section>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 className="size-3.5" /> Includes ({p.inclusions.length})
          </h4>
          <ul className="space-y-1 text-xs">
            {p.inclusions.map((i) => (
              <li
                key={i.id}
                className="rounded bg-emerald-50 px-2 py-1 dark:bg-emerald-950/30"
              >
                {i.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {p.exclusions.length > 0 && (
        <section>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-red-700 dark:text-red-400">
            <XCircle className="size-3.5" /> What they DO NOT do ({p.exclusions.length})
          </h4>
          <ul className="space-y-1 text-xs">
            {p.exclusions.map((e) => (
              <li
                key={e.id}
                className="rounded bg-red-50 px-2 py-1 dark:bg-red-950/30"
              >
                {e.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          <Shield className="size-3.5" /> Qualifications
        </h4>
        <ul className="space-y-1 text-xs">
          <QualLine ok={p.qualifications.has_license_or_insurance} count={p.qualifications.license_or_insurance_count} label="License / insurance" />
          <QualLine ok={p.qualifications.has_safety_manual} count={p.qualifications.safety_manual_count} label="Safety manual / training" />
          <QualLine ok={p.qualifications.has_contractor_info} count={p.qualifications.contractor_info_count} label="Contractor information" />
        </ul>
        {!p.qualifications.is_complete && (
          <p className="mt-2 flex items-start gap-1 text-[11px] text-amber-700 dark:text-amber-400">
            <AlertCircle className="mt-0.5 size-3" />
            Incomplete qualification package — request missing docs before award.
          </p>
        )}
      </section>

      <section>
        <h4 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          <FileText className="size-3.5" /> Documents ({p.documents.length})
        </h4>
        <ul className="space-y-1 text-xs">
          {p.documents.map((d) => (
            <li key={d.id} className="flex items-start justify-between gap-2 rounded border p-2">
              <div className="min-w-0 flex-1">
                <p className="truncate">{d.filename}</p>
                <p className="text-[10px] text-muted-foreground">
                  {d.doc_type ?? "?"} ·{" "}
                  {d.classification_confidence
                    ? (d.classification_confidence * 100).toFixed(0) + "%"
                    : "—"}{" "}
                  · {d.processing_status}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function QualLine({
  ok,
  count,
  label,
}: {
  ok: boolean;
  count: number;
  label: string;
}) {
  return (
    <li className="flex items-center gap-2">
      {ok ? (
        <CheckCircle2 className="size-3.5 text-emerald-600" />
      ) : (
        <CircleSlash className="size-3.5 text-zinc-400" />
      )}
      <span className={ok ? "" : "text-muted-foreground"}>
        {label}
        {ok && count > 1 ? ` (${count} docs)` : ok ? "" : " — not uploaded"}
      </span>
    </li>
  );
}

function StatusGlyph({ status }: { status: BidCoverageStatus }) {
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
    not_applicable: ["bg-zinc-100 text-zinc-500 dark:bg-zinc-900/40", " "],
  };
  const [cls, glyph] = map[status];
  return (
    <span
      className={`inline-flex h-5 w-5 items-center justify-center rounded text-xs font-bold ${cls}`}
    >
      {glyph}
    </span>
  );
}

function BidLevelingTab({ projectId }: { projectId: string }) {
  const [division, setDivision] = useState<string | null>(null);

  // Get list of divisions any vendor bids on
  const vendors = useQuery({
    queryKey: ["vendors", projectId],
    queryFn: () => api.listVendors(projectId),
  });
  const divisions = useMemo(() => {
    const all = new Set<string>();
    for (const v of vendors.data ?? []) {
      for (const d of v.primary_csi_divisions) all.add(d);
    }
    return Array.from(all).sort();
  }, [vendors.data]);

  const leveling = useQuery({
    queryKey: ["bid-leveling", projectId, division],
    queryFn: () => api.getBidLeveling(projectId, division ?? undefined),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">Filter by CSI division:</span>
        <button
          type="button"
          onClick={() => setDivision(null)}
          className={`rounded px-2 py-1 ${division === null ? "bg-violet-600 text-white" : "border bg-card hover:bg-muted"}`}
        >
          All
        </button>
        {divisions.map((d) => (
          <button
            key={d}
            type="button"
            onClick={() => setDivision(d)}
            className={`rounded px-2 py-1 ${division === d ? "bg-violet-600 text-white" : "border bg-card hover:bg-muted"}`}
          >
            {d}
          </button>
        ))}
      </div>

      {leveling.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : !leveling.data || leveling.data.rows.length === 0 ? (
        <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
          {division
            ? `No bid leveling rows for division ${division}.`
            : "No bid leveling data — run bid analysis first."}
        </p>
      ) : (
        <LevelingTable
          vendors={leveling.data.vendors}
          rows={leveling.data.rows}
        />
      )}
    </div>
  );
}

function LevelingTable({
  vendors,
  rows,
}: {
  vendors: string[];
  rows: BidLevelingRow[];
}) {
  return (
    <div className="overflow-auto rounded-lg border bg-card max-h-[70vh]">
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-card">
          <tr>
            <th className="sticky left-0 z-10 min-w-[300px] border-b bg-card p-2 text-left font-medium">
              Scope item
            </th>
            {vendors.map((v) => (
              <th
                key={v}
                className="border-b border-l p-2 text-left font-medium"
                title={v}
              >
                <div className="max-w-[140px] truncate">{v}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.scope_item_id} className="hover:bg-muted/20">
              <td className="sticky left-0 max-w-[320px] border-b bg-card p-2 align-top">
                <div className="line-clamp-2" title={r.description}>
                  {r.description}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                  <span className="font-mono">{r.csi_code}</span>
                  {r.quantity && (
                    <span>
                      {r.quantity}
                      {r.unit ? ` ${r.unit}` : ""}
                    </span>
                  )}
                </div>
              </td>
              {r.cells.map((c) => (
                <td
                  key={c.vendor}
                  className="border-b border-l p-1.5 align-top"
                  title={c.reasoning ?? ""}
                >
                  <div className="flex items-start gap-1">
                    <StatusGlyph status={c.status} />
                    <div className="min-w-0 flex-1">
                      {c.matched_line_description && (
                        <p className="line-clamp-1 text-[10px]">
                          {c.matched_line_description}
                        </p>
                      )}
                      {c.matched_line_total_usd != null && (
                        <p className="font-mono text-[10px] text-muted-foreground">
                          {fmtUSD(c.matched_line_total_usd)}
                        </p>
                      )}
                    </div>
                  </div>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VendorsView({ projectId }: { projectId: string }) {
  const vendors = useQuery({
    queryKey: ["vendors", projectId],
    queryFn: () => api.listVendors(projectId),
  });
  const [openVendor, setOpenVendor] = useState<string | null>(null);

  return (
    <>
      <header className="mb-6">
        <Link
          href={`/projects/${projectId}`}
          className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to project
        </Link>
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Users className="size-6 text-violet-600" /> Vendors &amp; Bid Leveling
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Auto-grouped from {vendors.data?.length ?? 0} bidders. Click any
          card to see what they do, what they don&rsquo;t, and their
          qualifications. Switch tabs for side-by-side bid leveling.
        </p>
      </header>

      <Tabs defaultValue="profiles">
        <TabsList>
          <TabsTrigger value="profiles">
            <Users className="size-4" />
            <span className="ml-1.5">
              Profiles ({vendors.data?.length ?? 0})
            </span>
          </TabsTrigger>
          <TabsTrigger value="leveling">
            <GitCompare className="size-4" />
            <span className="ml-1.5">Bid leveling</span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="profiles" className="pt-4">
          {vendors.isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : !vendors.data || vendors.data.length === 0 ? (
            <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
              No vendors yet. Upload bid documents and run bid analysis.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {vendors.data.map((v) => (
                <VendorCard
                  key={v.canonical_vendor}
                  v={v}
                  projectId={projectId}
                  onClick={() => setOpenVendor(v.canonical_vendor)}
                />
              ))}
            </div>
          )}
        </TabsContent>

        <TabsContent value="leveling" className="pt-4">
          <BidLevelingTab projectId={projectId} />
        </TabsContent>
      </Tabs>

      {openVendor && (
        <>
          <div
            className="fixed inset-0 z-30 bg-black/40"
            onClick={() => setOpenVendor(null)}
            aria-hidden
          />
          <VendorProfileDrawer
            projectId={projectId}
            vendor={openVendor}
            onClose={() => setOpenVendor(null)}
          />
        </>
      )}
    </>
  );
}

export default function VendorsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl flex-1 p-6">
        <VendorsView projectId={id} />
      </main>
    </AuthGuard>
  );
}
