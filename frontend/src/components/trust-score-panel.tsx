"use client";

import type { TrustScore } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  trust: TrustScore | null | undefined;
  loading?: boolean;
  compact?: boolean;
}

const COMPONENT_LABELS: Record<string, string> = {
  bilateral_coverage: "Bilateral coverage",
  extraction_confidence_avg: "Extraction confidence",
  link_judge_pass_rate: "Citation entailment",
  spec_section_coverage: "Spec section coverage",
  ocr_text_coverage: "OCR text coverage",
  schedule_extraction_validity: "Schedule extraction",
  document_version_consistency: "Doc version consistency",
};

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
                <span className="text-xs font-medium">
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
            </div>
          );
        })}
      </div>
    </div>
  );
}
