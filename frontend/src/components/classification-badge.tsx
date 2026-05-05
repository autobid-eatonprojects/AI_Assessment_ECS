import type { DocType } from "@/lib/types";

const STYLES: Record<DocType | "unclassified", { label: string; className: string }> = {
  "drawing-set": {
    label: "Drawings",
    className: "bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200",
  },
  "written-spec": {
    label: "Project manual",
    className: "bg-indigo-100 text-indigo-900 dark:bg-indigo-900/40 dark:text-indigo-200",
  },
  "trade-list": {
    label: "Trade list",
    className: "bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200",
  },
  "bid-quote": {
    label: "Bid",
    className: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  },
  "scope-letter": {
    label: "Scope letter",
    className: "bg-teal-100 text-teal-900 dark:bg-teal-900/40 dark:text-teal-200",
  },
  "license-insurance": {
    label: "License / Insurance",
    className: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  },
  "safety-manual": {
    label: "Safety",
    className: "bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200",
  },
  "contractor-info": {
    label: "Contractor info",
    className: "bg-purple-100 text-purple-900 dark:bg-purple-900/40 dark:text-purple-200",
  },
  other: {
    label: "Other",
    className: "bg-zinc-200 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
  },
  unclassified: {
    label: "Unclassified",
    className: "bg-zinc-100 text-zinc-600 dark:bg-zinc-900 dark:text-zinc-400",
  },
};

export function ClassificationBadge({
  docType,
  confidence,
}: {
  docType: DocType | null;
  confidence: number | null;
}) {
  const style = STYLES[docType ?? "unclassified"] ?? STYLES.other;
  const conf =
    confidence != null
      ? ` · ${Math.round(confidence * 100)}%`
      : "";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}
      title={
        confidence != null
          ? `Classifier confidence ${Math.round(confidence * 100)}%`
          : "Awaiting classification"
      }
    >
      {style.label}
      <span className="ml-1 opacity-70">{conf}</span>
    </span>
  );
}
