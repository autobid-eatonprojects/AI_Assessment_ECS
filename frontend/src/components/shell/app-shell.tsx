"use client";

import { usePathname } from "next/navigation";
import { SearchBar } from "@/components/search-bar";
import { Sidebar } from "./sidebar";
import { Breadcrumbs } from "./breadcrumbs";

const SHELL_DISABLED_ROUTES = ["/login"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "";

  // /login (unauthenticated) renders without the shell
  if (SHELL_DISABLED_ROUTES.some((p) => pathname.startsWith(p))) {
    return <>{children}</>;
  }

  // Detect projectId from /projects/<id>/... so the shell-level SearchBar
  // (and its global ⌘K listener) is project-scoped to whichever project
  // the user is in.
  const segs = pathname.split("/").filter(Boolean);
  const projectIdx = segs.indexOf("projects");
  const projectId =
    projectIdx >= 0 && segs.length > projectIdx + 1 ? segs[projectIdx + 1] : null;

  return (
    <div className="flex min-h-screen w-full bg-background">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-12 items-center border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="flex w-full items-center gap-3 px-6">
            <Breadcrumbs />
            {projectId && (
              <div className="ml-auto w-72">
                <SearchBar projectId={projectId} />
              </div>
            )}
          </div>
        </header>
        <div className="flex-1 px-6 py-6">{children}</div>
      </main>
    </div>
  );
}
