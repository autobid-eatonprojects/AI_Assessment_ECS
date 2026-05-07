"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, Loader2, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { ClassificationBadge } from "@/components/classification-badge";
import { ProcessingStatusIndicator } from "@/components/processing-status";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { formatBytes, formatRelativeTime } from "@/lib/format";

interface Props {
  projectId: string;
  source?: "project_document" | "bid_submission";
  emptyHint?: string;
}

export function DocumentList({ projectId, source, emptyHint }: Props) {
  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);
  const [downloading, setDownloading] = useState<Set<string>>(new Set());

  const { data: allDocs, isLoading } = useQuery({
    queryKey: ["documents", projectId],
    queryFn: () => api.listDocuments(projectId),
    // Poll while anything is processing
    refetchInterval: (query) => {
      const docs = query.state.data;
      if (!docs) return false;
      const processing = docs.some(
        (d) =>
          d.processing_status === "pending" ||
          d.processing_status === "classifying" ||
          d.processing_status === "rendering" ||
          d.processing_status === "ocr" ||
          d.processing_status === "indexing",
      );
      return processing ? 2_000 : false;
    },
  });

  const data = source ? allDocs?.filter((d) => d.source === source) : allDocs;

  const del = useMutation({
    mutationFn: (id: string) => api.deleteDocument(projectId, id),
    onSuccess: () => {
      toast.success("Document deleted");
      qc.invalidateQueries({ queryKey: ["documents", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const download = async (id: string, filename: string) => {
    if (downloading.has(id)) return;
    setDownloading((s) => new Set(s).add(id));
    try {
      const res = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL}/api/projects/${projectId}/documents/${id}/download`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!res.ok) {
        toast.error(`Download failed (HTTP ${res.status})`);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`Download failed: ${(e as Error).message}`);
    } finally {
      setDownloading((s) => {
        const next = new Set(s);
        next.delete(id);
        return next;
      });
    }
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading documents…</p>;
  }

  if (!data || data.length === 0) {
    return (
      <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
        {emptyHint || "No documents yet. Upload some files to get started."}
      </p>
    );
  }

  return (
    <ul className="divide-y rounded-md border">
      {data.map((d) => (
        <li
          key={d.id}
          className="flex items-center gap-3 px-4 py-3 hover:bg-muted/50"
        >
          <FileText className="size-5 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Link
                href={`/projects/${projectId}/documents/${d.id}`}
                className="truncate font-medium hover:underline"
                title={d.filename}
              >
                {d.filename}
              </Link>
              <ClassificationBadge
                docType={d.doc_type}
                confidence={d.classification_confidence}
              />
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {d.vendor_name && (
                <>
                  <span className="font-medium text-foreground">
                    {d.vendor_name}
                  </span>
                  <span>·</span>
                </>
              )}
              <span>{formatBytes(d.size_bytes)}</span>
              <span>·</span>
              <span>{formatRelativeTime(d.created_at)}</span>
              {d.page_count != null && (
                <>
                  <span>·</span>
                  <span>{d.page_count} {d.page_count === 1 ? "page" : "pages"}</span>
                </>
              )}
              <span>·</span>
              <ProcessingStatusIndicator
                status={d.processing_status}
                error={d.processing_error}
              />
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => download(d.id, d.filename)}
            disabled={downloading.has(d.id)}
            title={downloading.has(d.id) ? "Downloading…" : "Download"}
          >
            {downloading.has(d.id) ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Download className="size-4" />
            )}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => {
              const inFlight =
                d.processing_status !== "ready" &&
                d.processing_status !== "failed";
              const msg = inFlight
                ? `Delete ${d.filename}?\n\nThis document is currently processing ` +
                  `(status: ${d.processing_status}). Deleting will cancel any ` +
                  `in-flight tasks and stop their API calls. Already-spent API ` +
                  `costs are not refunded.`
                : `Delete ${d.filename}?`;
              if (confirm(msg)) del.mutate(d.id);
            }}
            disabled={del.isPending}
            title="Delete"
          >
            <Trash2 className="size-4 text-destructive" />
          </Button>
        </li>
      ))}
    </ul>
  );
}
