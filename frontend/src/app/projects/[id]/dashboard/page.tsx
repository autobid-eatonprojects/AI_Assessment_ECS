"use client";

import Link from "next/link";
import { use, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  ClipboardCheck,
  DollarSign,
  FileText,
  Layers,
  ListChecks,
  Receipt,
  TrendingUp,
} from "lucide-react";
import { AuthGuard } from "@/components/auth-guard";
import { CostBreakdownPanel } from "@/components/cost-breakdown-panel";
import { TrustScorePanel } from "@/components/trust-score-panel";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import type { AuditLogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

function StatCard({
  label,
  value,
  href,
  icon: Icon,
  hint,
  tone = "default",
}: {
  label: string;
  value: string | number;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  hint?: string;
  tone?: "default" | "warn" | "danger";
}) {
  return (
    <Link
      href={href}
      className={cn(
        "group flex flex-col rounded-lg border bg-card p-4 transition-all hover:border-primary/40 hover:shadow-sm",
      )}
    >
      <div className="flex items-center justify-between">
        <Icon
          className={cn(
            "size-4",
            tone === "warn" && "text-amber-500",
            tone === "danger" && "text-rose-500",
            tone === "default" && "text-muted-foreground",
          )}
        />
        <ArrowRight className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
      </div>
      <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
      {hint && <div className="mt-1 text-[10px] text-muted-foreground">{hint}</div>}
    </Link>
  );
}

// Pass C2 — merged activity feed combining user audit log + synthesized
// pipeline events (LLMCall + PageExtraction). Filterable by kind.
type ActivityEvent = {
  kind: "pipeline" | "cost" | "error" | "user";
  ts: string | null;
  title: string;
  document_id: string | null;
  ref_id: string | null;
  severity: "info" | "warn" | "error";
};

type ActivityFilter = "all" | "pipeline" | "cost" | "user" | "error";

function ActivityFeed({
  audit,
  events,
  filter,
  onFilterChange,
}: {
  audit: AuditLogEntry[];
  events: ActivityEvent[];
  filter: ActivityFilter;
  onFilterChange: (f: ActivityFilter) => void;
}) {
  // Adapt audit log entries into the same shape so we can merge.
  const merged = useMemo<ActivityEvent[]>(() => {
    const userEvents: ActivityEvent[] = audit.map((e) => ({
      kind: "user" as const,
      ts: e.created_at,
      title: `${e.actor.replace(/^user:/, "")} ${e.action} ${e.entity_type}${
        e.note ? ` — ${e.note}` : ""
      }`,
      document_id: null,
      ref_id: e.id,
      severity: "info" as const,
    }));
    const all = [...userEvents, ...events];
    all.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
    return all;
  }, [audit, events]);

  const filtered = filter === "all" ? merged : merged.filter((e) => e.kind === filter);

  const counts = {
    all: merged.length,
    pipeline: merged.filter((e) => e.kind === "pipeline").length,
    cost: merged.filter((e) => e.kind === "cost").length,
    user: merged.filter((e) => e.kind === "user").length,
    error: merged.filter((e) => e.kind === "error").length,
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {(["all", "pipeline", "cost", "user", "error"] as const).map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => onFilterChange(f)}
            className={cn(
              "rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors",
              filter === f
                ? "border-primary bg-primary/10 text-primary"
                : "border-muted-foreground/20 bg-card text-muted-foreground hover:bg-muted/50",
            )}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)} ({counts[f]})
          </button>
        ))}
      </div>
      {filtered.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">
          No activity yet for this filter.
        </p>
      ) : (
        <div className="max-h-96 divide-y overflow-y-auto pr-1">
          {filtered.map((e, i) => (
            <ActivityEventRow key={`${e.ref_id}-${i}`} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

function ActivityEventRow({ event }: { event: ActivityEvent }) {
  const dotColor =
    event.severity === "error"
      ? "bg-rose-500"
      : event.kind === "user"
        ? "bg-primary"
        : event.kind === "cost"
          ? "bg-amber-500"
          : "bg-muted-foreground/40";
  return (
    <div className="flex items-start gap-3 py-2 text-sm">
      <div className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", dotColor)} />
      <div className="flex-1 min-w-0">
        <div className="line-clamp-2">{event.title}</div>
        <div className="text-xs text-muted-foreground">
          {event.kind} · {event.ts ? formatRelativeTime(event.ts) : "—"}
        </div>
      </div>
    </div>
  );
}

function ActivityRow({ entry }: { entry: AuditLogEntry }) {
  const isUser = entry.actor.startsWith("user:");
  return (
    <div className="flex items-start gap-3 py-2 text-sm">
      <div
        className={cn(
          "mt-1 size-1.5 shrink-0 rounded-full",
          isUser ? "bg-primary" : "bg-muted-foreground/40",
        )}
      />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="font-medium">{entry.action}</span>
          <span className="text-muted-foreground">{entry.entity_type}</span>
        </div>
        <div className="text-xs text-muted-foreground">
          {entry.actor} · {formatRelativeTime(entry.created_at)}
        </div>
        {entry.note && (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {entry.note}
          </div>
        )}
      </div>
    </div>
  );
}

function Dashboard({ projectId }: { projectId: string }) {
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });
  const { data: scopeOverview } = useQuery({
    queryKey: ["scope-overview", projectId],
    queryFn: () => api.getScopeOverview(projectId),
  });
  const { data: trust } = useQuery({
    queryKey: ["trust-score", projectId],
    queryFn: () => api.getTrustScore(projectId),
    refetchInterval: 8_000,
  });
  const { data: packages } = useQuery({
    queryKey: ["packages", projectId],
    queryFn: () => api.listPackages(projectId),
    refetchInterval: 8_000,
  });
  const { data: conflicts } = useQuery({
    queryKey: ["conflicts", projectId, "open"],
    queryFn: () => api.listConflicts(projectId, "open"),
    refetchInterval: 8_000,
  });
  const { data: gapsBlock } = useQuery({
    queryKey: ["gaps", projectId, "blocker"],
    queryFn: () => api.listGaps(projectId, { status: "open", severity: ["blocker"] }),
    refetchInterval: 8_000,
  });
  const { data: gapsWarn } = useQuery({
    queryKey: ["gaps", projectId, "warn"],
    queryFn: () => api.listGaps(projectId, { status: "open", severity: ["warn"] }),
    refetchInterval: 8_000,
  });
  const { data: lowConf } = useQuery({
    queryKey: ["low-conf", projectId],
    queryFn: () => api.listLowConfidence(projectId),
    refetchInterval: 8_000,
  });
  const { data: audit } = useQuery({
    queryKey: ["audit-log", projectId, 30],
    queryFn: () => api.listAuditLog(projectId, { limit: 30 }),
    refetchInterval: 8_000,
  });
  // Pass C2 — pipeline activity events synthesized from LLMCall +
  // PageExtraction. Merges into the same card as user audit log so the
  // estimator sees BOTH "I resolved conflict X" AND "Vision pre-pass
  // ready on E3.1 ($0.05)" in chronological order.
  const { data: activityEvents } = useQuery({
    queryKey: ["activity-events", projectId, 50],
    queryFn: () => api.listActivityEvents(projectId, { limit: 50 }),
    refetchInterval: 8_000,
  });
  const [activityFilter, setActivityFilter] = useState<
    "all" | "pipeline" | "cost" | "user" | "error"
  >("all");
  const { data: llmCalls } = useQuery({
    queryKey: ["llm-calls", projectId],
    queryFn: () => api.listLLMCalls(projectId, { limit: 500 }),
    refetchInterval: 12_000,
  });

  const totalCost =
    llmCalls?.reduce((sum, c) => sum + (c.cost_usd ?? 0), 0) ?? 0;

  const docs = project
    ? project.project_document_count + project.bid_submission_count
    : 0;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{project?.name ?? "Project"}</h1>
          {project?.description && (
            <p className="mt-1 text-sm text-muted-foreground">
              {project.description}
            </p>
          )}
        </div>
        <Link
          href={`/projects/${projectId}`}
          className={buttonVariants({ variant: "outline", size: "sm" })}
        >
          Open Overview & Uploads
        </Link>
      </header>

      {/* Top stat row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Documents"
          value={docs}
          href={`/projects/${projectId}/documents`}
          icon={FileText}
        />
        <StatCard
          label="Scope items"
          value={scopeOverview?.total_items ?? 0}
          href={`/projects/${projectId}/scope`}
          icon={ListChecks}
        />
        <StatCard
          label="Trade packages"
          value={packages?.length ?? 0}
          href={`/projects/${projectId}/packages`}
          icon={Layers}
        />
        <StatCard
          label="Total LLM cost"
          value={`$${totalCost.toFixed(2)}`}
          href={`/projects/${projectId}/dashboard`}
          icon={DollarSign}
          hint={`${llmCalls?.length ?? 0} API calls logged`}
        />
      </div>

      {/* Cost breakdown — drill into total spend by stage / model / document */}
      <CostBreakdownPanel calls={llmCalls} />

      {/* Trust score + review queue depth */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <TrustScorePanel trust={trust} />
        </div>
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Review queue depth</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <Link
              href={`/projects/${projectId}/review`}
              className="flex items-center justify-between rounded-md border p-2.5 hover:bg-muted/50"
            >
              <span className="flex items-center gap-2 text-sm">
                <AlertTriangle className="size-4 text-rose-500" />
                Conflicts (open)
              </span>
              <span className="text-sm font-semibold tabular-nums">
                {conflicts?.length ?? 0}
              </span>
            </Link>
            <Link
              href={`/projects/${projectId}/review`}
              className="flex items-center justify-between rounded-md border p-2.5 hover:bg-muted/50"
            >
              <span className="flex items-center gap-2 text-sm">
                <ClipboardCheck className="size-4 text-amber-500" />
                Gaps (blocker)
              </span>
              <span className="text-sm font-semibold tabular-nums">
                {gapsBlock?.length ?? 0}
              </span>
            </Link>
            <Link
              href={`/projects/${projectId}/review`}
              className="flex items-center justify-between rounded-md border p-2.5 hover:bg-muted/50"
            >
              <span className="flex items-center gap-2 text-sm">
                <ClipboardCheck className="size-4 text-amber-500" />
                Gaps (warn)
              </span>
              <span className="text-sm font-semibold tabular-nums">
                {gapsWarn?.length ?? 0}
              </span>
            </Link>
            <Link
              href={`/projects/${projectId}/review`}
              className="flex items-center justify-between rounded-md border p-2.5 hover:bg-muted/50"
            >
              <span className="flex items-center gap-2 text-sm">
                <TrendingUp className="size-4 text-rose-500" />
                Low-confidence items
              </span>
              <span className="text-sm font-semibold tabular-nums">
                {lowConf?.length ?? 0}
              </span>
            </Link>
            <div className="pt-2">
              <Link
                href={`/projects/${projectId}/review`}
                className={buttonVariants({
                  variant: "default",
                  size: "sm",
                  className: "w-full",
                })}
              >
                Open review queue
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Recent activity — merged stream of user actions + pipeline events */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Recent activity</CardTitle>
          </CardHeader>
          <CardContent>
            <ActivityFeed
              audit={audit ?? []}
              events={activityEvents ?? []}
              filter={activityFilter}
              onFilterChange={setActivityFilter}
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Quick links</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <Link
              href={`/projects/${projectId}/scope`}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
            >
              Browse scope items by division
            </Link>
            <Link
              href={`/projects/${projectId}/packages`}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
            >
              View trade packages
            </Link>
            <Link
              href={`/projects/${projectId}/bids`}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
            >
              <span className="flex items-center gap-2">
                <Receipt className="size-3.5" /> Bid analysis
              </span>
            </Link>
            <Link
              href={`/projects/${projectId}/outputs`}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
            >
              Generated outputs (SOWs, gap reports)
            </Link>
            <Link
              href={`/projects/${projectId}/rfi`}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
            >
              RFI list (design-team punch list)
            </Link>
            <a
              href={api.linkJudgeDatasetUrl(projectId)}
              className="block rounded-md border p-2.5 hover:bg-muted/50"
              download
            >
              <span className="flex items-center gap-2">
                Download fine-tuning dataset (.jsonl)
                <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-primary">
                  P8
                </span>
              </span>
            </a>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

export default function DashboardPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <Dashboard projectId={id} />
    </AuthGuard>
  );
}
