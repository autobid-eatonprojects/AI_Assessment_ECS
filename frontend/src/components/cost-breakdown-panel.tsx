"use client";

import { useMemo, useState } from "react";

/**
 * Pass D1 — cost breakdown panel.
 *
 * Aggregates the existing /audit-log/llm-calls response client-side. Three
 * views toggleable from a header:
 *   - by stage (LLMCall.purpose)
 *   - by model (LLMCall.model)
 *   - by document (LLMCall.document_id)
 *
 * Renders inline horizontal bars (no chart lib). Each bar shows
 * percentage of total project spend + absolute cost. Top 10 in each
 * grouping; small ones collapse into "+ N other".
 */

type LLMCall = {
  id: string;
  purpose: string;
  model: string;
  cost_usd: number | null;
  latency_ms: number | null;
  document_id?: string | null;
  status: string;
};

type DocLookup = Record<string, string>; // document_id -> filename

type GroupKey = "purpose" | "model" | "document_id";

export function CostBreakdownPanel({
  calls,
  documents,
}: {
  calls: LLMCall[] | undefined;
  documents?: DocLookup;
}) {
  const [groupBy, setGroupBy] = useState<GroupKey>("purpose");

  const { rows, total } = useMemo(() => {
    const grouped: Record<string, { count: number; cost: number }> = {};
    let total = 0;
    for (const c of calls ?? []) {
      const cost = c.cost_usd ?? 0;
      total += cost;
      const key =
        groupBy === "document_id"
          ? c.document_id || "(no document)"
          : (c[groupBy] as string) || "(unknown)";
      const bucket = grouped[key] ?? { count: 0, cost: 0 };
      bucket.count += 1;
      bucket.cost += cost;
      grouped[key] = bucket;
    }
    const rows = Object.entries(grouped)
      .map(([key, v]) => ({ key, ...v }))
      .sort((a, b) => b.cost - a.cost);
    return { rows, total };
  }, [calls, groupBy]);

  if (!calls || calls.length === 0) {
    return null;
  }

  const top = rows.slice(0, 10);
  const remaining = rows.slice(10);
  const remainingCost = remaining.reduce((sum, r) => sum + r.cost, 0);

  return (
    <div className="rounded-md border bg-card p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold">Cost breakdown</h3>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">${total.toFixed(2)} total</span>
          <span className="text-muted-foreground">·</span>
          <span className="text-muted-foreground">{calls.length} calls</span>
        </div>
      </div>
      <div className="mb-3 flex gap-1.5 text-xs">
        {(
          [
            ["purpose", "By stage"],
            ["model", "By model"],
            ["document_id", "By document"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setGroupBy(k)}
            className={
              groupBy === k
                ? "rounded-full border border-primary bg-primary/10 px-2.5 py-0.5 font-medium text-primary"
                : "rounded-full border border-muted-foreground/20 bg-card px-2.5 py-0.5 font-medium text-muted-foreground hover:bg-muted/50"
            }
          >
            {label}
          </button>
        ))}
      </div>
      <ul className="space-y-1.5 text-xs">
        {top.map((row) => {
          const pct = total > 0 ? (row.cost / total) * 100 : 0;
          const label =
            groupBy === "document_id" && documents
              ? documents[row.key] ?? row.key.slice(0, 8)
              : row.key;
          return (
            <li
              key={row.key}
              className="grid grid-cols-[1fr_auto] items-center gap-x-2"
            >
              <div className="space-y-0.5">
                <div className="flex items-baseline justify-between">
                  <span className="truncate font-medium" title={label}>
                    {label}
                  </span>
                  <span className="text-muted-foreground">
                    {row.count} call{row.count === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full bg-primary"
                    style={{ width: `${Math.min(100, pct)}%` }}
                  />
                </div>
              </div>
              <div className="ml-2 whitespace-nowrap font-mono">
                ${row.cost.toFixed(3)}
              </div>
            </li>
          );
        })}
        {remaining.length > 0 && (
          <li className="grid grid-cols-[1fr_auto] gap-x-2 text-muted-foreground">
            <span>
              + {remaining.length} other{remaining.length === 1 ? "" : "s"}
            </span>
            <span className="ml-2 whitespace-nowrap font-mono">
              ${remainingCost.toFixed(3)}
            </span>
          </li>
        )}
      </ul>
    </div>
  );
}
