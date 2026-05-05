"use client";

import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useCallback, useEffect } from "react";
import { AuthImage } from "@/components/auth-image";
import { Button } from "@/components/ui/button";

interface Props {
  projectId: string;
  documentId: string;
  pageNumber: number;
  totalPages: number;
  filename: string;
  onClose: () => void;
  onNavigate: (pageNumber: number) => void;
}

export function PageViewerModal({
  projectId,
  documentId,
  pageNumber,
  totalPages,
  filename,
  onClose,
  onNavigate,
}: Props) {
  const goPrev = useCallback(() => {
    if (pageNumber > 1) onNavigate(pageNumber - 1);
  }, [pageNumber, onNavigate]);

  const goNext = useCallback(() => {
    if (pageNumber < totalPages) onNavigate(pageNumber + 1);
  }, [pageNumber, totalPages, onNavigate]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") goPrev();
      else if (e.key === "ArrowRight") goNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, goPrev, goNext]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/95"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="flex items-center justify-between border-b border-white/10 px-4 py-3 text-white"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{filename}</p>
          <p className="text-xs text-white/60">
            Page {pageNumber} of {totalPages}
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            onClick={goPrev}
            disabled={pageNumber <= 1}
            className="text-white hover:bg-white/10 hover:text-white"
          >
            <ChevronLeft className="size-5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={goNext}
            disabled={pageNumber >= totalPages}
            className="text-white hover:bg-white/10 hover:text-white"
          >
            <ChevronRight className="size-5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="text-white hover:bg-white/10 hover:text-white"
          >
            <X className="size-5" />
          </Button>
        </div>
      </div>
      <div
        className="flex flex-1 items-center justify-center overflow-auto p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <AuthImage
          projectId={projectId}
          documentId={documentId}
          pageNumber={pageNumber}
          variant="image"
          alt={`Page ${pageNumber} of ${filename}`}
          className="max-h-full max-w-full object-contain"
        />
      </div>
    </div>
  );
}
