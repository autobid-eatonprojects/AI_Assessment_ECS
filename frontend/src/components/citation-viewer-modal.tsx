"use client";

import { ExternalLink, MapPin, X } from "lucide-react";
import Link from "next/link";
import { PageImageWithBbox } from "@/components/page-image-with-bbox";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ScopeCitation, ScopeItem } from "@/lib/types";

// ----- Pass B: per-citation badges -----------------------------------------
// Renders evidence_type (drawing/spec/bid/other) + link_judge entailment
// status. Both export from this file so the inline citation list in
// scope/page.tsx can also use them.

const EVIDENCE_TYPE_META: Record<
  string,
  { label: string; classes: string; tooltip: string }
> = {
  drawing: {
    label: "Drawing",
    classes:
      "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/30",
    tooltip: "Citation comes from a drawing-set document",
  },
  spec: {
    label: "Spec",
    classes:
      "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/30",
    tooltip: "Citation comes from a written-spec document",
  },
  bid: {
    label: "Bid",
    classes:
      "bg-slate-500/10 text-slate-700 dark:text-slate-300 border-slate-500/30",
    tooltip: "Citation comes from a bid submission",
  },
  other: {
    label: "Other",
    classes:
      "bg-zinc-500/10 text-zinc-700 dark:text-zinc-300 border-zinc-500/30",
    tooltip: "Citation source not categorized",
  },
};

