"use client";

import { use, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  Layers,
  Search,
  ArrowRightLeft,
} from "lucide-react";
import { toast } from "sonner";
import { AuthGuard } from "@/components/auth-guard";
import { EvidenceTierBadge } from "@/components/evidence-tier-badge";
import { ExtractionMethodBadge } from "@/components/extraction-method-badge";
import { NarrativeMarkdown } from "@/components/narrative-markdown";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";
import type { TradePackage } from "@/lib/types";
import { cn } from "@/lib/utils";

function PackageCard({
  pkg,
  onClick,
}: {
  pkg: TradePackage;
  onClick: () => void;
}) {
  const bilateralPct =
    pkg.item_count > 0 ? (pkg.bilateral_count / pkg.item_count) * 100 : 0;
  const conf = pkg.avg_confidence ?? 0;
  return (
    <button
      type="button"
      onClick={onClick}
      className="group relative flex flex-col gap-3 rounded-lg border bg-card p-4 text-left transition-all hover:border-primary/40 hover:shadow-sm"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <Layers className="size-3.5 text-muted-foreground" />
            <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
              {pkg.bundling_rule_source === "override" ? "Override" : "AGC default"}
            </span>
          </div>
          <h3 className="mt-1 truncate text-sm font-semibold">
            {pkg.package_label}
          </h3>
          {pkg.csi_divisions && pkg.csi_divisions.length > 0 && (
            <div className="mt-0.5 text-xs font-mono text-muted-foreground">
              Div {pkg.csi_divisions.join(", ")}
            </div>
          )}
        </div>
        <span className="rounded-full bg-secondary px-2 py-0.5 text-xs font-semibold tabular-nums">
          {pkg.item_count}
        </span>
      </div>

      <div className="space-y-1 text-xs text-muted-foreground">
        <div className="flex items-center justify-between">
          <span>Bilateral coverage</span>
          <span className="tabular-nums">{Math.round(bilateralPct)}%</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-full rounded-full",
              bilateralPct >= 50
                ? "bg-emerald-500"
                : bilateralPct >= 25
                  ? "bg-amber-500"
                  : "bg-rose-500",
            )}
            style={{ width: `${bilateralPct}%` }}
          />
        </div>
        <div className="flex items-center justify-between pt-1">
          <span>Avg confidence</span>
          <span className="tabular-nums">{Math.round(conf * 100)}%</span>
        </div>
      </div>
    </button>
  );
}

