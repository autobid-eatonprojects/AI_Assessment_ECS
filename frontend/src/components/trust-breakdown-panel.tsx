"use client";

import type { TrustComponents } from "@/lib/types";

/**
 * Per-item trust breakdown — surfaces the populated `trust_components` JSON
 * that bilateral_evidence._classify writes on every scope_item. Lets the
 * user see WHY an item ended up in its evidence tier.
 *
 * Fields displayed (skipped when missing):
 *   - Expected pattern  (spec_only / drawing_only / bilateral)
 *   - Pattern match     (boolean — did the item's evidence shape match?)
 *   - Bilateral         (legacy raw flag — has both spec + drawing)
 *   - Confidence        (0–1)
 *   - Link judge        (pass / fail / n/a)
 *   - Tier reason       (one-line plain English from the rollup)
 */
export function TrustBreakdownPanel({
  components,
}: {
  components: TrustComponents | null | undefined;
}) {
  if (!components) {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        No trust components available — this item may not have been classified yet.
      </p>
    );
  }

  return (
    <div className="mt-2 space-y-2 text-xs">
      <Row
        label="Expected evidence pattern"
        value={
          components.expected_pattern ? (
            <PatternBadge pattern={components.expected_pattern} />
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        }
        hint={EXPECTED_HINT[components.expected_pattern ?? ""] ?? ""}
      />
      <Row
        label="Pattern match"
        value={<BoolBadge value={components.pattern_match} />}
        hint="True when the item's evidence shape matches what's expected for its type."
      />
      <Row
        label="Bilateral evidence"
        value={<BoolBadge value={components.bilateral} />}
        hint="True when item has BOTH spec + drawing citations (regardless of expectation)."
      />
      {components.confidence != null && (
        <Row
          label="Confidence"
          value={
            <div className="flex w-full items-center gap-2">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-primary"
                  style={{
                    width: `${Math.round(components.confidence * 100)}%`,
                  }}
                />
              </div>
              <span className="font-mono">
                {Math.round(components.confidence * 100)}%
              </span>
            </div>
          }
          hint="Average extractor vote confidence."
        />
      )}
      <Row
        label="Link judge"
        value={<LinkJudgeStatus state={components.link_judge} />}
        hint="Haiku entailment verdict: pass = cited chunks support the item."
      />
      {components.tier_reason && (
        <div className="rounded border border-dashed border-muted-foreground/30 bg-muted/20 p-2">
          <span className="font-medium">Why this tier: </span>
          <span className="text-muted-foreground">{components.tier_reason}</span>
        </div>
      )}
    </div>
  );
}

const EXPECTED_HINT: Record<string, string> = {
  bilateral: "Material/equipment items expect both spec + drawing.",
  spec_only:
    "Admin/QC items (submittals, mockups, warranties) are spec-only by nature.",
  drawing_only:
    "Demolition extent and existing-condition tie-ins are typically drawing-only.",
};

function Row({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5">
      <div title={hint} className="text-muted-foreground">
        {label}
      </div>
      <div className="text-right">{value}</div>
    </div>
  );
}

function PatternBadge({ pattern }: { pattern: string }) {
  const meta: Record<string, { label: string; classes: string }> = {
    bilateral: {
      label: "Bilateral",
      classes:
        "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    },
    spec_only: {
      label: "Spec only",
      classes:
        "border-indigo-500/30 bg-indigo-500/10 text-indigo-700 dark:text-indigo-300",
    },
    drawing_only: {
      label: "Drawing only",
      classes:
        "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
    },
  };
  const m = meta[pattern] ?? { label: pattern, classes: "border bg-muted" };
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${m.classes}`}
    >
      {m.label}
    </span>
  );
}

function BoolBadge({ value }: { value: boolean | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="text-muted-foreground">—</span>;
  }
  if (value) {
    return (
      <span className="inline-flex items-center rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">
        ✓ Yes
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
      ✗ No
    </span>
  );
}

function LinkJudgeStatus({ state }: { state: string | null | undefined }) {
  if (!state || state === "n/a") {
    return <span className="text-muted-foreground">not run yet</span>;
  }
  if (state === "pass") {
    return (
      <span className="inline-flex items-center rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">
        ✓ pass
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
      ✗ fail
    </span>
  );
}