export function EvidenceTypeBadge({ type }: { type: string | null | undefined }) {
  if (!type) return null;
  const meta = EVIDENCE_TYPE_META[type] ?? EVIDENCE_TYPE_META.other;
  return (
    <span
      title={meta.tooltip}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${meta.classes}`}
    >
      {meta.label}
    </span>
  );
}

/** Link-judge entailment badge. Renders only when link_judge has run. */
export function LinkJudgeBadge({
  pass,
  score,
}: {
  pass: boolean | null | undefined;
  score?: number | null;
}) {
  if (pass === null || pass === undefined) return null;
  const conf = score != null ? ` (${Math.round(score * 100)}%)` : "";
  if (pass) {
    return (
      <span
        title={`Link judge: cited chunk supports the item${conf}`}
        className="inline-flex items-center gap-1 rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300"
      >
        ✓ Entailed{conf}
      </span>
    );
  }
  return (
    <span
      title={`Link judge: cited chunk did not support the item${conf}`}
      className="inline-flex items-center gap-1 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300"
    >
      ✗ Failed{conf}
    </span>
  );
}

// ---------------------------------------------------------------------------

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  scopeItem: ScopeItem;
  citation: ScopeCitation;
}

/**
 * Phase 5 — citation grounding viewer.
 *
 * Opens when an estimator clicks a citation in the scope explorer. Shows the
 * full PDF page on the left with the citation's bounding box highlighted, and
 * the scope item's metadata + excerpt on the right. Page-text / page-summary
 * citations don't carry a bbox — for those we render the full page without
 * the highlight overlay rather than fake a precision the data doesn't have.
 */
export function CitationViewerModal({
  open,
  onOpenChange,
  projectId,
  scopeItem,
  citation,
}: Props) {
  const hasBbox = !!citation.bbox;
  const hasPage = citation.document_id && citation.page_number != null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-[min(1400px,96vw)] overflow-hidden p-0 sm:max-w-[min(1400px,96vw)]">
        <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr]">
          {/* Left: PDF page with bbox overlay */}
          <div className="relative min-h-[60vh] max-h-[92vh] overflow-auto bg-zinc-950 p-3">
            {hasPage ? (
              <PageImageWithBbox
                projectId={projectId}
                documentId={citation.document_id!}
                pageNumber={citation.page_number!}
                highlight={
                  hasBbox
                    ? {
                        bbox: citation.bbox!,
                        color: "#2563eb",
                        label: scopeItem.csi_code,
                      }
                    : null
                }
              />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Citation has no document/page reference
              </div>
            )}
            {!hasBbox && hasPage && (
              <div className="absolute right-4 top-4 rounded-md bg-amber-100/95 px-3 py-1.5 text-[11px] text-amber-900 shadow dark:bg-amber-900/80 dark:text-amber-100">
                Source not bbox-grounded — showing full page
              </div>
            )}
          </div>

          {/* Right: scope item detail + citation metadata */}
          <div className="flex flex-col overflow-hidden border-l bg-card">
            <div className="flex items-start justify-between border-b p-4">
              <div>
                <DialogTitle className="text-sm font-semibold">
                  Source citation
                </DialogTitle>
                <DialogDescription className="mt-0.5 text-xs text-muted-foreground">
                  Click <kbd className="rounded border px-1">Esc</kbd> or the{" "}
                  <X className="inline size-3" /> to close
                </DialogDescription>
              </div>
            </div>

            <div className="space-y-4 overflow-y-auto p-4 text-sm">
              {/* Scope item summary */}
              <section>
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
                  <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono dark:bg-zinc-800">
                    {scopeItem.csi_code}
                  </span>
                  {scopeItem.section_title && (
                    <span className="text-muted-foreground">
                      {scopeItem.section_title}
                    </span>
                  )}
                </div>
                <h3 className="text-base font-semibold">{scopeItem.description}</h3>
                {scopeItem.specification && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {scopeItem.specification}
                  </p>
                )}
              </section>

              {/* Citation metadata */}
              <section className="space-y-2 border-t pt-3">
                <h4 className="text-xs uppercase tracking-wide text-muted-foreground">
                  Source location
                </h4>
                {/* Pass B: link_judge + evidence_type badges */}
                <div className="flex flex-wrap items-center gap-1.5">
                  <EvidenceTypeBadge type={citation.evidence_type} />
                  <LinkJudgeBadge
                    pass={citation.is_link_judge_pass}
                    score={citation.link_judge_score}
                  />
                </div>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                  {citation.sheet_number && (
                    <>
                      <dt className="text-muted-foreground">Sheet</dt>
                      <dd className="font-mono">{citation.sheet_number}</dd>
                    </>
                  )}
                  {citation.page_number != null && (
                    <>
                      <dt className="text-muted-foreground">Page</dt>
                      <dd className="font-mono">{citation.page_number}</dd>
                    </>
                  )}
                  {citation.extraction_query && (
                    <>
                      <dt className="text-muted-foreground">Found by</dt>
                      <dd className="text-muted-foreground">
                        {citation.extraction_query}-query
                      </dd>
                    </>
                  )}
                  {citation.rerank_score != null && (
                    <>
                      <dt className="text-muted-foreground">Rerank score</dt>
                      <dd className="font-mono">
                        {citation.rerank_score.toFixed(3)}
                      </dd>
                    </>
                  )}
                  {citation.bbox && (
                    <>
                      <dt className="text-muted-foreground">Bounding box</dt>
                      <dd className="flex items-center gap-1 font-mono text-[11px]">
                        <MapPin className="size-3 text-blue-600" />
                        {(citation.bbox.x * 100).toFixed(0)},{" "}
                        {(citation.bbox.y * 100).toFixed(0)} ·{" "}
                        {(citation.bbox.width * 100).toFixed(0)}×
                        {(citation.bbox.height * 100).toFixed(0)}%
                      </dd>
                    </>
                  )}
                </dl>
              </section>

              {/* Excerpt */}
              {citation.excerpt && (
                <section className="border-t pt-3">
                  <h4 className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">
                    Excerpt
                  </h4>
                  <p className="rounded bg-muted/40 p-2 font-mono text-xs leading-relaxed">
                    {citation.excerpt}
                  </p>
                </section>
              )}

              {/* Quantity if available */}
              {scopeItem.quantity && (
                <section className="border-t pt-3">
                  <h4 className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">
                    Quantity
                  </h4>
                  <p className="font-mono text-sm">
                    {scopeItem.quantity} {scopeItem.unit}
                  </p>
                </section>
              )}
            </div>

            {/* Footer actions */}
            <div className="mt-auto flex items-center justify-between gap-2 border-t bg-muted/30 p-3 text-xs">
              {hasPage ? (
                <Link
                  href={`/projects/${projectId}/documents/${citation.document_id}/pages/${citation.page_number}`}
                  className="inline-flex items-center gap-1 font-medium text-blue-700 hover:underline dark:text-blue-400"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open page in viewer
                  <ExternalLink className="size-3" />
                </Link>
              ) : (
                <span className="text-muted-foreground">No page link</span>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Close
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
