"use client";

import type { EvidenceTier } from "@/lib/types";
import { cn } from "@/lib/utils";

const TIER_META: Record<EvidenceTier, { label: string; classes: string }> = {
  EXPLICITLY_CITED: {
    label: "Explicit",
    classes:
      "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30",
  },
  INFERRED_HIGH_CONFIDENCE: {
    label: "Inferred / High",
    classes:
      "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30",
  },
  INFERRED_LOW_CONFIDENCE: {
    label: "Inferred / Low",
    classes: "bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30",
  },
};

export function EvidenceTierBadge({
  tier,
  className,
}: {
  tier: EvidenceTier | null | undefined;
  className?: string;
}) {
  if (!tier) {
    return (
      <span
        className={cn(
          "inline-flex items-center rounded-full border bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground",
          className,
        )}
      >
        unscored
      </span>
    );
  }
  const meta = TIER_META[tier];
  return (
    <span
      title={tier}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        meta.classes,
        className,
      )}
    >
      {meta.label}
    </span>
  );
}
