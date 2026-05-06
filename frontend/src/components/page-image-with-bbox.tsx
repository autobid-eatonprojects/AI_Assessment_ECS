"use client";

import { useEffect, useRef, useState } from "react";
import { ZoomIn, X } from "lucide-react";
import { api } from "@/lib/api";
import type { BoundingBox } from "@/lib/types";

interface HighlightTarget {
  bbox: BoundingBox;
  color?: string;
  label?: string;
  /** Full text content of the underlying element. Surfaces as a tooltip
   *  next to the bbox + becomes the title of the zoom modal. Important
   *  for general-notes / fine-print where the bbox lands on text that's
   *  too small to read at thumbnail render scale. */
  text?: string;
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
 *
 * Click the bbox to open a zoom modal — useful when the underlying
 * content is small print (general-notes, title-block tables) that
 * isn't readable at thumbnail scale.
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
  const [zoomOpen, setZoomOpen] = useState(false);

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
      <div className="flex h-full animate-pulse items-center justify-center bg-muted text-sm text-muted-foreground">
        Loading page…
      </div>
    );
  }

  const color = highlight?.color ?? "#2563eb";

  return (
    <>
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
          <button
            type="button"
            title={
              highlight.text
                ? `Click to zoom · ${highlight.text.slice(0, 80)}${
                    highlight.text.length > 80 ? "…" : ""
                  }`
                : "Click to zoom"
            }
            onClick={() => setZoomOpen(true)}
            className="group absolute cursor-zoom-in rounded-sm border-2 ring-2 ring-offset-1 transition-all hover:ring-4"
            style={{
              left: `${highlight.bbox.x * 100}%`,
              top: `${highlight.bbox.y * 100}%`,
              width: `${highlight.bbox.width * 100}%`,
              height: `${highlight.bbox.height * 100}%`,
              borderColor: color,
              background: `${color}1A`,
            }}
          >
            {highlight.label && (
              <span
                className="absolute -top-6 left-0 inline-flex max-w-xs items-center gap-1 truncate rounded px-1.5 py-0.5 text-xs font-medium text-white shadow"
                style={{ background: color }}
              >
                <ZoomIn className="size-3 shrink-0" />
                {highlight.label}
              </span>
            )}
            {highlight.text && (
              <span
                className="pointer-events-none absolute left-0 top-full z-10 mt-1 hidden max-h-40 w-72 overflow-y-auto rounded-md border bg-popover p-2 text-left text-xs leading-snug text-popover-foreground shadow-lg group-hover:block"
              >
                {highlight.text}
              </span>
            )}
          </button>
        )}
      </div>

      {zoomOpen && highlight && src && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setZoomOpen(false)}
        >
          <div
            className="relative flex h-full max-h-[90vh] w-full max-w-5xl flex-col gap-2 rounded-lg bg-background p-4 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold">
                  {highlight.label ? `Zoomed: ${highlight.label}` : "Zoomed view"}
                </h3>
                {highlight.text && (
                  <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap text-xs text-muted-foreground">
                    {highlight.text}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => setZoomOpen(false)}
                className="rounded-md p-1 hover:bg-muted"
                aria-label="Close zoom"
              >
                <X className="size-4" />
              </button>
            </div>
            <ZoomedRegion src={src} bbox={highlight.bbox} color={color} />
            <p className="text-[11px] text-muted-foreground">
              Click outside or press Esc to close
            </p>
          </div>
        </div>
      )}
    </>
  );
}

/** Renders the page image scaled up so the bbox occupies the full
 *  visible area (with margin). Pure CSS transform — no extra fetch. */
function ZoomedRegion({
  src,
  bbox,
  color,
}: {
  src: string;
  bbox: BoundingBox;
  color: string;
}) {
  // Pad the bbox by 8% on each side so context is visible
  const PAD = 0.08;
  const left = Math.max(0, bbox.x - PAD);
  const top = Math.max(0, bbox.y - PAD);
  const right = Math.min(1, bbox.x + bbox.width + PAD);
  const bottom = Math.min(1, bbox.y + bbox.height + PAD);
  const cropW = right - left;
  const cropH = bottom - top;

  return (
    <div className="relative flex-1 overflow-hidden rounded-md bg-zinc-100">
      <div
        className="absolute"
        style={{
          left: `${(-left / cropW) * 100}%`,
          top: `${(-top / cropH) * 100}%`,
          width: `${(1 / cropW) * 100}%`,
          height: `${(1 / cropH) * 100}%`,
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt=""
          className="h-full w-full object-fill"
          draggable={false}
        />
        {/* Highlight overlay re-positioned in the cropped frame */}
        <div
          className="pointer-events-none absolute rounded-sm border-2"
          style={{
            left: `${bbox.x * 100}%`,
            top: `${bbox.y * 100}%`,
            width: `${bbox.width * 100}%`,
            height: `${bbox.height * 100}%`,
            borderColor: color,
            background: `${color}26`,
          }}
        />
      </div>
    </div>
  );
}