function PackageDetail({
  projectId,
  packageId,
  onBack,
  packages,
}: {
  projectId: string;
  packageId: string;
  onBack: () => void;
  packages: TradePackage[];
}) {
  const qc = useQueryClient();
  const { data: detail, isLoading } = useQuery({
    queryKey: ["package", projectId, packageId],
    queryFn: () => api.getPackageDetail(projectId, packageId),
  });

  const move = useMutation({
    mutationFn: ({
      itemId,
      target_package_id,
    }: {
      itemId: string;
      target_package_id: string;
    }) =>
      api.movePackageItem(projectId, packageId, itemId, {
        target_package_id,
      }),
    onSuccess: () => {
      toast.success("Item moved");
      qc.invalidateQueries({ queryKey: ["package", projectId] });
      qc.invalidateQueries({ queryKey: ["packages", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading || !detail) {
    return (
      <div className="text-sm text-muted-foreground">Loading package…</div>
    );
  }

  const otherPackages = packages.filter((p) => p.id !== packageId);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <button
            type="button"
            onClick={onBack}
            className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="size-4" /> All packages
          </button>
          <h2 className="text-2xl font-semibold">{detail.package_label}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {detail.csi_divisions && detail.csi_divisions.length > 0
              ? `Div ${detail.csi_divisions.join(", ")}`
              : "No divisions claimed"}{" "}
            · {detail.item_count} items · source:{" "}
            <span className="font-mono">{detail.bundling_rule_source}</span>
          </p>
        </div>
      </div>

      {detail.narrative_md && (
        <Card className="border-primary/20 bg-primary/5">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              Bid invitation cover letter
              <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-primary">
                Haiku-drafted
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                onClick={() => {
                  void navigator.clipboard.writeText(detail.narrative_md ?? "");
                  toast.success("Narrative copied to clipboard");
                }}
              >
                Copy
              </Button>
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <NarrativeMarkdown source={detail.narrative_md} />
          </CardContent>
        </Card>
      )}

      {detail.items_by_section.map((section) => (
        <Card key={section.csi_section}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-semibold">
              <span className="font-mono text-muted-foreground">
                {section.csi_section}
              </span>{" "}
              {section.section_title ?? ""}
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {section.items.length} item
                {section.items.length === 1 ? "" : "s"}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {section.items.map((it) => (
              <div
                key={it.id}
                className="flex items-start justify-between gap-3 border-b py-2 last:border-b-0 last:pb-0"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <EvidenceTierBadge tier={it.evidence_tier} />
                    <ExtractionMethodBadge method={it.extraction_method} />
                    <span className="font-mono text-muted-foreground">
                      {it.csi_code}
                    </span>
                    <span className="text-muted-foreground">
                      conf {(it.confidence * 100).toFixed(0)}%
                    </span>
                    {it.bilateral_evidence === true && (
                      <span
                        title="Both spec AND drawing evidence cited"
                        className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300"
                      >
                        Bilateral ✓
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-sm">{it.description}</p>
                  {(it.qty_value != null || it.quantity || it.unit) && (
                    <p className="text-xs text-muted-foreground">
                      {it.qty_value != null ? (
                        <>
                          <span className="font-mono tabular-nums text-foreground">
                            {it.qty_value.toLocaleString()}
                          </span>{" "}
                          <span className="font-mono">{it.qty_uom ?? ""}</span>
                          {it.quantity && it.quantity !== String(it.qty_value) && (
                            <span className="ml-2 italic">
                              (source: {it.quantity} {it.unit ?? ""})
                            </span>
                          )}
                        </>
                      ) : (
                        <>
                          {it.quantity ?? "—"} {it.unit ?? ""}
                        </>
                      )}
                    </p>
                  )}
                </div>
                {otherPackages.length > 0 && (
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      render={
                        <Button variant="ghost" size="sm">
                          <ArrowRightLeft className="mr-1 size-3.5" /> Move
                        </Button>
                      }
                    />
                    <DropdownMenuContent align="end" className="w-56">
                      <DropdownMenuGroup>
                        <DropdownMenuLabel>Move to package</DropdownMenuLabel>
                        {otherPackages.map((p) => (
                          <DropdownMenuItem
                            key={p.id}
                            onClick={() =>
                              move.mutate({
                                itemId: it.id,
                                target_package_id: p.id,
                              })
                            }
                          >
                            {p.package_label}
                          </DropdownMenuItem>
                        ))}
                      </DropdownMenuGroup>
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function PackagesView({ projectId }: { projectId: string }) {
  const [filter, setFilter] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);

  const { data: packages, isLoading } = useQuery({
    queryKey: ["packages", projectId],
    queryFn: () => api.listPackages(projectId),
    refetchInterval: 12_000,
  });

  const filtered = useMemo(() => {
    if (!packages) return [];
    if (!filter) return packages;
    const q = filter.toLowerCase();
    return packages.filter(
      (p) =>
        p.package_label.toLowerCase().includes(q) ||
        p.package_key.toLowerCase().includes(q) ||
        (p.csi_divisions ?? []).some((d) => d.includes(q)),
    );
  }, [packages, filter]);

  if (activeId && packages) {
    return (
      <PackageDetail
        projectId={projectId}
        packageId={activeId}
        onBack={() => setActiveId(null)}
        packages={packages}
      />
    );
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Trade packages</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            AGC-style rollup of scope items into bid packages. Click a package
            to inspect its items and move items between packages as needed.
          </p>
        </div>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 opacity-50" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter by name or division…"
            className="w-64 pl-7 text-sm"
          />
        </div>
      </header>

      {isLoading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : filtered.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-10 text-center">
            <Layers className="mx-auto mb-2 size-8 text-muted-foreground" />
            <p className="text-sm font-medium">No trade packages yet</p>
            <p className="text-xs text-muted-foreground">
              Run a scope extraction to populate trade packages from
              bundling_rules.yaml.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((p) => (
            <PackageCard
              key={p.id}
              pkg={p}
              onClick={() => setActiveId(p.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function PackagesPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <PackagesView projectId={id} />
    </AuthGuard>
  );
}
