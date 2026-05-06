"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Building2,
  CheckCircle2,
  FileText,
  Receipt,
  Search,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { AuthGuard } from "@/components/auth-guard";
import { NewProjectDialog } from "@/components/new-project-dialog";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import type { Project } from "@/lib/types";
import { cn } from "@/lib/utils";

function lifecyclePill(state: Project["lifecycle_state"]) {
  if (state === "setup") {
    return "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30";
  }
  if (state === "open-for-bids") {
    return "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30";
  }
  return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30";
}

function trustTier(score: number | null): {
  label: string;
  classes: string;
} {
  if (score === null || score === undefined) {
    return {
      label: "—",
      classes: "bg-muted text-muted-foreground border-border",
    };
  }
  if (score >= 0.8) {
    return {
      label: `${Math.round(score * 100)} GREEN`,
      classes:
        "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30",
    };
  }
  if (score >= 0.6) {
    return {
      label: `${Math.round(score * 100)} YELLOW`,
      classes:
        "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30",
    };
  }
  return {
    label: `${Math.round(score * 100)} RED`,
    classes: "bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30",
  };
}

function ProjectRow({ p }: { p: Project }) {
  const trust = trustTier(p.trust_score_latest);
  return (
    <Link
      href={`/projects/${p.id}/dashboard`}
      className="group block rounded-lg border bg-card p-4 transition-all hover:border-primary/40 hover:shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Building2 className="size-4 text-muted-foreground" />
            <h3 className="truncate text-base font-semibold">{p.name}</h3>
            <span
              className={cn(
                "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
                lifecyclePill(p.lifecycle_state),
              )}
            >
              {p.lifecycle_state.replace("-", " ")}
            </span>
          </div>
          {p.description && (
            <p className="mt-1 text-sm text-muted-foreground line-clamp-2">
              {p.description}
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <FileText className="size-3.5" />
              {p.project_document_count} docs
            </span>
            <span className="inline-flex items-center gap-1">
              <Receipt className="size-3.5" />
              {p.bid_submission_count} bids
            </span>
            <span>Updated {formatRelativeTime(p.updated_at)}</span>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <span
            className={cn(
              "inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-semibold tabular-nums",
              trust.classes,
            )}
          >
            {trust.label}
          </span>
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Trust score
          </span>
        </div>
      </div>
    </Link>
  );
}

function ProjectsList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });
  const [filter, setFilter] = useState("");
  const [stateFilter, setStateFilter] = useState<
    Project["lifecycle_state"] | "all"
  >("all");

  const filtered = useMemo(() => {
    if (!data) return [];
    return data.filter((p) => {
      const matchState =
        stateFilter === "all" || p.lifecycle_state === stateFilter;
      const matchText =
        !filter ||
        p.name.toLowerCase().includes(filter.toLowerCase()) ||
        (p.description ?? "").toLowerCase().includes(filter.toLowerCase());
      return matchState && matchText;
    });
  }, [data, filter, stateFilter]);

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading projects…</p>;
  }
  if (error) {
    return <p className="text-sm text-destructive">Failed to load projects.</p>;
  }
  if (!data || data.length === 0) {
    return (
      <Card className="border-dashed">
        <CardContent className="py-12 text-center">
          <p className="text-sm text-muted-foreground">
            No projects yet. Create your first project to get started.
          </p>
        </CardContent>
      </Card>
    );
  }

  const states: { value: typeof stateFilter; label: string }[] = [
    { value: "all", label: "All" },
    { value: "setup", label: "Setup" },
    { value: "open-for-bids", label: "Open for bids" },
    { value: "complete", label: "Complete" },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 opacity-50" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter projects…"
            className="w-64 pl-7 text-sm"
          />
        </div>
        <div className="ml-auto flex items-center gap-1 rounded-lg border bg-card p-1 text-xs">
          {states.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => setStateFilter(s.value)}
              className={cn(
                "rounded px-2 py-1 transition-colors",
                stateFilter === s.value
                  ? "bg-secondary text-secondary-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-10 text-center">
            <p className="text-sm text-muted-foreground">
              No projects match the current filter.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {filtered.map((p) => (
            <ProjectRow key={p.id} p={p} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <AuthGuard>
      <div className="space-y-5">
        <div className="flex items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">Projects</h1>
            <p className="text-sm text-muted-foreground">
              Each project holds the manuals, drawings, bids, and trade list for
              one estimating job.
            </p>
          </div>
          <NewProjectDialog />
        </div>
        <ProjectsList />
      </div>
    </AuthGuard>
  );
}
