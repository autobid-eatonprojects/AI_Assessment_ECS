"use client";

import { Loader2, Play, RefreshCw, Target } from "lucide-react";
import { useEffect, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { RagasEvalRun } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

interface Props {
  projectId: string;
  scopeRunId: string;
  scopeRunStatus: string;
}

const METRIC_TOOLTIPS = {
  context_precision:
    "Of the chunks the system cited, how many were judged relevant to the section's question. Punishes spammy citations.",
  context_recall:
    "Did the cited chunks contain the information needed to express each expected ground-truth item.",
  answer_faithfulness:
    "Is every claim in the extracted item list grounded in the cited chunks. Detects hallucination.",
  answer_relevancy:
    "Embedding similarity between the extracted answer and reverse-generated questions targeting the same answer.",
};

function pct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function gradeFor(v: number | null | undefined): {
  letter: string;
  cls: string;
} {
  if (v == null) return { letter: "—", cls: "text-muted-foreground" };
  if (v >= 0.85) return { letter: "A", cls: "text-emerald-600 dark:text-emerald-400" };
  if (v >= 0.75) return { letter: "B", cls: "text-emerald-600 dark:text-emerald-400" };
  if (v >= 0.6) return { letter: "C", cls: "text-amber-600 dark:text-amber-400" };
  if (v >= 0.4) return { letter: "D", cls: "text-amber-600 dark:text-amber-400" };
  return { letter: "F", cls: "text-rose-600 dark:text-rose-400" };
}

function MetricTile({
  label,
  tooltip,
  value,
}: {
  label: string;
  tooltip: string;
  value: number | null;
}) {
  const grade = gradeFor(value);
  return (
    <div
      className="rounded-md border bg-card p-3"
      title={tooltip}
    >
      <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold tabular-nums">{pct(value)}</span>
        <span className={cn("text-sm font-medium", grade.cls)}>{grade.letter}</span>
      </div>
    </div>
  );
}

export function RagasPanel({ projectId, scopeRunId, scopeRunStatus }: Props) {
  const qc = useQueryClient();

  const ragasQuery = useQuery({
    queryKey: ["ragas", projectId, scopeRunId],
    queryFn: () => api.getRagasEval(projectId, scopeRunId),
    enabled: scopeRunStatus === "complete",
    refetchInterval: (q) => {
      const r = q.state.data;
      return r?.status === "running" ? 3000 : false;
    },
  });

  const start = useMutation({
    mutationFn: () => api.startRagasEval(projectId, scopeRunId),
    onSuccess: () => {
      toast.success("RAGAS benchmark started — polling for results");
      qc.invalidateQueries({ queryKey: ["ragas", projectId, scopeRunId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const evaluation = ragasQuery.data ?? null;
  const isRunning = evaluation?.status === "running";
  const isComplete = evaluation?.status === "complete";
  const isFailed = evaluation?.status === "failed";

  // Cancel polling once the latest cached result is complete.
  useEffect(() => {
    if (isComplete) qc.invalidateQueries({ queryKey: ["ragas", projectId, scopeRunId] });
  }, [isComplete, qc, projectId, scopeRunId]);

  const sortedFixtures = useMemo(() => {
    if (!evaluation?.per_fixture) return [];
    return [...evaluation.per_fixture].sort((a, b) =>
      a.csi_section.localeCompare(b.csi_section),
    );
  }, [evaluation?.per_fixture]);

  if (scopeRunStatus !== "complete") return null;

  return (
    <div className="mb-6 rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <Target className="size-4 text-muted-foreground" />
            RAGAS benchmark
          </h2>
          <p className="text-sm text-muted-foreground">
            Evaluates this run against curated ground-truth fixtures using the
            four canonical RAGAS metrics. ~$1, ~80s.
          </p>
        </div>
        <Button
          size="sm"
          variant={evaluation ? "outline" : "default"}
          disabled={isRunning || start.isPending}
          onClick={() => start.mutate()}
        >
          {isRunning || start.isPending ? (
            <Loader2 className="size-4 animate-spin" />
          ) : evaluation ? (
            <RefreshCw className="size-4" />
          ) : (
            <Play className="size-4" />
          )}
          <span className="ml-1.5">
            {isRunning
              ? "Evaluating…"
              : start.isPending
                ? "Starting…"
                : evaluation
                  ? "Re-run benchmark"
                  : "Benchmark this run"}
          </span>
        </Button>
      </div>

      {!evaluation && !start.isPending && (
        <div className="rounded-md border border-dashed py-6 text-center text-sm text-muted-foreground">
          No benchmark yet for this scope run.
        </div>
      )}

      {isRunning && (
        <div className="rounded-md border bg-blue-50 p-3 text-sm dark:bg-blue-950/30">
          Evaluating {evaluation.n_fixtures || "…"} fixtures · started{" "}
          {new Date(evaluation.started_at).toLocaleTimeString()}
        </div>
      )}

      {isFailed && evaluation && (
        <div className="rounded-md border border-rose-500/30 bg-rose-50 p-3 text-sm dark:bg-rose-950/30">
          Evaluation failed: {evaluation.error || "unknown error"}
        </div>
      )}

      {isComplete && evaluation && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <MetricTile
              label="Context precision"
              tooltip={METRIC_TOOLTIPS.context_precision}
              value={evaluation.context_precision}
            />
            <MetricTile
              label="Context recall"
              tooltip={METRIC_TOOLTIPS.context_recall}
              value={evaluation.context_recall}
            />
            <MetricTile
              label="Faithfulness"
              tooltip={METRIC_TOOLTIPS.answer_faithfulness}
              value={evaluation.answer_faithfulness}
            />
            <MetricTile
              label="Answer relevancy"
              tooltip={METRIC_TOOLTIPS.answer_relevancy}
              value={evaluation.answer_relevancy}
            />
          </div>

          <div className="mb-3 flex items-baseline justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2 text-sm">
            <div>
              <span className="text-muted-foreground">Overall (geom mean): </span>
              <span className="font-semibold tabular-nums">
                {pct(evaluation.overall_score)}
              </span>
              <span
                className={cn(
                  "ml-2 font-medium",
                  gradeFor(evaluation.overall_score).cls,
                )}
              >
                {gradeFor(evaluation.overall_score).letter}
              </span>
            </div>
            <div className="text-muted-foreground">
              {evaluation.n_fixtures} fixtures · {evaluation.elapsed_sec.toFixed(1)}s ·
              ${evaluation.total_cost_usd.toFixed(2)}
            </div>
          </div>

          {sortedFixtures.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b">
                    <th className="px-2 py-1.5 text-left font-medium">Section</th>
                    <th className="px-2 py-1.5 text-left font-medium">Title</th>
                    <th className="px-2 py-1.5 text-right font-medium">Items (got/exp)</th>
                    <th className="px-2 py-1.5 text-right font-medium">Precision</th>
                    <th className="px-2 py-1.5 text-right font-medium">Recall</th>
                    <th className="px-2 py-1.5 text-right font-medium">Faith.</th>
                    <th className="px-2 py-1.5 text-right font-medium">Relev.</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedFixtures.map((f) => (
                    <tr key={f.csi_section} className="border-b last:border-0">
                      <td className="px-2 py-1.5 font-mono">{f.csi_section}</td>
                      <td className="px-2 py-1.5">{f.section_title}</td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-muted-foreground">
                        {f.n_extracted_items}/{f.n_expected_items}
                      </td>
                      <td className={cn("px-2 py-1.5 text-right tabular-nums", gradeFor(f.context_precision).cls)}>
                        {pct(f.context_precision)}
                      </td>
                      <td className={cn("px-2 py-1.5 text-right tabular-nums", gradeFor(f.context_recall).cls)}>
                        {pct(f.context_recall)}
                      </td>
                      <td className={cn("px-2 py-1.5 text-right tabular-nums", gradeFor(f.answer_faithfulness).cls)}>
                        {pct(f.answer_faithfulness)}
                      </td>
                      <td className={cn("px-2 py-1.5 text-right tabular-nums", gradeFor(f.answer_relevancy).cls)}>
                        {pct(f.answer_relevancy)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
