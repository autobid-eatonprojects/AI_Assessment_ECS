"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, FileText, Receipt, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { BidUpload } from "@/components/bid-upload";
import { DocumentList } from "@/components/document-list";
import { DocumentUpload } from "@/components/document-upload";
import { LifecycleBanner } from "@/components/lifecycle-banner";
import { ProjectProfileCard } from "@/components/project-profile-card";
import { SearchBar } from "@/components/search-bar";
import { TradeRelevanceCard } from "@/components/trade-relevance-card";
import { Button } from "@/components/ui/button";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

function ProjectDetail({ projectId }: { projectId: string }) {
  const router = useRouter();
  const qc = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    refetchInterval: 4_000,
  });

  const del = useMutation({
    mutationFn: () => api.deleteProject(projectId),
    onSuccess: () => {
      toast.success("Project deleted");
      qc.invalidateQueries({ queryKey: ["projects"] });
      router.replace("/projects");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading) {
    return <div className="text-sm text-muted-foreground">Loading project…</div>;
  }

  if (error || !data) {
    return <div className="text-sm text-destructive">Project not found.</div>;
  }

  const lifecycle = data.lifecycle_state;
  const inSetup = lifecycle === "setup";
  const inBids = lifecycle === "open-for-bids";

  return (
    <>
      <div className="mb-6">
        <Link
          href="/projects"
          className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="size-4" /> Back to projects
        </Link>

        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold">{data.name}</h1>
            {data.description && (
              <p className="mt-1 text-sm text-muted-foreground">{data.description}</p>
            )}
            <p className="mt-1 text-xs text-muted-foreground">
              Created {formatRelativeTime(data.created_at)} ·{" "}
              {data.project_document_count} project doc
              {data.project_document_count === 1 ? "" : "s"} ·{" "}
              {data.bid_submission_count} bid
              {data.bid_submission_count === 1 ? "" : "s"}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              if (confirm(`Delete project "${data.name}" and all its documents?`)) {
                del.mutate();
              }
            }}
            disabled={del.isPending}
          >
            <Trash2 className="size-4 text-destructive" />
            <span className="ml-1.5">Delete</span>
          </Button>
        </div>
      </div>

      <div className="mb-6">
        <LifecycleBanner project={data} />
      </div>

      <div className="mb-6">
        <ProjectProfileCard projectId={projectId} />
      </div>

      <div className="mb-6">
        <TradeRelevanceCard projectId={projectId} />
      </div>

      <div className="mb-6">
        <SearchBar projectId={projectId} />
      </div>

      <Tabs defaultValue={inBids ? "bids" : "project-docs"} className="w-full">
        <TabsList>
          <TabsTrigger value="project-docs">
            <FileText className="size-4" />
            <span className="ml-1.5">
              Project documents ({data.project_document_count})
            </span>
          </TabsTrigger>
          <TabsTrigger value="bids" disabled={lifecycle === "setup" && data.bid_submission_count === 0}>
            <Receipt className="size-4" />
            <span className="ml-1.5">
              Bid submissions ({data.bid_submission_count})
            </span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="project-docs" className="space-y-6 pt-4">
          <section className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Upload project documents
            </h2>
            <DocumentUpload
              projectId={projectId}
              source="project_document"
              disabled={!inSetup}
              hint={
                !inSetup
                  ? "Re-open the scope to upload more project documents"
                  : "Drawings, project manual, written specs, trade list…"
              }
            />
          </section>
          <section className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Project documents
            </h2>
            <DocumentList
              projectId={projectId}
              source="project_document"
              emptyHint="No project documents yet. Drag-and-drop drawings or the project manual above."
            />
          </section>
        </TabsContent>

        <TabsContent value="bids" className="space-y-6 pt-4">
          <section className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Upload a vendor bid submission
            </h2>
            <BidUpload
              projectId={projectId}
              disabled={!inBids}
              disabledHint={
                lifecycle === "setup"
                  ? "Lock scope first (status badge above) to accept bid submissions"
                  : "Project is complete — re-open to accept more bids"
              }
            />
          </section>
          <section className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Submissions received
            </h2>
            <DocumentList
              projectId={projectId}
              source="bid_submission"
              emptyHint="No bid submissions yet. Upload one above to attach it to a vendor."
            />
          </section>
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 p-6">
        <ProjectDetail projectId={id} />
      </main>
    </AuthGuard>
  );
}
