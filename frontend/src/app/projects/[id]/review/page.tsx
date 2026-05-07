"use client";

import { use, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  Check,
  ChevronRight,
  ClipboardCheck,
  Info,
  Search,
  TrendingUp,
} from "lucide-react";
import { toast } from "sonner";
import { AuthGuard } from "@/components/auth-guard";
import { EvidenceTierBadge } from "@/components/evidence-tier-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { Conflict, Gap, ScopeItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "@/lib/format";

const SEVERITY_META = {
  blocker: {
    label: "Blocker",
    icon: AlertCircle,
    classes: "bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30",
  },
  warn: {
    label: "Warn",
    icon: AlertTriangle,
    classes:
      "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30",
  },
  info: {
    label: "Info",
    icon: Info,
    classes:
      "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30",
  },
} as const;

function SeverityBadge({ severity }: { severity: Gap["severity"] }) {
  const m = SEVERITY_META[severity];
  const Icon = m.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        m.classes,
      )}
    >
      <Icon className="mr-1 size-3" /> {m.label}
    </span>
  );
}

function ConflictCard({
  conflict,
  projectId,
}: {
  conflict: Conflict;
  projectId: string;
}) {
  const qc = useQueryClient();
  const [selectedMember, setSelectedMember] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const resolve = useMutation({
    mutationFn: (winner_member_id: string) =>
      api.resolveConflict(projectId, conflict.id, {
        winner_member_id,
        note: note || undefined,
      }),
    onSuccess: () => {
      toast.success("Conflict resolved");
      qc.invalidateQueries({ queryKey: ["conflicts", projectId] });
      qc.invalidateQueries({ queryKey: ["audit-log", projectId] });
      setSelectedMember(null);
      setNote("");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card className="overflow-hidden">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <span className="rounded-md bg-rose-500/10 px-2 py-0.5 text-xs font-medium uppercase tracking-wider text-rose-700 dark:text-rose-300">
            {conflict.conflict_type.replace(/_/g, " ")}
          </span>
          {conflict.csi_division && (
            <span className="text-xs font-mono text-muted-foreground">
              Div {conflict.csi_division}
            </span>
          )}
          <span className="ml-auto text-xs text-muted-foreground">
            {formatRelativeTime(conflict.created_at)}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {(conflict.members ?? []).map((m) => {
            const snap = (conflict.item_snapshots ?? []).find(
              (s) => s.id === m.scope_item_id,
            );
            if (!snap) return null;
            const isSelected = selectedMember === m.id;
            return (
              <button
                type="button"
                key={m.id}
                onClick={() => setSelectedMember(m.id)}
                className={cn(
                  "flex flex-col gap-1.5 rounded-md border p-2.5 text-left transition-colors",
                  isSelected
                    ? "border-primary bg-primary/5"
                    : "hover:border-muted-foreground/40",
                )}
              >
                <div className="flex items-center gap-1.5 text-[11px] font-mono text-muted-foreground">
                  {snap.csi_code}
                  <EvidenceTierBadge tier={snap.evidence_tier} />
                </div>
                <div className="text-sm">{snap.description}</div>
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>
                    {snap.quantity ?? "—"} {snap.unit ?? ""}
                  </span>
                  <span>conf {(snap.confidence * 100).toFixed(0)}%</span>
                </div>
                {isSelected && (
                  <div className="mt-1 flex items-center gap-1 text-[11px] font-medium text-primary">
                    <Check className="size-3" /> Selected as winner
                  </div>
                )}
              </button>
            );
          })}
        </div>

        {selectedMember && (
          <div className="space-y-2 border-t pt-3">
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Resolution note (optional)…"
              className="text-sm"
            />
            <div className="flex items-center justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setSelectedMember(null);
                  setNote("");
                }}
                disabled={resolve.isPending}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => resolve.mutate(selectedMember)}
                disabled={resolve.isPending}
              >
                {resolve.isPending ? "Resolving…" : "Resolve conflict"}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function GapCard({ gap, projectId }: { gap: Gap; projectId: string }) {
  const qc = useQueryClient();
  const ack = useMutation({
    mutationFn: () => api.acknowledgeGap(projectId, gap.id, {}),
    onSuccess: () => {
      toast.success("Gap acknowledged");
      qc.invalidateQueries({ queryKey: ["gaps", projectId] });
      qc.invalidateQueries({ queryKey: ["audit-log", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const promote = useMutation({
    mutationFn: () => api.promoteGapToRfi(projectId, gap.id),
    onSuccess: (draft) => {
      const md =
        `# RFI: ${draft.rfi_subject}\n\n` +
        `**Discipline**: ${draft.discipline}\n` +
        (draft.csi_section ? `**CSI Section**: ${draft.csi_section}\n` : "") +
        `**Priority**: ${draft.priority.toUpperCase()}\n` +
        (draft.sheet_refs.length
          ? `**Sheets**: ${draft.sheet_refs.join(", ")}\n`
          : "") +
        `\n${draft.rfi_body}`;
      void navigator.clipboard.writeText(md);
      toast.success(`RFI drafted + copied: "${draft.rfi_subject}"`);
      qc.invalidateQueries({ queryKey: ["audit-log", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <SeverityBadge severity={gap.severity} />
              <span className="text-xs font-mono text-muted-foreground">
                {gap.gap_type.replace(/_/g, " ")}
              </span>
              {gap.csi_division && (
                <span className="text-xs font-mono text-muted-foreground">
                  · Div {gap.csi_division}
                </span>
              )}
              {gap.csi_section && (
                <span className="text-xs font-mono text-muted-foreground">
                  · §{gap.csi_section}
                </span>
              )}
            </div>
            <p className="mt-1.5 text-sm">{gap.description}</p>
            {gap.suggested_remediation && (
              <p className="mt-1 text-xs text-muted-foreground">
                <span className="font-medium">Suggested: </span>
                {gap.suggested_remediation}
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-col gap-1.5">
            <Button
              variant="default"
              size="sm"
              onClick={() => promote.mutate()}
              disabled={promote.isPending}
            >
              {promote.isPending ? "Drafting…" : "Send to RFI"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => ack.mutate()}
              disabled={ack.isPending}
            >
              {ack.isPending ? "Acknowledging…" : "Acknowledge"}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function LowConfidenceCard({ item }: { item: ScopeItem }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          <EvidenceTierBadge tier={item.evidence_tier} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-xs">
              <span className="font-mono text-muted-foreground">
                {item.csi_code}
              </span>
              <span className="text-muted-foreground">·</span>
              <span className="text-muted-foreground">
                {item.section_title ?? item.division_label}
              </span>
              <span className="ml-auto text-muted-foreground">
                conf {(item.confidence * 100).toFixed(0)}%
              </span>
            </div>
            <p className="mt-1 text-sm">{item.description}</p>
            <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
              <span>
                {item.quantity ?? "—"} {item.unit ?? ""}
              </span>
              <span>{item.citations.length} citation{item.citations.length === 1 ? "" : "s"}</span>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ReviewView({ projectId }: { projectId: string }) {
  const [filter, setFilter] = useState("");
  const [activeTab, setActiveTab] = useState<"conflicts" | "gaps" | "low">(
    "conflicts",
  );

  const { data: conflicts } = useQuery({
    queryKey: ["conflicts", projectId, "open"],
    queryFn: () => api.listConflicts(projectId, "open"),
    refetchInterval: 6_000,
  });
  const { data: gaps } = useQuery({
    queryKey: ["gaps", projectId, "open"],
    queryFn: () =>
      api.listGaps(projectId, {
        status: "open",
        severity: ["blocker", "warn"],
      }),
    refetchInterval: 6_000,
  });
  const { data: lowConf } = useQuery({
    queryKey: ["low-conf", projectId],
    queryFn: () => api.listLowConfidence(projectId),
    refetchInterval: 6_000,
  });

  const filteredConflicts =
    !filter || !conflicts
      ? conflicts ?? []
      : conflicts.filter((c) =>
          c.item_snapshots.some((s) =>
            s.description.toLowerCase().includes(filter.toLowerCase()),
          ),
        );
  const filteredGaps =
    !filter || !gaps
      ? gaps ?? []
      : gaps.filter((g) =>
          g.description.toLowerCase().includes(filter.toLowerCase()),
        );
  const filteredLow =
    !filter || !lowConf
      ? lowConf ?? []
      : lowConf.filter((it) =>
          it.description.toLowerCase().includes(filter.toLowerCase()),
        );

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Review</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Surface and resolve flagged scope items, gaps, and conflicts before
            sending the trade packages out.
          </p>
        </div>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 opacity-50" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="w-64 pl-7 text-sm"
          />
        </div>
      </header>

      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v as typeof activeTab)}
      >
        <TabsList>
          <TabsTrigger value="conflicts" className="flex items-center gap-2">
            <AlertTriangle className="size-3.5" />
            Conflicts
            <span className="ml-1 rounded-full bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-rose-700 dark:text-rose-300">
              {conflicts?.length ?? 0}
            </span>
          </TabsTrigger>
          <TabsTrigger value="gaps" className="flex items-center gap-2">
            <ClipboardCheck className="size-3.5" />
            Gaps
            <span className="ml-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-amber-700 dark:text-amber-300">
              {gaps?.length ?? 0}
            </span>
          </TabsTrigger>
          <TabsTrigger value="low" className="flex items-center gap-2">
            <TrendingUp className="size-3.5" />
            Low confidence
            <span className="ml-1 rounded-full bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-rose-700 dark:text-rose-300">
              {lowConf?.length ?? 0}
            </span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="conflicts" className="mt-4 space-y-3">
          {filteredConflicts.length === 0 ? (
            <EmptyState
              icon={Check}
              title="No open conflicts"
              hint="Items don't disagree on quantity, unit, or division."
            />
          ) : (
            filteredConflicts.map((c) => (
              <ConflictCard key={c.id} conflict={c} projectId={projectId} />
            ))
          )}
        </TabsContent>

        <TabsContent value="gaps" className="mt-4 space-y-3">
          {filteredGaps.length === 0 ? (
            <EmptyState
              icon={Check}
              title="No open gaps at blocker/warn severity"
            />
          ) : (
            filteredGaps.map((g) => (
              <GapCard key={g.id} gap={g} projectId={projectId} />
            ))
          )}
        </TabsContent>

        <TabsContent value="low" className="mt-4 space-y-3">
          {filteredLow.length === 0 ? (
            <EmptyState
              icon={Check}
              title="No low-confidence items"
              hint="Every item has bilateral evidence or strong validator support."
            />
          ) : (
            filteredLow.map((it) => (
              <LowConfidenceCard key={it.id} item={it} />
            ))
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

function EmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
}) {
  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
        <Icon className="size-8 text-emerald-500" />
        <div className="text-sm font-medium">{title}</div>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </CardContent>
    </Card>
  );
}

export default function ReviewPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <ReviewView projectId={id} />
    </AuthGuard>
  );
}
