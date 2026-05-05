import { AlertTriangle, CheckCircle2, KeyRound, Loader2 } from "lucide-react";
import type { ProcessingStatus } from "@/lib/types";

const STATUS: Record<
  ProcessingStatus,
  { label: string; icon: React.ComponentType<{ className?: string }>; className: string }
> = {
  pending: { label: "Queued", icon: Loader2, className: "text-zinc-500" },
  classifying: { label: "Classifying", icon: Loader2, className: "text-blue-600" },
  rendering: { label: "Rendering pages", icon: Loader2, className: "text-blue-600" },
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
}: {
  status: ProcessingStatus;
  error?: string | null;
}) {
  const cfg = STATUS[status];
  const Icon = cfg.icon;
  const spin = status === "pending" || status === "classifying" || status === "rendering";
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-medium ${cfg.className}`}
      title={error ?? cfg.label}
    >
      <Icon className={`size-3.5 ${spin ? "animate-spin" : ""}`} />
      {cfg.label}
    </span>
  );
}
