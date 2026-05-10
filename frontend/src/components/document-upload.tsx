"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { DocumentSource } from "@/lib/types";

interface Props {
  projectId: string;
  source?: DocumentSource;
  vendorName?: string;
  disabled?: boolean;
  hint?: string;
}

export function DocumentUpload({
  projectId,
  source = "project_document",
  vendorName,
  disabled = false,
  hint,
}: Props) {
  const qc = useQueryClient();

  const upload = useMutation({
    mutationFn: (files: File[]) =>
      api.uploadDocuments(projectId, files, {
        source,
        vendor_name: vendorName,
      }),
    onSuccess: (docs) => {
      toast.success(`Uploaded ${docs.length} file${docs.length === 1 ? "" : "s"}`);
      qc.invalidateQueries({ queryKey: ["documents", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
    },
    onError: (e: Error) => {
      toast.error(e.message || "Upload failed");
    },
  });

  // Phase 11: vendor_name is no longer required for bid submissions —
  // the classifier auto-extracts vendor from each document's letterhead.
  // Operator-typed vendor still works as an override.
  const isLocked = disabled;

  const onDrop = useCallback(
    (accepted: File[]) => {
      if (isLocked) return;
      if (accepted.length > 0) upload.mutate(accepted);
    },
    [upload, isLocked],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: true,
    disabled: isLocked || upload.isPending,
  });

  const defaultHint =
    source === "bid_submission"
      ? "Vendor's bid PDFs / scope letters / insurance / safety docs"
      : "Drawings, project manual, trade list — up to 200 MB each";

  return (
    <div
      {...getRootProps()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-center transition-colors ${
        isDragActive
          ? "border-primary bg-primary/5"
          : "border-muted-foreground/30 hover:bg-muted/50"
      } ${isLocked ? "cursor-not-allowed opacity-50" : ""} ${upload.isPending ? "pointer-events-none opacity-60" : ""}`}
    >
      <input {...getInputProps()} />
      <UploadCloud className="mb-2 size-8 text-muted-foreground" />
      {upload.isPending ? (
        <p className="text-sm text-muted-foreground">Uploading…</p>
      ) : isDragActive ? (
        <p className="text-sm">Drop files here</p>
      ) : isLocked ? (
        <p className="text-sm text-muted-foreground">
          {hint || "Upload disabled"}
        </p>
      ) : (
        <>
          <p className="text-sm font-medium">
            Drag &amp; drop files, or click to browse
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {hint || defaultHint} · max {formatBytes(200 * 1024 * 1024)} each
          </p>
        </>
      )}
    </div>
  );
}
