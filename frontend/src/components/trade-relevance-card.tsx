"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Filter, Network, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { TradeDivisionRelevance } from "@/lib/types";

interface Props {
  projectId: string;
}

function Chip({
  d,
  onToggle,
  pending,
}: {
  d: TradeDivisionRelevance;
  onToggle: () => void;
  pending: boolean;
}) {
  const effective =
    d.operator_override && d.override_value != null
      ? d.override_value
      : d.is_relevant;
  const cls = effective
    ? "bg-emerald-100 text-emerald-900 hover:bg-emerald-200 dark:bg-emerald-900/40 dark:text-emerald-100"
    : "bg-zinc-200 text-zinc-700 line-through hover:bg-zinc-300 dark:bg-zinc-800 dark:text-zinc-400";
  const overrideMark = d.operator_override ? "★ " : "";

  const tooltip = [
    d.reasoning ?? "",
    d.confidence != null ? `· conf ${(d.confidence * 100).toFixed(0)}%` : "",
    d.operator_override ? "· operator override" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={pending}
      className={`rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:opacity-50 ${cls}`}
      title={tooltip}
    >
      {overrideMark}
      {d.csi_division} · {d.division_label.replace(/^\d+ - /, "")}
    </button>
  );
}

export function TradeRelevanceCard({ projectId }: Props) {
  const qc = useQueryClient();

  const matrixQuery = useQuery({
    queryKey: ["trade-relevance", projectId],
    queryFn: () => api.getTradeRelevance(projectId),
  });

  const run = useMutation({
    mutationFn: (force: boolean) => api.runTradeRelevance(projectId, force),
    onSuccess: (m) => {
      toast.success(
        `Filter complete: ${m.relevant_count} relevant, ${m.skipped_count} skipped`,
      );
      qc.invalidateQueries({ queryKey: ["trade-relevance", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const override = useMutation({
    mutationFn: ({
      csi_division,
      is_relevant,
    }: {
      csi_division: string;
      is_relevant: boolean;
    }) => api.overrideTradeRelevance(projectId, csi_division, is_relevant),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trade-relevance", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const matrix = matrixQuery.data;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Filter className="size-4 text-emerald-600" />
            Relevant trades
          </h3>
          <p className="text-xs text-muted-foreground">
            CSI divisions to extract scope from. Click a chip to flip it (★
            = manually overridden). Phase 4.2.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {matrix && (
            <span className="text-xs text-muted-foreground">
              {matrix.relevant_count} of {matrix.divisions.length} ·{" "}
              ${matrix.total_cost_usd.toFixed(3)}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => run.mutate(matrix != null)}
            disabled={run.isPending}
          >
            {matrix ? (
              <RefreshCw className={`size-4 ${run.isPending ? "animate-spin" : ""}`} />
            ) : (
              <Network className="size-4" />
            )}
            <span className="ml-1.5">
              {run.isPending
                ? "Filtering…"
                : matrix
                  ? "Re-filter"
                  : "Filter relevant trades"}
            </span>
          </Button>
        </div>
      </div>

      {!matrix || matrix.divisions.length === 0 ? (
        <p className="rounded-md border border-dashed py-4 text-center text-sm text-muted-foreground">
          {matrixQuery.isLoading
            ? "Loading…"
            : "Filter not yet run. Profile the project first, then click Filter relevant trades."}
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {matrix.divisions.map((d) => {
            const effective =
              d.operator_override && d.override_value != null
                ? d.override_value
                : d.is_relevant;
            return (
              <Chip
                key={d.id}
                d={d}
                pending={override.isPending}
                onToggle={() =>
                  override.mutate({
                    csi_division: d.csi_division,
                    is_relevant: !effective,
                  })
                }
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
