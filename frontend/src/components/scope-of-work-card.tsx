"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ListChecks, Loader2, Play, RefreshCw } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

interface Props {
  projectId: string;
}

export function ScopeOfWorkCard({ projectId }: Props) {
  const qc = useQueryClient();

  const overview = useQuery({
    queryKey: ["scope-overview", projectId],
    queryFn: () => api.getScopeOverview(projectId),
    refetchInterval: (q) => {
      const r = q.state.data?.latest_run;
      return r && r.status === "running" ? 4_000 : false;
    },
  });

  const start = useMutation({
    mutationFn: () => api.startScopeRun(projectId),
    onSuccess: () => {
      toast.success("Scope extraction started — watch the progress in this card");
      qc.invalidateQueries({ queryKey: ["scope-overview", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const data = overview.data;
  const run = data?.latest_run;
  const isRunning = run?.status === "running";
  // Once sections_completed catches up to sections_total but status is still
  // "running", the pipeline is past extraction and inside the post-extraction
  // stages (link_judge, conflicts, bundling, gaps, trust score). The progress
  // bar otherwise sits stuck at 100% with the "Extracting…" label, which is
  // confusing. Detect that and switch to "Finalizing…".
  const isFinalizing =
    !!run &&
    isRunning &&
    (run.sections_total ?? 0) > 0 &&
    (run.sections_completed ?? 0) >= (run.sections_total ?? 0);
  const stageLabel = isFinalizing ? "Finalizing…" : "Extracting…";
  // Prefer the overview's live ScopeItem count over run.items_after_dedupe,
  // which is only set once the dedupe stage completes (i.e. zero during the
  // extraction phase even though items are persisted continuously).
  const liveItemCount = data?.total_items ?? run?.items_after_dedupe ?? 0;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <ListChecks className="size-4 text-blue-600" />
            Scope of Work
          </h3>
          <p className="text-xs text-muted-foreground">
            Multi-query EVE extraction across relevant CSI divisions, validated
            with 3-vote majority. Phase 4.3.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && data.total_items > 0 && (
            <Link
              href={`/projects/${projectId}/scope`}
              className="inline-flex h-8 items-center gap-1 rounded-md border bg-background px-3 text-sm hover:bg-muted"
            >
              Open scope explorer
              <ArrowRight className="size-3.5" />
            </Link>
          )}
          <Button
            onClick={() => start.mutate()}
            size="sm"
            disabled={start.isPending || isRunning}
          >
            {isRunning || start.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : run ? (
              <RefreshCw className="size-4" />
            ) : (
              <Play className="size-4" />
            )}
            <span className="ml-1.5">
              {isRunning
                ? stageLabel
                : run
                  ? "Re-generate"
                  : "Generate scope"}
            </span>
          </Button>
        </div>
      </div>

      {!run ? (
        <p className="rounded-md border border-dashed py-3 text-center text-xs text-muted-foreground">
          Profile the project + filter trades, then click Generate scope.
        </p>
      ) : isRunning ? (
        <div>
          <div className="mb-2 flex items-center justify-between text-xs">
            <span>
              {run.sections_completed}/{run.sections_total} stages ·{" "}
              {liveItemCount} items so far · ${run.total_cost_usd.toFixed(3)}
            </span>
            <span className="text-muted-foreground">
              {isFinalizing
                ? "post-processing"
                : `~${Math.max(1, Math.round(((run.sections_total - run.sections_completed) * 30) / 60))} min remaining`}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-blue-100 dark:bg-blue-950/40">
            <div
              className={
                isFinalizing
                  ? "h-full animate-pulse bg-blue-600"
                  : "h-full bg-blue-600 transition-all"
              }
              style={{
                width: `${(run.sections_completed / Math.max(1, run.sections_total)) * 100}%`,
              }}
            />
          </div>
        </div>
      ) : run.status === "complete" ? (
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <p className="text-xs text-muted-foreground">Items</p>
            <p className="font-mono font-semibold">{data?.total_items ?? 0}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Divisions</p>
            <p className="font-mono font-semibold">
              {data?.by_division.length ?? 0}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Cost</p>
            <p className="font-mono font-semibold">
              ${run.total_cost_usd.toFixed(2)}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Last run</p>
            <p className="font-mono text-xs">
              {formatRelativeTime(run.completed_at ?? run.started_at)}
            </p>
          </div>
        </div>
      ) : run.status === "failed" ? (
        <div className="rounded-md bg-red-50 p-3 text-sm text-red-900 dark:bg-red-950/30 dark:text-red-200">
          Run failed: {run.error ?? "(no error message)"}
        </div>
      ) : null}
    </div>
  );
}
