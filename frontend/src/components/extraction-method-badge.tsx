"use client";

import { cn } from "@/lib/utils";

const META: Record<
  string,
  { label: string; classes: string; tooltip: string }
> = {
  discipline_agent_v1: {
    label: "Discipline agent",
    classes:
      "bg-violet-500/10 text-violet-700 dark:text-violet-300 border-violet-500/30",
    tooltip:
      "Bilateral-evidence emission from the per-discipline Sonnet 4.6 agent (P3)",
  },
  schedule_miner: {
    label: "Schedule miner",
    classes:
      "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30",
    tooltip:
      "Deterministic enumeration from a typed schedule row (Phase-2 structured data)",
  },
  schedule: {
    label: "Schedule",
    classes:
      "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30",
    tooltip: "Sonnet EVE candidate seeded by a schedule row",
  },
  note: {
    label: "Drawing note",
    classes:
      "bg-slate-500/10 text-slate-700 dark:text-slate-300 border-slate-500/30",
    tooltip: "Sonnet EVE candidate seeded by a drawing note",
  },
  spec_section: {
    label: "Spec",
    classes:
      "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/30",
    tooltip: "Sonnet EVE candidate seeded by a spec section",
  },
  plan_callout: {
    label: "Plan callout",
    classes:
      "bg-teal-500/10 text-teal-700 dark:text-teal-300 border-teal-500/30",
    tooltip: "Sonnet EVE candidate seeded by a drawing plan callout",
  },
  inferred: {
    label: "Inferred",
    classes:
      "bg-zinc-500/10 text-zinc-700 dark:text-zinc-300 border-zinc-500/30",
    tooltip: "Sonnet inferred from context without a single concrete source",
  },
  // section_extractor variants — items emitted from per-CSI-section Sonnet
  // extraction. Sub-type encoded after the slash (material/equipment/admin/qc/demo)
  // tells the evidence_pattern rollup whether to expect bilateral evidence.
  "section_extractor/material": {
    label: "Material",
    classes:
      "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30",
    tooltip:
      "Material spec from a CSI section (concrete, drywall, paint, etc.). Expects bilateral evidence — drawing-side count comes from drawing_grounder.",
  },
  "section_extractor/equipment": {
    label: "Equipment",
    classes:
      "bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 border-cyan-500/30",
    tooltip:
      "Tagged equipment item (AHU, panel, fixture). Expects bilateral — spec defines product, drawing locates it.",
  },
  "section_extractor/admin": {
    label: "Admin",
    classes:
      "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30",
    tooltip:
      "Administrative scope (submittals, mockups, warranties, closeout). Spec-only by nature — never drawn.",
  },
  "section_extractor/qc": {
    label: "QC",
    classes:
      "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/30",
    tooltip:
      "Quality control / testing requirement. Typically spec-only.",
  },
  "section_extractor/demo": {
    label: "Demo",
    classes:
      "bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/30",
    tooltip:
      "Demolition extent. Typically drawing-only — shown on demo plan.",
  },
};

export function ExtractionMethodBadge({
  method,
  className,
}: {
  method: string | null | undefined;
  className?: string;
}) {
  if (!method) return null;
  const meta = META[method] ?? {
    label: method,
    classes:
      "bg-muted text-muted-foreground border-muted-foreground/20",
    tooltip: method,
  };
  return (
    <span
      title={meta.tooltip}
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium",
        meta.classes,
        className,
      )}
    >
      {meta.label}
    </span>
  );
}
