"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  RefreshCw,
} from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { ExtractedScheduleTable } from "@/components/extracted-schedule-table";
import { ExtractionStatusBadge } from "@/components/extraction-status-badge";
import { PageImageWithBbox } from "@/components/page-image-with-bbox";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { BoundingBox } from "@/lib/types";

const ENTITY_COLORS: Record<string, string> = {
  material: "#10b981",
  manufacturer: "#8b5cf6",
  code: "#f59e0b",
  dimension: "#0ea5e9",
  room: "#ec4899",
  equipment: "#f97316",
  symbol: "#6366f1",
  other: "#64748b",
};

interface Highlight {
  bbox: BoundingBox;
  color?: string;
  label?: string;
  text?: string;
}


function PageTextPanel({
  text,
  source,
  isLoading,
  docStatus,
}: {
  text: string | null;
  source: string | null;
  isLoading: boolean;
  docStatus: string;
}) {
  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading page text…</p>;
  }
  if (!text) {
    if (docStatus === "ocr") {
      return (
        <div className="space-y-2">
          <p className="text-sm font-medium">OCR in progress…</p>
          <p className="text-xs text-muted-foreground">
            Gemini 2.5 Flash is transcribing this page. Text will appear here
            within ~5 seconds of completion.
          </p>
        </div>
      );
    }
    if (docStatus === "ready") {
      return (
        <p className="text-sm text-muted-foreground">
          No text extracted from this page. Likely an empty / image-only page
          that returned no readable content.
        </p>
      );
    }
    return (
      <p className="text-sm text-muted-foreground">
        Text not yet available — current status: {docStatus}
      </p>
    );
  }

  // Pretty preserve whitespace & line breaks; mono font for spec readability.
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{text.length.toLocaleString()} characters</span>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard.writeText(text);
            toast.success("Copied to clipboard");
          }}
          className="rounded px-2 py-0.5 hover:bg-muted"
        >
          Copy
        </button>
      </div>
      <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-snug text-foreground">
        {text}
      </pre>
    </div>
  );
}

