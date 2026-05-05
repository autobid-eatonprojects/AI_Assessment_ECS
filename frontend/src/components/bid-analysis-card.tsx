"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Loader2, Play, Receipt, RefreshCw } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

interface Props {
  projectId: string;
}

export function BidAnalysisCard({ projectId }: Props) {
  const qc = useQueryClient();

  const overview = useQuery({
    queryKey: ["bid-analysis-overview", projectId],
    queryFn: () => api.getBidAnalysisOverview(projectId),
    refetchInterval: (q) => {
      const r = q.state.data?.latest_run;
      return r && r.status === "running" ? 4_000 : false;
    },
  });

  const start = useMutation({
    mutationFn: () => api.startBidAnalysisRun(projectId),
    onSuccess: () => {
      toast.success("Bid analysis started");
      qc.invalidateQueries({ queryKey: ["bid-analysis-overview", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const data = overview.data;
  const run = data?.latest_run;
  const isRunning = run?.status === "running";

  const coverage = data?.coverage_counts;
  const totalCoverage = coverage
    ? coverage.covered + coverage.partial + coverage.excluded + coverage.not_covered
    : 0;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Receipt className="size-4 text-violet-600" />
            Bid Analysis
          </h3>
          <p className="text-xs text-muted-foreground">
            Extract bid line items + score coverage matrix against the scope
            of work. Phase 8.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && data.bid_summaries.length > 0 && (
            <Link
              href={`/projects/${projectId}/bids`}
              className="inline-flex h-8 items-center gap-1 rounded-md border bg-background px-3 text-sm hover:bg-muted"
            >
              Open bid analysis
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
                ? "Analyzing…"
                : run
                  ? "Re-run analysis"
                  : "Run bid analysis"}
            </span>
          </Button>
        </div>
      </div>

      {!run ? (
        <p className="rounded-md border border-dashed py-3 text-center text-xs text-muted-foreground">
          Lock scope, upload bids, then click Run bid analysis.
        </p>
      ) : isRunning ? (
        <div>
          <div className="mb-2 flex items-center justify-between text-xs">
            <span>
              {run.bids_extracted}/{run.bids_total} bids extracted ·{" "}
              {run.coverage_pairs_completed}/{run.coverage_pairs_total} pairs
              scored · ${run.total_cost_usd.toFixed(3)}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-violet-100 dark:bg-violet-950/40">
            <div
              className="h-full bg-violet-600 transition-all"
              style={{
                width: `${
                  ((run.bids_extracted +
                    run.coverage_pairs_completed /
                      Math.max(1, run.coverage_pairs_total)) /
                    Math.max(1, run.bids_total + 1)) *
                  100
                }%`,
              }}
            />
          </div>
        </div>
      ) : run.status === "complete" ? (
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
          <div>
            <p className="text-xs text-muted-foreground">Bids</p>
            <p className="font-mono font-semibold">{data?.bid_summaries.length ?? 0}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Covered</p>
            <p className="font-mono font-semibold text-emerald-700 dark:text-emerald-400">
              {coverage?.covered ?? 0}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Excluded / Gap</p>
            <p className="font-mono font-semibold text-red-700 dark:text-red-400">
              {(coverage?.excluded ?? 0) + (coverage?.not_covered ?? 0)}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Cost</p>
            <p className="font-mono font-semibold">${run.total_cost_usd.toFixed(2)}</p>
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
      {run?.status === "complete" && totalCoverage > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          {totalCoverage} (scope item × bid) pairs scored ·{" "}
          {coverage?.partial ?? 0} partial ·{" "}
          {(((coverage?.covered ?? 0) / totalCoverage) * 100).toFixed(0)}% coverage
          rate
        </p>
      )}
    </div>
  );
}
