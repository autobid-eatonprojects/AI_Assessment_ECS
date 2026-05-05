"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { DocumentList } from "@/components/document-list";
import { DocumentUpload } from "@/components/document-upload";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

function ProjectDetail({ projectId }: { projectId: string }) {
  const router = useRouter();
  const qc = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
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
    return (
      <div className="text-sm text-muted-foreground">Loading project…</div>
    );
  }

  if (error || !data) {
    return <div className="text-sm text-destructive">Project not found.</div>;
  }

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
              {data.document_count} {data.document_count === 1 ? "document" : "documents"}
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

      <section className="space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Upload documents
        </h2>
        <DocumentUpload projectId={projectId} />
      </section>

      <section className="mt-8 space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Documents
        </h2>
        <DocumentList projectId={projectId} />
      </section>
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
      <main className="mx-auto w-full max-w-4xl flex-1 p-6">
        <ProjectDetail projectId={id} />
      </main>
    </AuthGuard>
  );
}
