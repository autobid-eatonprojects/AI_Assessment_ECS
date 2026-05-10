"use client";

import { ExternalLink, MapPin } from "lucide-react";
import type { ScopeCitation, ScopeItem } from "@/lib/types";

/**
 * Pass C — drawing-grounding evidence card.
 *
 * The drawing_grounder backend module synthesizes per-item drawing chunks
 * whose text follows a stable shape:
 *   "On sheet E3.1 (page 42): 3 instance(s) of {description} — locations: Restroom 116, Mens 111 — callout: ..."
 *
 * That text lands in ScopeCitation.excerpt (truncated to 240 chars). We
 * detect those citations here by their `On sheet ` prefix + extract the
 * sheet, count, and locations to render a structured "Found on drawings"
 * panel inside the scope item detail.
 *
 * Until drawing_grounder is wired into scope_runner (Phase 4), this card
 * just doesn't render — it gracefully degrades to empty.
 */
export function DrawingGroundingCard({
  item,
  onCitationClick,
}: {
  item: ScopeItem;
  onCitationClick: (c: ScopeCitation) => void;
}) {
  const groundings = item.citations
    .map((c) => parseGrounding(c))
    .filter((g): g is GroundingRow => g !== null);

  if (groundings.length === 0) return null;

  // De-duplicate by sheet (multiple citations for the same item on the
  // same sheet would be redundant in the UI).
  const bySheet = new Map<string, GroundingRow>();
  for (const g of groundings) {
    const existing = bySheet.get(g.sheet);
    if (existing == null || g.count > existing.count) {
      bySheet.set(g.sheet, g);
    }
  }

  const totalCount = Array.from(bySheet.values()).reduce(
    (sum, g) => sum + g.count,
    0,
  );

  return (
    <div className="rounded-md border bg-card p-3">
      <div className="mb-2 flex items-center gap-2 text-xs">
        <MapPin className="size-3.5 text-blue-600" />
        <span className="font-semibold">Found on drawings</span>
        <span className="text-muted-foreground">
          ({totalCount} {totalCount === 1 ? "instance" : "instances"} across {bySheet.size}{" "}
          {bySheet.size === 1 ? "sheet" : "sheets"})
        </span>
      </div>
      <ul className="space-y-1.5 text-xs">
        {Array.from(bySheet.values()).map((g) => (
          <li
            key={g.citation.id}
            className="flex items-start gap-2 rounded border bg-muted/20 px-2 py-1.5"
          >
            <span className="rounded bg-blue-100 px-1.5 py-0.5 font-mono font-medium text-blue-900 dark:bg-blue-900/40 dark:text-blue-200">
              {g.sheet}
            </span>
            <div className="flex-1">
              <div className="flex items-center gap-1.5">
                <span className="font-medium">
                  {g.count} {g.count === 1 ? "instance" : "instances"}
                </span>
                {g.locations.length > 0 && (
                  <span className="text-muted-foreground">
                    in {g.locations.join(", ")}
                  </span>
                )}
              </div>
              {g.callout && (
                <div className="mt-0.5 line-clamp-2 text-muted-foreground">
                  {g.callout}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={() => onCitationClick(g.citation)}
              className="inline-flex items-center gap-1 rounded border bg-card px-2 py-0.5 text-[11px] font-medium hover:bg-muted"
              title="Open this sheet"
            >
              <ExternalLink className="size-3" />
              View
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface GroundingRow {
  citation: ScopeCitation;
  sheet: string;
  count: number;
  locations: string[];
  callout: string | null;
}

// Match patterns like:
//   "On sheet E3.1 (page 42): 3 instance(s) of Floor outlet"
//   "On sheet P2.1 (page 53): 5 instance(s) of WC2: Water Closet — locations: Restroom 116, Mens 111"
//   "On sheet S1.1 (page 30): 1 instance(s) of Footing F2.0 — callout: ..."
const GROUNDING_RE =
  /^On sheet\s+(\S+)\s*\(page\s+\d+\):\s*(\d+)\s+instance\(s\)\s+of\s+/i;

function parseGrounding(c: ScopeCitation): GroundingRow | null {
  const text = c.excerpt;
  if (!text) return null;
  const m = text.match(GROUNDING_RE);
  if (!m) return null;

  const sheet = m[1];
  const count = parseInt(m[2], 10);
  if (Number.isNaN(count) || count <= 0) return null;

  // Pull locations after "— locations: " until the next "—" or end-of-string.
  const locMatch = text.match(/—\s*locations:\s*([^—]+)/i);
  const locations = locMatch
    ? locMatch[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    : [];

  // Pull callout if present.
  const calloutMatch = text.match(/—\s*callout:\s*(.+)$/i);
  const callout = calloutMatch ? calloutMatch[1].trim() : null;

  return { citation: c, sheet, count, locations, callout };
}
