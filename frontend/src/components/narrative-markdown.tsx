"use client";

/**
 * Lightweight markdown renderer for Haiku-drafted package narratives
 * and RFI bodies. Handles only the subset the backend produces:
 * `# H1`, `## H2`, paragraphs, `**bold**`, `- bullet lists`.
 *
 * Avoids pulling in `react-markdown` for ~150KB just for these
 * features; the LLM output we care about is a constrained subset.
 */

import { cn } from "@/lib/utils";

function inlineFormat(text: string): React.ReactNode {
  // Bold: **text**
  const parts: React.ReactNode[] = [];
  let last = 0;
  const re = /\*\*(.+?)\*\*/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    parts.push(
      <strong key={m.index} className="font-semibold text-foreground">
        {m[1]}
      </strong>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function NarrativeMarkdown({
  source,
  className,
}: {
  source: string;
  className?: string;
}) {
  const lines = source
    // Strip the HTML-comment subject hint we embed at the top
    .replace(/<!--\s*subject:.*?-->/i, "")
    .split("\n");

  const out: React.ReactNode[] = [];
  let bulletGroup: string[] = [];
  let paragraphGroup: string[] = [];

  function flushBullets() {
    if (bulletGroup.length === 0) return;
    out.push(
      <ul
        key={`ul-${out.length}`}
        className="my-2 ml-5 list-disc space-y-1 text-sm text-muted-foreground"
      >
        {bulletGroup.map((b, i) => (
          <li key={i}>{inlineFormat(b)}</li>
        ))}
      </ul>,
    );
    bulletGroup = [];
  }
  function flushParagraph() {
    if (paragraphGroup.length === 0) return;
    out.push(
      <p
        key={`p-${out.length}`}
        className="my-2 text-sm leading-relaxed text-muted-foreground"
      >
        {inlineFormat(paragraphGroup.join(" "))}
      </p>,
    );
    paragraphGroup = [];
  }

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flushBullets();
      flushParagraph();
      continue;
    }
    if (line.startsWith("# ")) {
      flushBullets();
      flushParagraph();
      out.push(
        <h2 key={`h1-${out.length}`} className="mt-4 text-lg font-semibold">
          {inlineFormat(line.slice(2))}
        </h2>,
      );
      continue;
    }
    if (line.startsWith("## ")) {
      flushBullets();
      flushParagraph();
      out.push(
        <h3
          key={`h2-${out.length}`}
          className="mt-3 text-sm font-semibold uppercase tracking-wide text-foreground/80"
        >
          {inlineFormat(line.slice(3))}
        </h3>,
      );
      continue;
    }
    if (line.startsWith("- ") || line.startsWith("* ")) {
      flushParagraph();
      bulletGroup.push(line.slice(2));
      continue;
    }
    paragraphGroup.push(line);
  }
  flushBullets();
  flushParagraph();

  return <div className={cn("prose-sm", className)}>{out}</div>;
}
