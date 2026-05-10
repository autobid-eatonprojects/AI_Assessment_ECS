"use client";

import type { TrustScore } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  trust: TrustScore | null | undefined;
  loading?: boolean;
  compact?: boolean;
}

const COMPONENT_LABELS: Record<string, string> = {
  // bilateral_coverage is the legacy backend key; the metric now reflects
  // pattern_match (evidence-pattern coverage) after the redesign — see
  // bilateral_evidence._classify and evidence_pattern.expected_pattern.
  // Keep the key for back-compat; rename the user-visible label.
  bilateral_coverage: "Evidence coverage",
  extraction_confidence_avg: "Extraction confidence",
  link_judge_pass_rate: "Citation entailment",
  spec_section_coverage: "Spec section coverage",
  ocr_text_coverage: "OCR text coverage",
  schedule_extraction_validity: "Schedule extraction",
  document_version_consistency: "Doc version consistency",
};

const COMPONENT_TOOLTIPS: Record<string, string> = {
  bilateral_coverage:
    "Items whose evidence matches the pattern expected for their type. Material/equipment items expect bilateral (spec + drawing). Division 1 admin items expect spec-only. Demolition items expect drawing-only.",
  extraction_confidence_avg:
    "Average extractor confidence across all items in this run.",
  link_judge_pass_rate:
    "Fraction of citations where the Haiku link judge ruled the cited chunk genuinely supports the item.",
  spec_section_coverage:
    "Fraction of CSI sections in the project that produced at least one scope item.",
  ocr_text_coverage: "Fraction of pages that have searchable text content.",
  schedule_extraction_validity:
    "Fraction of detected schedules that the typed-extractor parsed cleanly.",
  document_version_consistency:
    "Whether the project's documents reference consistent revision dates.",
};

// Pass D3 — interpret each trust-score component's percentage as a
// plain-English explanation of what the number means and what would
// improve it. Surfaces only when the component is below the green
// threshold (0.8) so high-performing components stay visually clean.
function interpretComponent(key: string, value: number): string {
  const pctMissing = Math.round((1 - value) * 100);
  switch (key) {
    case "bilateral_coverage":
      return `${pctMissing}% of items don't meet their expected evidence pattern. Most often: material items missing drawing-side evidence.`;
    case "extraction_confidence_avg":
      return `Average extractor confidence is below 80%. Items with lower vote agreement live in the review queue.`;
    case "link_judge_pass_rate":
      return `${pctMissing}% of citations were flagged by the Haiku link judge as not actually supporting their item.`;
    case "spec_section_coverage":
      return `${pctMissing}% of CSI sections in the project produced zero scope items.`;
    case "ocr_text_coverage":
      return `${pctMissing}% of pages have no searchable text content (likely scanned, blank, or image-only).`;
    case "schedule_extraction_validity":
      return `${pctMissing}% of detected schedules failed to parse cleanly into rows.`;
    case "document_version_consistency":
      return `Document revision dates are inconsistent — possibly a mix of old and new addenda.`;
    default:
      return "";
  }
}

function tierColor(tier?: string) {
  if (tier === "GREEN") return "text-emerald-600 dark:text-emerald-400";
  if (tier === "YELLOW") return "text-amber-600 dark:text-amber-400";
  if (tier === "RED") return "text-rose-600 dark:text-rose-400";
  return "text-muted-foreground";
}

function tierBg(tier?: string) {
  if (tier === "GREEN") return "bg-emerald-500/10";
  if (tier === "YELLOW") return "bg-amber-500/10";
  if (tier === "RED") return "bg-rose-500/10";
  return "bg-muted";
}

function pct(v: number | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Math.round(v * 100)}%`;
}

function Gauge({ score, tier }: { score: number; tier: string }) {
  const pctScore = Math.round(score * 100);
  const radius = 44;
  const stroke = 8;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score * circumference);
  const ringColor =
    tier === "GREEN"
      ? "stroke-emerald-500"
      : tier === "YELLOW"
        ? "stroke-amber-500"
        : "stroke-rose-500";
  return (
    <svg width="120" height="120" viewBox="0 0 120 120" className="shrink-0">
      <circle
        cx="60"
        cy="60"
        r={radius}
        strokeWidth={stroke}
        className="fill-none stroke-muted"
      />
      <circle
        cx="60"
        cy="60"
        r={radius}
        strokeWidth={stroke}
        className={cn("fill-none transition-all", ringColor)}
        strokeDasharray={circumference}
        strokeDashoffset={offset}
        strokeLinecap="round"
        transform="rotate(-90 60 60)"
      />
      <text
        x="60"
        y="62"
        textAnchor="middle"
        className={cn("fill-current font-semibold text-2xl", tierColor(tier))}
      >
        {pctScore}
      </text>
      <text
        x="60"
        y="80"
        textAnchor="middle"
        className="fill-muted-foreground text-[10px] uppercase tracking-wider"
      >
        Trust
      </text>
    </svg>
  );
}

export function TrustScorePanel({ trust, loading, compact }: Props) {
  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Loading trust score…
      </div>
    );
  }
  if (!trust) {
    return (
      <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Trust score not yet computed. Run a scope extraction first.
      </div>
    );
  }

  const { score, tier, components, weights } = trust;

  if (compact) {
    return (
      <div className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3">
        <Gauge score={score} tier={tier} />
        <div className="min-w-0">
          <div className={cn("text-sm font-semibold uppercase tracking-wider", tierColor(tier))}>
            {tier}
          </div>
          <p className="text-xs text-muted-foreground">
            Project trust score
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-5">
      <div className="flex items-start gap-5">
        <Gauge score={score} tier={tier} />
        <div className="min-w-0 flex-1">
          <div
            className={cn(
              "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wider",
              tierBg(tier),
              tierColor(tier),
            )}
          >
            {tier}
          </div>
          <h3 className="mt-2 text-base font-semibold">Project trust score</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            6-component weighted score: bilateral evidence, extraction
            confidence, citation entailment, spec coverage, OCR text
            coverage, schedule extraction validity, and document version
            consistency.
          </p>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {Object.entries(components).map(([key, value]) => {
          const weight = weights[key] ?? 0;
          const pctVal = (value as number | undefined) ?? 0;
          return (
            <div
              key={key}
              className="rounded-md border bg-background/50 px-3 py-2"
            >
              <div className="flex items-baseline justify-between">
                <span
                  className="text-xs font-medium"
                  title={COMPONENT_TOOLTIPS[key] ?? key}
                >
                  {COMPONENT_LABELS[key] ?? key}
                </span>
                <span className="text-xs tabular-nums text-muted-foreground">
                  weight {Math.round(weight * 100)}%
                </span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn(
                      "h-full rounded-full",
                      pctVal >= 0.8
                        ? "bg-emerald-500"
                        : pctVal >= 0.6
                          ? "bg-amber-500"
                          : "bg-rose-500",
                    )}
                    style={{ width: `${Math.round(pctVal * 100)}%` }}
                  />
                </div>
                <span className="w-12 text-right text-xs font-semibold tabular-nums">
                  {pct(pctVal)}
                </span>
              </div>
              {/* Pass D3 — concrete interpretation of what this number means */}
              {pctVal < 0.8 && (
                <div className="mt-1.5 text-[10px] leading-snug text-muted-foreground">
                  {interpretComponent(key, pctVal)}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