function PageView({
  projectId,
  documentId,
  pageNumber,
}: {
  projectId: string;
  documentId: string;
  pageNumber: number;
}) {
  const qc = useQueryClient();
  const [hover, setHover] = useState<Highlight | null>(null);

  const docQuery = useQuery({
    queryKey: ["document", projectId, documentId],
    queryFn: () => api.getDocument(projectId, documentId),
  });

  const pagesQuery = useQuery({
    queryKey: ["pages", projectId, documentId],
    queryFn: () => api.listPages(projectId, documentId),
  });

  const isDrawingSet = docQuery.data?.doc_type === "drawing-set";

  const extractionQuery = useQuery({
    queryKey: ["extraction", projectId, documentId, pageNumber],
    queryFn: () => api.getPageExtraction(projectId, documentId, pageNumber),
    enabled: isDrawingSet,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "extracting" || s === "pending" ? 2_000 : false;
    },
  });

  // Per-page text (PyMuPDF or OCR). Used for non-drawing docs and as a
  // supplementary tab even on drawings when native text exists.
  const textQuery = useQuery({
    queryKey: ["page-text", projectId, documentId, pageNumber],
    queryFn: () => api.getPageText(projectId, documentId, pageNumber),
    enabled: !!docQuery.data,
    // While the doc is OCR'ing, the text appears progressively per-page.
    // Poll until this page has content or we know the doc is fully done.
    refetchInterval: (q) => {
      const text = q.state.data?.text;
      const docStatus = docQuery.data?.processing_status;
      const stillProcessing =
        docStatus === "ocr" ||
        docStatus === "pending" ||
        docStatus === "rendering" ||
        docStatus === "indexing";
      return !text && stillProcessing ? 3_000 : false;
    },
  });

  const reextract = useMutation({
    mutationFn: () => api.reextractPage(projectId, documentId, pageNumber),
    onSuccess: () => {
      toast.success("Re-extracting page");
      qc.invalidateQueries({ queryKey: ["extraction", projectId, documentId, pageNumber] });
      qc.invalidateQueries({ queryKey: ["extraction-overview", projectId, documentId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const doc = docQuery.data;
  const allPages = pagesQuery.data ?? [];
  const ext = extractionQuery.data;

  if (!doc || !pagesQuery.isFetched) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  const totalPages = doc.page_count ?? allPages.length;
  const prevPage = pageNumber > 1 ? pageNumber - 1 : null;
  const nextPage = pageNumber < totalPages ? pageNumber + 1 : null;

  return (
    <>
      <div className="mb-4 flex items-center justify-between gap-2">
        <Link
          href={`/projects/${projectId}/documents/${documentId}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to document
        </Link>
        <div className="flex items-center gap-2">
          {prevPage ? (
            <Link
              href={`/projects/${projectId}/documents/${documentId}/pages/${prevPage}`}
              className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-sm hover:bg-muted"
            >
              <ChevronLeft className="size-4" />
              Previous
            </Link>
          ) : (
            <span className="inline-flex h-7 items-center gap-1 px-2 text-sm text-muted-foreground/50">
              <ChevronLeft className="size-4" />
              Previous
            </span>
          )}
          <span className="text-sm text-muted-foreground">
            Page {pageNumber} of {totalPages}
          </span>
          {nextPage ? (
            <Link
              href={`/projects/${projectId}/documents/${documentId}/pages/${nextPage}`}
              className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-sm hover:bg-muted"
            >
              Next
              <ChevronRight className="size-4" />
            </Link>
          ) : (
            <span className="inline-flex h-7 items-center gap-1 px-2 text-sm text-muted-foreground/50">
              Next
              <ChevronRight className="size-4" />
            </span>
          )}
        </div>
      </div>

      <div className="mb-4 flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">
          {ext?.sheet_number ?? `Page ${pageNumber}`}
          {ext?.sheet_title ? ` — ${ext.sheet_title}` : ""}
          {!isDrawingSet && doc?.filename && (
            <span className="ml-2 text-base font-normal text-muted-foreground">
              · {doc.filename}
            </span>
          )}
        </h1>
        {ext?.discipline && (
          <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-700">
            {ext.discipline}
          </span>
        )}
        {ext?.drawing_scale && (
          <span className="text-xs text-muted-foreground">
            scale: <span className="font-mono">{ext.drawing_scale}</span>
          </span>
        )}
        {ext && <ExtractionStatusBadge status={ext.status} />}
        {ext?.cost_usd != null && (
          <span className="text-xs text-muted-foreground" title="Anthropic cost for this page">
            ${ext.cost_usd.toFixed(4)}
          </span>
        )}
        {!isDrawingSet && textQuery.data?.text_source && (
          <span
            className="rounded-full bg-cyan-100 px-2 py-0.5 text-xs font-medium text-cyan-900 dark:bg-cyan-900/40 dark:text-cyan-200"
            title="How the text on this page was produced"
          >
            text:{" "}
            {textQuery.data.text_source === "ocr-gemini"
              ? "OCR (Gemini Flash)"
              : textQuery.data.text_source === "ocr-anthropic"
                ? "OCR (Claude)"
                : "PyMuPDF (native)"}
          </span>
        )}
        {isDrawingSet && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => reextract.mutate()}
            disabled={
              reextract.isPending ||
              ext?.status === "extracting" ||
              ext?.status === "pending"
            }
            className="ml-auto"
          >
            <RefreshCw
              className={`size-4 ${reextract.isPending ? "animate-spin" : ""}`}
            />
            <span className="ml-1.5">Re-extract</span>
          </Button>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[3fr_2fr]">
        <div className="rounded-md border bg-zinc-100">
          <PageImageWithBbox
            projectId={projectId}
            documentId={documentId}
            pageNumber={pageNumber}
            highlight={hover}
          />
        </div>

        <div className="min-w-0 max-h-[80vh] overflow-y-auto rounded-md border bg-card p-4">
          {!isDrawingSet ? (
            // Non-drawing docs: show extracted text panel
            <PageTextPanel
              text={textQuery.data?.text ?? null}
              source={textQuery.data?.text_source ?? null}
              isLoading={textQuery.isLoading}
              docStatus={doc.processing_status}
            />
          ) : !ext ? (
            <p className="text-sm text-muted-foreground">Loading extraction…</p>
          ) : ext.status === "pending" ? (
            <p className="text-sm text-muted-foreground">Queued for extraction…</p>
          ) : ext.status === "extracting" ? (
            <p className="text-sm text-muted-foreground">Vision pre-pass running…</p>
          ) : ext.status === "failed" ? (
            <div className="space-y-2">
              <p className="text-sm font-medium text-destructive">Extraction failed</p>
              <p className="text-xs text-muted-foreground">{ext.error}</p>
              <Button size="sm" onClick={() => reextract.mutate()} disabled={reextract.isPending}>
                Retry
              </Button>
            </div>
          ) : (
            <Tabs defaultValue="schedules" className="w-full">
              <TabsList>
                <TabsTrigger value="schedules">
                  Schedules ({ext.schedules.length})
                </TabsTrigger>
                <TabsTrigger value="notes">
                  Notes ({ext.notes.length})
                </TabsTrigger>
                <TabsTrigger value="refs">
                  Refs ({ext.cross_references.length})
                </TabsTrigger>
                <TabsTrigger value="entities">
                  Entities ({ext.entities.length})
                </TabsTrigger>
                <TabsTrigger value="raw">Raw</TabsTrigger>
              </TabsList>

              <TabsContent value="schedules" className="space-y-3 pt-3">
                {ext.schedules.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No schedules on this page.</p>
                ) : (
                  ext.schedules.map((s) => (
                    <div
                      key={s.id}
                      onMouseEnter={() =>
                        s.bbox &&
                        setHover({
                          bbox: s.bbox,
                          label: s.name,
                          text:
                            `Schedule: ${s.name}\n` +
                            (s.rows && s.rows.length
                              ? `${s.rows.length} row${s.rows.length === 1 ? "" : "s"} extracted`
                              : "no rows"),
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <ExtractedScheduleTable schedule={s} />
                    </div>
                  ))
                )}
              </TabsContent>

              <TabsContent value="notes" className="space-y-2 pt-3">
                {ext.notes.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No notes on this page.</p>
                ) : (
                  ext.notes.map((n) => (
                    <div
                      key={n.id}
                      className="rounded-md border bg-background p-3 text-sm hover:border-blue-400"
                      onMouseEnter={() =>
                        n.bbox &&
                        setHover({
                          bbox: n.bbox,
                          color: "#2563eb",
                          label: "Note",
                          text: n.text,
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      {n.text}
                    </div>
                  ))
                )}
              </TabsContent>

              <TabsContent value="refs" className="space-y-2 pt-3">
                {ext.cross_references.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No cross-references on this page.</p>
                ) : (
                  ext.cross_references.map((cr) => (
                    <div
                      key={cr.id}
                      className="rounded-md border bg-background p-3 text-sm hover:border-amber-400"
                      onMouseEnter={() =>
                        cr.bbox &&
                        setHover({
                          bbox: cr.bbox,
                          color: "#d97706",
                          label: cr.target_sheet,
                          text:
                            `→ ${cr.target_sheet}` +
                            (cr.detail_id ? ` / ${cr.detail_id}` : "") +
                            (cr.context ? `\n\n${cr.context}` : ""),
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <div className="font-mono text-xs font-medium">
                        {cr.target_sheet}
                        {cr.detail_id ? ` / ${cr.detail_id}` : ""}
                      </div>
                      {cr.context && (
                        <div className="mt-1 text-xs text-muted-foreground">{cr.context}</div>
                      )}
                    </div>
                  ))
                )}
              </TabsContent>

              <TabsContent value="entities" className="space-y-1 pt-3">
                {ext.entities.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No entities on this page.</p>
                ) : (
                  ext.entities.map((e) => (
                    <div
                      key={e.id}
                      className="flex items-start gap-2 rounded-md border bg-background px-2 py-1.5 text-xs hover:border-blue-400"
                      onMouseEnter={() =>
                        e.bbox &&
                        setHover({
                          bbox: e.bbox,
                          color: ENTITY_COLORS[e.entity_type] ?? "#2563eb",
                          label: e.value.slice(0, 40),
                          text: `${e.entity_type}: ${e.value}`,
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <span
                        className="mt-0.5 inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white"
                        style={{
                          background: ENTITY_COLORS[e.entity_type] ?? "#64748b",
                        }}
                      >
                        {e.entity_type}
                      </span>
                      <span className="flex-1">{e.value}</span>
                    </div>
                  ))
                )}
              </TabsContent>

              <TabsContent value="raw" className="pt-3">
                <div className="space-y-2 text-xs">
                  <div>
                    <span className="text-muted-foreground">Model:</span>{" "}
                    <code className="rounded bg-muted px-1">{ext.model}</code>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Latency:</span>{" "}
                    {ext.latency_ms} ms
                  </div>
                  <div>
                    <span className="text-muted-foreground">Cost:</span>{" "}
                    ${ext.cost_usd?.toFixed(5)}
                  </div>
                  <div>
                    <span className="text-muted-foreground">Extracted:</span>{" "}
                    {ext.extracted_at}
                  </div>
                </div>
              </TabsContent>
            </Tabs>
          )}
        </div>
      </div>
    </>
  );
}

export default function PageDetail({
  params,
}: {
  params: Promise<{ id: string; docId: string; pageNum: string }>;
}) {
  const { id, docId, pageNum } = use(params);
  const pageNumber = parseInt(pageNum, 10);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl flex-1 p-6">
        <PageView projectId={id} documentId={docId} pageNumber={pageNumber} />
      </main>
    </AuthGuard>
  );
}
