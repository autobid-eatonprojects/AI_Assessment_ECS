"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/format";

interface Props {
  projectId: string;
}

export function DocumentUpload({ projectId }: Props) {
  const qc = useQueryClient();

  const upload = useMutation({
    mutationFn: (files: File[]) => api.uploadDocuments(projectId, files),
    onSuccess: (docs) => {
      toast.success(`Uploaded ${docs.length} file${docs.length === 1 ? "" : "s"}`);
      qc.invalidateQueries({ queryKey: ["documents", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e: Error) => {
      toast.error(e.message || "Upload failed");
    },
  });

  const onDrop = useCallback(
    (accepted: File[]) => {
      if (accepted.length > 0) upload.mutate(accepted);
    },
    [upload],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: true,
    disabled: upload.isPending,
  });

  return (
    <div
      {...getRootProps()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-center transition-colors ${
        isDragActive
          ? "border-primary bg-primary/5"
          : "border-muted-foreground/30 hover:bg-muted/50"
      } ${upload.isPending ? "pointer-events-none opacity-60" : ""}`}
    >
      <input {...getInputProps()} />
      <UploadCloud className="mb-2 size-8 text-muted-foreground" />
      {upload.isPending ? (
        <p className="text-sm text-muted-foreground">Uploading…</p>
      ) : isDragActive ? (
        <p className="text-sm">Drop files here</p>
      ) : (
        <>
          <p className="text-sm font-medium">
            Drag &amp; drop files, or click to browse
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            PDFs, drawings, bids, trade lists — up to {formatBytes(200 * 1024 * 1024)} each
          </p>
        </>
      )}
    </div>
  );
}
