"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Props {
  projectId: string;
  documentId: string;
  pageNumber: number;
  variant: "image" | "thumbnail";
  className?: string;
  alt?: string;
}

/** <img> wrapper that authenticates the fetch and renders via blob URL. */
export function AuthImage({ projectId, documentId, pageNumber, variant, className, alt }: Props) {
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    setError(false);
    setSrc(null);

    api
      .fetchPageImage(projectId, documentId, pageNumber, variant)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });

    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [projectId, documentId, pageNumber, variant]);

  if (error) {
    return (
      <div className={`flex items-center justify-center bg-muted text-xs text-muted-foreground ${className ?? ""}`}>
        Load failed
      </div>
    );
  }

  if (!src) {
    return (
      <div className={`animate-pulse bg-muted ${className ?? ""}`} aria-label="loading image" />
    );
  }

  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={alt ?? `Page ${pageNumber}`} className={className} loading="lazy" />;
}
