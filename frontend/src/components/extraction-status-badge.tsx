import { AlertCircle, CheckCircle2, Clock, Loader2 } from "lucide-react";
import type { ExtractionStatus } from "@/lib/types";

const STATUS: Record<
  ExtractionStatus,
  { label: string; className: string; icon: React.ComponentType<{ className?: string }>; spin?: boolean }
> = {
  pending: { label: "Queued", className: "bg-zinc-100 text-zinc-700", icon: Clock },
  extracting: { label: "Extracting", className: "bg-blue-100 text-blue-800", icon: Loader2, spin: true },
  ready: { label: "Ready", className: "bg-emerald-100 text-emerald-800", icon: CheckCircle2 },
  failed: { label: "Failed", className: "bg-red-100 text-red-800", icon: AlertCircle },
};

export function ExtractionStatusBadge({
  status,
  size = "sm",
}: {
  status: ExtractionStatus;
  size?: "xs" | "sm";
}) {
  const cfg = STATUS[status];
  const Icon = cfg.icon;
  const spinClass = cfg.spin ? "animate-spin" : "";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full font-medium ${cfg.className} ${
        size === "xs" ? "px-1.5 py-0 text-[10px]" : "px-2 py-0.5 text-xs"
      }`}
    >
      <Icon className={`${size === "xs" ? "size-2.5" : "size-3"} ${spinClass}`} />
      {cfg.label}
    </span>
  );
}
