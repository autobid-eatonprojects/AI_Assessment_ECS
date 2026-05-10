"use client";

import { AlertTriangle, CheckCircle2, HelpCircle, Sparkles } from "lucide-react";
import type { QtyConfidence } from "@/lib/types";

interface Props {
  quantity: string | null | undefined;
  unit: string | null | undefined;
  confidence: QtyConfidence | null | undefined;
  provenance: Record<string, unknown> | null | undefined;
  /** Smaller variant for use in dense list rows. */
  compact?: boolean;
}

/**
 * Phase 6 — quantity confidence badge.
 *
 * Renders the resolved quantity (e.g. "38 EA", "5,200 SF") next to a small
 * confidence pill (high / medium / conflicting / unverified) with a tooltip
 * that explains where the number came from. When quantity is null we show
 * "—" rather than a fake number.
 */
export function QuantityBadge({
  quantity,
  unit,
  confidence,
  provenance,
  compact,
}: Props) {
  const text = quantity ? `${quantity}${unit ? " " + unit : ""}` : "—";
  const band: QtyConfidence = confidence ?? "unverified";

  const styles: Record<
    QtyConfidence,
    { wrap: string; pill: string; Icon: typeof CheckCircle2; label: string }
  > = {
    high: {
      wrap: "text-emerald-900 dark:text-emerald-200",
      pill: "bg-emerald-100 dark:bg-emerald-900/40",
      Icon: CheckCircle2,
      label: "high",
    },
    medium: {
      wrap: "text-blue-900 dark:text-blue-200",
      pill: "bg-blue-100 dark:bg-blue-900/40",
      Icon: Sparkles,
      label: "medium",
    },
    conflicting: {
      wrap: "text-amber-900 dark:text-amber-200",
      pill: "bg-amber-100 dark:bg-amber-900/40",
      Icon: AlertTriangle,
      label: "conflict",
    },
    unverified: {
      wrap: "text-zinc-700 dark:text-zinc-400",
      pill: "bg-zinc-100 dark:bg-zinc-800",
      Icon: HelpCircle,
      label: "unverified",
    },
  };
  const s = styles[band];

  const tooltip = describeProvenance(band, provenance);

  if (compact) {
    return (
      <span
        title={tooltip}
        className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-xs ${s.pill} ${s.wrap}`}
      >
        <s.Icon className="size-3 shrink-0" />
        <span>{text}</span>
      </span>
    );
  }

  return (
    <span
      title={tooltip}
      className={`inline-flex items-center gap-1.5 rounded px-2 py-1 font-mono text-sm ${s.pill} ${s.wrap}`}
    >
      <s.Icon className="size-4 shrink-0" />
      <span>{text}</span>
      <span className="ml-0.5 rounded bg-background/40 px-1 text-[10px] font-medium uppercase tracking-wide">
        {s.label}
      </span>
    </span>
  );
}

function describeProvenance(
  band: QtyConfidence,
  prov: Record<string, unknown> | null | undefined,
): string {
  if (!prov || typeof prov !== "object") return `Quantity confidence: ${band}`;
  const chosen = (prov.chosen ?? null) as
    | { value?: number; unit?: string; source?: string; detail?: string }
    | null;
  const reason = (prov.reason ?? null) as string | null;
  const evidenceCount = prov.evidence_count;

  if (band === "unverified") {
    return reason ? `Unverified — ${reason}` : "No quantity signal in any source";
  }

  if (band === "conflicting") {
    const all = (prov.all_values ?? []) as Array<{
      value?: number;
      unit?: string;
      source?: string;
    }>;
    const items = all
      .map((v) => `${v.value} ${v.unit ?? ""} (${v.source ?? "?"})`)
      .join(" vs ");
    const spread = prov.spread_pct;
    return `Conflicting sources: ${items}${spread ? ` — spread ${spread}%` : ""}`;
  }

  const src = chosen?.source ?? "?";
  const evidence = evidenceCount && Number(evidenceCount) > 1
    ? `, corroborated by ${Number(evidenceCount) - 1} other source(s)`
    : "";
  const friendly: Record<string, string> = {
    schedule_miner_stated: "Per-row schedule enumeration (each row = 1 unit)",
    sonnet_stated: "Quantity stated explicitly in the source by Sonnet extraction",
    excerpt_regex: "Numeric value found in cited excerpt",
  };
  const reasonText = friendly[src] ?? src;
  return `${reasonText}${evidence}`;
}
