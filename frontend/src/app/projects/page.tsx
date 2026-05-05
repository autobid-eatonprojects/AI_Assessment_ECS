"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import Link from "next/link";
import { AppHeader } from "@/components/app-header";
import { AuthGuard } from "@/components/auth-guard";
import { NewProjectDialog } from "@/components/new-project-dialog";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";

function ProjectsList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading projects…</p>;
  }

  if (error) {
    return <p className="text-sm text-destructive">Failed to load projects.</p>;
  }

  if (!data || data.length === 0) {
    return (
      <Card className="border-dashed">
        <CardContent className="py-12 text-center">
          <p className="text-sm text-muted-foreground">
            No projects yet. Create your first project to get started.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {data.map((p) => (
        <Link key={p.id} href={`/projects/${p.id}`} className="block">
          <Card className="h-full transition-shadow hover:shadow-md">
            <CardHeader>
              <CardTitle className="truncate">{p.name}</CardTitle>
              {p.description && (
                <CardDescription className="line-clamp-2">{p.description}</CardDescription>
              )}
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <FileText className="size-3.5" />
                  {p.document_count} {p.document_count === 1 ? "document" : "documents"}
                </span>
                <span>{formatRelativeTime(p.updated_at)}</span>
              </div>
            </CardContent>
          </Card>
        </Link>
      ))}
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <AuthGuard>
      <AppHeader />
      <main className="mx-auto w-full max-w-6xl flex-1 p-6">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">Projects</h1>
            <p className="text-sm text-muted-foreground">
              Each project holds the manuals, drawings, bids, and trade list for one
              estimating job.
            </p>
          </div>
          <NewProjectDialog />
        </div>
        <ProjectsList />
      </main>
    </AuthGuard>
  );
}
