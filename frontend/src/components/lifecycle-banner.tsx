"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileText, Lock, Unlock } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { LifecycleState, Project } from "@/lib/types";

const STATE_META: Record<
  LifecycleState,
  { label: string; description: string; className: string }
> = {
  setup: {
    label: "Project setup",
    description: "Upload drawings, project manual, and trade list. Bid uploads are disabled.",
    className: "bg-blue-50 text-blue-900 ring-blue-200 dark:bg-blue-950/30 dark:text-blue-100 dark:ring-blue-900",
  },
  "open-for-bids": {
    label: "Open for bids",
    description: "Scope is locked. Subcontractor bid submissions can be uploaded.",
    className: "bg-emerald-50 text-emerald-900 ring-emerald-200 dark:bg-emerald-950/30 dark:text-emerald-100 dark:ring-emerald-900",
  },
  complete: {
    label: "Complete",
    description: "Bid analysis complete. Project archived.",
    className: "bg-zinc-100 text-zinc-700 ring-zinc-200 dark:bg-zinc-900 dark:text-zinc-300 dark:ring-zinc-800",
  },
};

export function LifecycleBanner({ project }: { project: Project }) {
  const qc = useQueryClient();
  const meta = STATE_META[project.lifecycle_state];

  const transition = useMutation({
    mutationFn: (newState: LifecycleState) =>
      api.transitionLifecycle(project.id, newState),
    onSuccess: (p) => {
      toast.success(`Project moved to '${p.lifecycle_state}'`);
      qc.invalidateQueries({ queryKey: ["project", project.id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <div
      className={`flex flex-wrap items-center justify-between gap-3 rounded-lg p-4 ring-1 ${meta.className}`}
    >
      <div className="flex items-center gap-3">
        <div className="flex size-9 items-center justify-center rounded-full bg-white/60 dark:bg-black/20">
          {project.lifecycle_state === "setup" && <FileText className="size-4" />}
          {project.lifecycle_state === "open-for-bids" && <Lock className="size-4" />}
          {project.lifecycle_state === "complete" && <CheckCircle2 className="size-4" />}
        </div>
        <div>
          <p className="text-sm font-semibold">{meta.label}</p>
          <p className="text-xs opacity-80">{meta.description}</p>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {project.lifecycle_state === "setup" && (
          <Button
            size="sm"
            onClick={() => {
              if (
                project.project_document_count === 0 &&
                !confirm("No project documents uploaded yet. Lock scope anyway?")
              )
                return;
              transition.mutate("open-for-bids");
            }}
            disabled={transition.isPending}
          >
            <Lock className="mr-1.5 size-4" /> Lock scope · open for bids
          </Button>
        )}
        {project.lifecycle_state === "open-for-bids" && (
          <>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                if (confirm("Re-open scope for editing? Existing bids stay attached."))
                  transition.mutate("setup");
              }}
              disabled={transition.isPending}
            >
              <Unlock className="mr-1.5 size-4" /> Re-open scope
            </Button>
            <Button
              size="sm"
              onClick={() => transition.mutate("complete")}
              disabled={transition.isPending}
            >
              <CheckCircle2 className="mr-1.5 size-4" /> Mark complete
            </Button>
          </>
        )}
        {project.lifecycle_state === "complete" && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => transition.mutate("open-for-bids")}
            disabled={transition.isPending}
          >
            Re-open
          </Button>
        )}
      </div>
    </div>
  );
}
