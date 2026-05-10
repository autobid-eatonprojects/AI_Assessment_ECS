"use client";

import { useMutation } from "@tanstack/react-query";
import { Loader2, Search, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { SearchHit } from "@/lib/types";

const CHUNK_TYPE_LABEL: Record<string, string> = {
  schedule: "Schedule",
  note: "Note",
  cross_reference: "Cross-ref",
  entity: "Entity",
  page_summary: "Page",
  page_text: "Page text",
};

interface Props {
  projectId: string;
}

export function SearchBar({ projectId }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [reranker, setReranker] = useState<string>("");
  const inputRef = useRef<HTMLInputElement>(null);

  const search = useMutation({
    mutationFn: (q: string) => api.search(projectId, q, 10),
    onSuccess: (res) => {
      setHits(res.hits);
      setReranker(res.rerank_used);
    },
    onError: (e: Error) => {
      toast.error(e.message || "Search failed");
      setHits([]);
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(true);
        setTimeout(() => inputRef.current?.focus(), 50);
      } else if (e.key === "Escape" && open) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setHits(null);
    search.mutate(q);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          setTimeout(() => inputRef.current?.focus(), 50);
        }}
        className="flex w-full max-w-md items-center gap-2 rounded-md border bg-background px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
      >
        <Search className="size-4" />
        <span>Search the project…</span>
        <kbd className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono">
          ⌘K
        </kbd>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-24"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-modal="true"
        >
          <div
            className="w-full max-w-2xl rounded-lg border bg-card shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <form onSubmit={submit} className="flex items-center gap-2 border-b px-3 py-2">
              <Search className="size-4 text-muted-foreground" />
              <Input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder='Search for materials, sheet numbers, codes (e.g. "NFPA 13"), or rooms…'
                className="flex-1 border-0 px-0 shadow-none focus-visible:ring-0"
              />
              {search.isPending && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded p-1 text-muted-foreground hover:bg-muted"
                aria-label="Close"
              >
                <X className="size-4" />
              </button>
            </form>

            <div className="max-h-[60vh] overflow-y-auto p-2">
              {hits === null && !search.isPending && (
                <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                  Search across schedules, notes, cross-references, and entities
                  in this project.
                </p>
              )}

              {hits && hits.length === 0 && (
                <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                  No results. Try different keywords.
                </p>
              )}

              {hits && hits.length > 0 && (
                <>
                  <p className="px-2 pb-2 text-xs text-muted-foreground">
                    {hits.length} results · reranker:{" "}
                    <span className="font-mono">{reranker}</span>
                  </p>
                  <ul className="divide-y">
                    {hits.map((h) => (
                      <li key={h.chunk_id}>
                        <Link
                          href={
                            h.page_number
                              ? `/projects/${projectId}/documents/${h.document_id}/pages/${h.page_number}`
                              : `/projects/${projectId}/documents/${h.document_id}`
                          }
                          onClick={() => setOpen(false)}
                          className="block rounded-md p-3 hover:bg-muted/60"
                        >
                          <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                            <span className="rounded bg-muted px-1.5 py-0.5 font-medium">
                              {CHUNK_TYPE_LABEL[h.chunk_type] ?? h.chunk_type}
                            </span>
                            {h.sheet_number && (
                              <span className="font-mono font-medium text-foreground">
                                {h.sheet_number}
                              </span>
                            )}
                            {h.sheet_title && <span>· {h.sheet_title}</span>}
                            {h.discipline && (
                              <span className="rounded bg-zinc-100 px-1 text-[10px] uppercase">
                                {h.discipline}
                              </span>
                            )}
                            <span className="ml-auto font-mono">
                              {h.rerank_score != null
                                ? h.rerank_score.toFixed(3)
                                : h.rrf_score.toFixed(3)}
                            </span>
                          </div>
                          <p className="line-clamp-3 text-sm">
                            {h.snippet ?? h.text}
                          </p>
                          <p className="mt-1 truncate text-xs text-muted-foreground">
                            {h.document_filename}
                            {h.page_number ? ` · page ${h.page_number}` : ""}
                          </p>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
