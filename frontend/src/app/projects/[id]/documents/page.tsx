"use client";

import Link from "next/link";
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText, Receipt } from "lucide-react";
import { AuthGuard } from "@/components/auth-guard";
import { DocumentList } from "@/components/document-list";
import { Card, CardContent } from "@/components/ui/card";
import { buttonVariants } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";

function DocumentsView({ projectId }: { projectId: string }) {
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
  });

  const projectDocCount = project?.project_document_count ?? 0;
  const bidCount = project?.bid_submission_count ?? 0;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Documents</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Browse every project document and bid submission attached to this
            project. To upload more, use the Overview & Uploads page.
          </p>
        </div>
        <Link
          href={`/projects/${projectId}`}
          className={buttonVariants({ variant: "outline", size: "sm" })}
        >
          Open Overview & Uploads
        </Link>
      </header>

      {!project ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : projectDocCount + bidCount === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-10 text-center">
            <FileText className="mx-auto mb-2 size-8 text-muted-foreground" />
            <p className="text-sm font-medium">No documents yet</p>
            <p className="text-xs text-muted-foreground">
              Head to{" "}
              <Link
                href={`/projects/${projectId}`}
                className="underline underline-offset-2"
              >
                Overview & Uploads
              </Link>{" "}
              to drop in drawings, the project manual, the trade list, and bid
              submissions.
            </p>
          </CardContent>
        </Card>
      ) : (
        <Tabs defaultValue="project-docs">
          <TabsList>
            <TabsTrigger value="project-docs" className="flex items-center gap-2">
              <FileText className="size-3.5" />
              Project documents
              <span className="ml-1 rounded-full bg-secondary px-1.5 py-0.5 text-[10px] font-semibold tabular-nums">
                {projectDocCount}
              </span>
            </TabsTrigger>
            <TabsTrigger value="bids" className="flex items-center gap-2">
              <Receipt className="size-3.5" />
              Bid submissions
              <span className="ml-1 rounded-full bg-secondary px-1.5 py-0.5 text-[10px] font-semibold tabular-nums">
                {bidCount}
              </span>
            </TabsTrigger>
          </TabsList>

          <TabsContent value="project-docs" className="mt-4">
            <DocumentList
              projectId={projectId}
              source="project_document"
              emptyHint="No project documents yet — upload drawings, manual, and trade list from Overview & Uploads."
            />
          </TabsContent>

          <TabsContent value="bids" className="mt-4">
            <DocumentList
              projectId={projectId}
              source="bid_submission"
              emptyHint="No bid submissions yet — accept bids on the Overview & Uploads page once the project is open for bids."
            />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

export default function DocumentsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <AuthGuard>
      <DocumentsView projectId={id} />
    </AuthGuard>
  );
}
