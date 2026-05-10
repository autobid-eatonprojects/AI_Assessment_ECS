"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, Target } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { RagasPanel } from "@/components/ragas-panel";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

interface PageProps {
  params: Promise<{ id: string }>;
}

function RagasPage({ projectId }: { projectId: string }) {
  const overviewQuery = useQuery({
    queryKey: ["scope-overview", projectId],
    queryFn: () => api.getScopeOverview(projectId),
  });

  const overview = overviewQuery.data;
  const run = overview?.latest_run ?? null;

  return (
    <>
      <Link
        href={`/projects/${projectId}/scope`}
        className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="size-4" /> Back to scope
      </Link>

      <div className="mb-6">
        <h1 className="flex items-center gap-2 text-2xl font-semibold">
          <Target className="size-6 text-muted-foreground" />
          RAGAS Evaluation
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Benchmark the latest scope-extraction run against curated
          ground-truth fixtures using the four canonical RAGAS metrics:
          context precision, context recall, faithfulness, and answer
          relevancy.
        </p>
      </div>

      {!run && overviewQuery.isLoading && (
        <div className="rounded-md border border-dashed py-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      )}

      {!run && !overviewQuery.isLoading && (
        <div className="rounded-md border border-dashed py-12 text-center text-sm text-muted-foreground">
          No scope-extraction run found for this project yet. Generate
          scope from the{" "}
          <Link
            href={`/projects/${projectId}/scope`}
            className="text-foreground underline hover:no-underline"
          >
            Scope page
          </Link>{" "}
          first.
        </div>
      )}

      {run && run.status !== "complete" && (
        <div className="rounded-md border border-dashed py-12 text-center text-sm text-muted-foreground">
          The latest scope run is{" "}
          <span className="font-medium text-foreground">{run.status}</span>.
          RAGAS evaluation is only available once the run completes.
        </div>
      )}

      {run && run.status === "complete" && (
        <>
          <div className="mb-4 rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
            Scope run{" "}
            <span className="font-mono text-foreground">{run.id.slice(0, 8)}</span>{" "}
            · {overview?.total_items ?? 0} items across{" "}
            {overview?.by_division.length ?? 0} divisions ·{" "}
            {formatRelativeTime(run.completed_at ?? run.started_at)}
          </div>
          <RagasPanel
            projectId={projectId}
            scopeRunId={run.id}
            scopeRunStatus={run.status}
          />
        </>
      )}
    </>
  );
}

export default function Page({ params }: PageProps) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl flex-1 p-6">
        <RagasPage projectId={id} />
      </main>
    </AuthGuard>
  );
}
