"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, RefreshCw } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { AuthImage } from "@/components/auth-image";
import { ClassificationBadge } from "@/components/classification-badge";
import { ExtractionOverview } from "@/components/extraction-overview";
import { PageViewerModal } from "@/components/page-viewer-modal";
import { ProcessingStatusIndicator } from "@/components/processing-status";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatBytes, formatRelativeTime } from "@/lib/format";

function isProcessing(status: string): boolean {
  return (
    status === "pending" ||
    status === "classifying" ||
    status === "rendering" ||
    status === "extracting"
  );
}

function DocumentDetail({
  projectId,
  documentId,
}: {
  projectId: string;
  documentId: string;
}) {
  const qc = useQueryClient();
  const [viewerPage, setViewerPage] = useState<number | null>(null);

  const docQuery = useQuery({
    queryKey: ["document", projectId, documentId],
    queryFn: () => api.getDocument(projectId, documentId),
    refetchInterval: (q) => (q.state.data && isProcessing(q.state.data.processing_status) ? 2_000 : false),
  });

  const pagesQuery = useQuery({
    queryKey: ["pages", projectId, documentId],
    queryFn: () => api.listPages(projectId, documentId),
    enabled: docQuery.data?.processing_status === "ready" || (docQuery.data?.page_count ?? 0) > 0,
    refetchInterval: (q) => {
      const doc = docQuery.data;
      const pages = q.state.data;
      if (!doc) return false;
      // Keep polling while pages haven't all arrived yet
      if (doc.page_count != null && pages && pages.length === doc.page_count) return false;
      return isProcessing(doc.processing_status) ? 2_000 : false;
    },
  });

  const reclassify = useMutation({
    mutationFn: () => api.reclassifyDocument(projectId, documentId),
    onSuccess: () => {
      toast.success("Re-processing started");
      qc.invalidateQueries({ queryKey: ["document", projectId, documentId] });
      qc.invalidateQueries({ queryKey: ["pages", projectId, documentId] });
      qc.invalidateQueries({ queryKey: ["documents", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const doc = docQuery.data;

  if (docQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (!doc) {
    return <p className="text-sm text-destructive">Document not found.</p>;
  }

  const pages = pagesQuery.data ?? [];

  return (
    <>
      <Link
        href={`/projects/${projectId}`}
        className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="size-4" /> Back to project
      </Link>

      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-semibold" title={doc.filename}>
            {doc.filename}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <ClassificationBadge
              docType={doc.doc_type}
              confidence={doc.classification_confidence}
            />
            <span>·</span>
            <ProcessingStatusIndicator
              status={doc.processing_status}
              error={doc.processing_error}
            />
            <span>·</span>
            <span>{formatBytes(doc.size_bytes)}</span>
            <span>·</span>
            <span>Uploaded {formatRelativeTime(doc.created_at)}</span>
            {doc.page_count != null && (
              <>
                <span>·</span>
                <span>{doc.page_count} {doc.page_count === 1 ? "page" : "pages"}</span>
              </>
            )}
          </div>
          {doc.classification_reasoning && (
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              <span className="font-medium text-foreground">Why this label:</span>{" "}
              {doc.classification_reasoning}
            </p>
          )}
          {doc.processing_error && (
            <p className="mt-2 text-sm text-destructive">
              {doc.processing_error}
            </p>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => reclassify.mutate()}
          disabled={reclassify.isPending || isProcessing(doc.processing_status)}
        >
          <RefreshCw
            className={`size-4 ${reclassify.isPending ? "animate-spin" : ""}`}
          />
          <span className="ml-1.5">Re-process</span>
        </Button>
      </div>

      {doc.doc_type === "drawing-set" && (
        <div className="mb-6">
          <ExtractionOverview
            projectId={projectId}
            documentId={documentId}
            documentStatus={doc.processing_status}
          />
        </div>
      )}

      {pages.length === 0 ? (
        <div className="rounded-md border border-dashed py-12 text-center text-sm text-muted-foreground">
          {isProcessing(doc.processing_status)
            ? "Rendering pages…"
            : doc.processing_status === "failed"
              ? "Page rendering failed. Try re-process."
              : "No previewable pages for this document type."}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {pages.map((p) => (
            <div key={p.id} className="space-y-1">
              <button
                onClick={() => setViewerPage(p.page_number)}
                className="group relative block w-full overflow-hidden rounded-md border bg-muted text-left transition-shadow hover:shadow-md"
                style={{ aspectRatio: `${p.width} / ${p.height}` }}
              >
                <AuthImage
                  projectId={projectId}
                  documentId={documentId}
                  pageNumber={p.page_number}
                  variant="thumbnail"
                  className="h-full w-full object-cover"
                  alt={`Page ${p.page_number}`}
                />
                <div className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-gradient-to-t from-black/70 to-transparent px-2 py-1.5 text-xs font-medium text-white">
                  <span>Page {p.page_number}</span>
                </div>
              </button>
              <Link
                href={`/projects/${projectId}/documents/${documentId}/pages/${p.page_number}`}
                className="block truncate text-center text-xs text-muted-foreground hover:text-foreground hover:underline"
              >
                {doc.doc_type === "drawing-set"
                  ? "Inspect extraction →"
                  : "View page text →"}
              </Link>
            </div>
          ))}
        </div>
      )}

      {viewerPage != null && (
        <PageViewerModal
          projectId={projectId}
          documentId={documentId}
          pageNumber={viewerPage}
          totalPages={pages.length}
          filename={doc.filename}
          onClose={() => setViewerPage(null)}
          onNavigate={setViewerPage}
        />
      )}
    </>
  );
}

export default function DocumentPage({
  params,
}: {
  params: Promise<{ id: string; docId: string }>;
}) {
  const { id, docId } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-6xl flex-1 p-6">
        <DocumentDetail projectId={id} documentId={docId} />
      </main>
    </AuthGuard>
  );
}
