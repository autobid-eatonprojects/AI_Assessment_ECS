"use client";

import { use, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Loader2, Sparkles, Copy } from "lucide-react";
import { toast } from "sonner";

import { AuthGuard } from "@/components/auth-guard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import type { RfiItem } from "@/lib/types";
import { cn } from "@/lib/utils";

const PRIORITY_META: Record<
  RfiItem["priority"],
  { label: string; classes: string }
> = {
  critical: {
    label: "Critical",
    classes:
      "bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/40",
  },
  high: {
    label: "High",
    classes:
      "bg-orange-500/15 text-orange-700 dark:text-orange-300 border-orange-500/40",
  },
  medium: {
    label: "Medium",
    classes:
      "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40",
  },
  low: {
    label: "Low",
    classes:
      "bg-slate-500/15 text-slate-700 dark:text-slate-300 border-slate-500/40",
  },
};

function RfiCard({ item }: { item: RfiItem }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{item.rfi_number}</span>
              <span
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
                  PRIORITY_META[item.priority].classes,
                )}
              >
                {PRIORITY_META[item.priority].label}
              </span>
              <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] uppercase">
                {item.discipline}
              </span>
            </div>
            <CardTitle className="mt-1 text-base">{item.subject}</CardTitle>
          </div>
          {item.csi_section && (
            <span className="rounded-full bg-secondary px-2 py-0.5 font-mono text-xs">
              {item.csi_section}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-2 pt-0 text-sm">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Issue
          </div>
          <p className="text-sm">{item.issue}</p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Ask
          </div>
          <p className="text-sm">{item.ask}</p>
        </div>
        {item.impact && (
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Impact
            </div>
            <p className="text-sm">{item.impact}</p>
          </div>
        )}
        {item.sheet_refs && item.sheet_refs.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {item.sheet_refs.map((ref) => (
              <span
                key={ref}
                className="rounded border bg-muted/50 px-1.5 py-0.5 font-mono text-[10px]"
              >
                {ref}
              </span>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RfiView({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const [generating, setGenerating] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["rfi", projectId],
    queryFn: async () => {
      try {
        return await api.getRfis(projectId);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          return null; // Not generated yet
        }
        throw err;
      }
    },
  });

  const generate = useMutation({
    mutationFn: () => api.generateRfis(projectId),
    onMutate: () => setGenerating(true),
    onSettled: () => setGenerating(false),
    onSuccess: (result) => {
      toast.success(
        `Drafted ${result.rfi_count} RFIs ($${result.cost_usd.toFixed(3)})`,
      );
      qc.invalidateQueries({ queryKey: ["rfi", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const copyAllMarkdown = async () => {
    if (!data) return;
    const md = data.rfi_list
      .map(
        (r) =>
          `## ${r.rfi_number} — ${r.subject}\n\n` +
          `**Discipline**: ${r.discipline}\n` +
          (r.csi_section ? `**CSI Section**: ${r.csi_section}\n` : "") +
          `**Priority**: ${r.priority.toUpperCase()}\n\n` +
          `**Issue**: ${r.issue}\n\n**Ask**: ${r.ask}\n` +
          (r.impact ? `\n**Impact**: ${r.impact}\n` : "") +
          "\n---\n",
      )
      .join("\n");
    await navigator.clipboard.writeText(md);
    toast.success(`Copied ${data.rfi_list.length} RFIs as Markdown`);
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Request For Information</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Opus-drafted RFI list for the design team. Consolidates the
            strongest gaps + open conflicts + low-confidence items into a
            formal punch list.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && (
            <Button variant="outline" size="sm" onClick={copyAllMarkdown}>
              <Copy className="mr-1.5 size-3.5" /> Copy as Markdown
            </Button>
          )}
          <Button
            size="sm"
            disabled={generating || generate.isPending}
            onClick={() => generate.mutate()}
          >
            {generating || generate.isPending ? (
              <Loader2 className="mr-1.5 size-3.5 animate-spin" />
            ) : (
              <Sparkles className="mr-1.5 size-3.5" />
            )}
            {data ? "Regenerate" : "Generate RFIs"}
          </Button>
        </div>
      </header>

      {isLoading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : !data ? (
        <Card className="border-dashed">
          <CardContent className="py-10 text-center">
            <FileText className="mx-auto mb-2 size-8 text-muted-foreground" />
            <p className="text-sm text-muted-foreground">
              No RFI list generated for this project yet. Click "Generate
              RFIs" to draft one — Opus will consolidate the gaps and
              conflicts from your latest scope run.
            </p>
          </CardContent>
        </Card>
      ) : data.rfi_list.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No RFIs needed at this time — the latest run had no gaps or
            conflicts that warrant clarification.
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="text-xs text-muted-foreground">
            {data.rfi_list.length} RFI{data.rfi_list.length === 1 ? "" : "s"}{" "}
            · cost: ${data.cost_usd.toFixed(3)} · run:{" "}
            <span className="font-mono">{data.run_id.slice(0, 8)}</span>
          </div>
          <div className="space-y-3">
            {data.rfi_list.map((r) => (
              <RfiCard key={r.rfi_number} item={r} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function RfiPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <RfiView projectId={id} />
    </AuthGuard>
  );
}
