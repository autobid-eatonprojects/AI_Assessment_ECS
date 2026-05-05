"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, Trash2 } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { ClassificationBadge } from "@/components/classification-badge";
import { ProcessingStatusIndicator } from "@/components/processing-status";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { formatBytes, formatRelativeTime } from "@/lib/format";

interface Props {
  projectId: string;
}

export function DocumentList({ projectId }: Props) {
  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);

  const { data, isLoading } = useQuery({
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
          d.processing_status === "rendering",
      );
      return processing ? 2_000 : false;
    },
  });

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
    const res = await fetch(
      `${process.env.NEXT_PUBLIC_API_URL}/api/projects/${projectId}/documents/${id}/download`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (!res.ok) {
      toast.error("Download failed");
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
  };

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading documents…</p>;
  }

  if (!data || data.length === 0) {
    return (
      <p className="rounded-md border border-dashed py-8 text-center text-sm text-muted-foreground">
        No documents yet. Upload some files to get started.
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
            title="Download"
          >
            <Download className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => {
              if (confirm(`Delete ${d.filename}?`)) del.mutate(d.id);
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
