import { AlertTriangle, CheckCircle2, KeyRound, Loader2 } from "lucide-react";
import type { ProcessingProgress, ProcessingStatus } from "@/lib/types";

const STATUS: Record<
  ProcessingStatus,
  { label: string; icon: React.ComponentType<{ className?: string }>; className: string }
> = {
  pending: { label: "Queued", icon: Loader2, className: "text-zinc-500" },
  classifying: { label: "Classifying", icon: Loader2, className: "text-blue-600" },
  rendering: { label: "Rendering pages", icon: Loader2, className: "text-blue-600" },
  extracting: { label: "Vision pre-pass", icon: Loader2, className: "text-purple-600" },
  enriching: { label: "Enriching metadata", icon: Loader2, className: "text-violet-600" },
  ocr: { label: "OCR (Gemini Flash)", icon: Loader2, className: "text-cyan-600" },
  indexing: { label: "Indexing", icon: Loader2, className: "text-blue-600" },
  ready: { label: "Ready", icon: CheckCircle2, className: "text-emerald-600" },
  failed: { label: "Failed", icon: AlertTriangle, className: "text-red-600" },
  "needs-api-key": {
    label: "Needs API key",
    icon: KeyRound,
    className: "text-amber-600",
  },
};

export function ProcessingStatusIndicator({
  status,
  error,
  progress,
}: {
  status: ProcessingStatus;
  error?: string | null;
  progress?: ProcessingProgress | null;
}) {
  // Defensive fallback: if the backend ever sends a status the UI
  // doesn't know about (e.g. a new pipeline stage shipped on the
  // server but not yet here), don't blow up the page — render the
  // unknown status as a neutral "Processing" chip.
  const cfg = STATUS[status] ?? {
    label: status || "Processing",
    icon: Loader2,
    className: "text-zinc-500",
  };
  const Icon = cfg.icon;
  const spin =
    status === "pending" ||
    status === "classifying" ||
    status === "rendering" ||
    status === "extracting" ||
    status === "enriching" ||
    status === "ocr" ||
    status === "indexing";
  // When the backend supplies stage-aware progress, append the count to
  // the chip so the user can see "OCR (Gemini Flash) 168/370 (45%)" at a
  // glance. Falls back gracefully to bare label when progress is null
  // (terminal states or backends that haven't restarted post-fix yet).
  let suffix = "";
  if (progress && progress.total > 0 && progress.stage === status) {
    const pct = Math.round((progress.completed / progress.total) * 100);
    suffix = ` ${progress.completed}/${progress.total} (${pct}%)`;
  }
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-medium ${cfg.className}`}
      title={error ?? cfg.label}
    >
      <Icon className={`size-3.5 ${spin ? "animate-spin" : ""}`} />
      {cfg.label}
      {suffix && <span className="font-normal opacity-80">{suffix}</span>}
    </span>
  );
}
