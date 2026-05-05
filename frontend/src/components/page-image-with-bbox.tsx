"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { BoundingBox } from "@/lib/types";

interface HighlightTarget {
  bbox: BoundingBox;
  color?: string;
  label?: string;
}

interface Props {
  projectId: string;
  documentId: string;
  pageNumber: number;
  highlight: HighlightTarget | null;
}

/**
 * Renders the full-resolution page image with an absolutely-positioned
 * bounding-box overlay. The image is fetched with auth (blob URL) and the
 * overlay is drawn in normalised [0, 1] coordinates so it stays correct
 * regardless of how the image is scaled by CSS.
 */
export function PageImageWithBbox({
  projectId,
  documentId,
  pageNumber,
  highlight,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    setError(false);
    setSrc(null);

    api
      .fetchPageImage(projectId, documentId, pageNumber, "image")
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => !cancelled && setError(true));

    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [projectId, documentId, pageNumber]);

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Failed to load page image
      </div>
    );
  }

  if (!src) {
    return (
      <div
        className="flex h-full animate-pulse items-center justify-center bg-muted text-sm text-muted-foreground"
      >
        Loading page…
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative inline-block max-h-full max-w-full overflow-auto bg-zinc-100"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={`Page ${pageNumber}`}
        className="block h-auto w-full"
        draggable={false}
      />
      {highlight && (
        <div
          className="pointer-events-none absolute rounded-sm border-2 ring-2 ring-offset-1 transition-all"
          style={{
            left: `${highlight.bbox.x * 100}%`,
            top: `${highlight.bbox.y * 100}%`,
            width: `${highlight.bbox.width * 100}%`,
            height: `${highlight.bbox.height * 100}%`,
            borderColor: highlight.color ?? "#2563eb",
            background: `${(highlight.color ?? "#2563eb")}1A`,
          }}
        >
          {highlight.label && (
            <span
              className="absolute -top-6 left-0 max-w-xs truncate rounded px-1.5 py-0.5 text-xs font-medium text-white shadow"
              style={{ background: highlight.color ?? "#2563eb" }}
            >
              {highlight.label}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
