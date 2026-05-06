"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { api } from "@/lib/api";

const SECTION_LABELS: Record<string, string> = {
  dashboard: "Dashboard",
  documents: "Documents",
  scope: "Scope",
  packages: "Trade Packages",
  review: "Review",
  bids: "Bid Analysis",
  vendors: "Vendors",
  outputs: "Outputs",
  settings: "Settings",
  pages: "Page",
};

function fmtSegment(seg: string): string {
  if (SECTION_LABELS[seg]) return SECTION_LABELS[seg];
  // numeric or uuid — leave as-is
  return seg;
}

export function Breadcrumbs() {
  const pathname = usePathname() || "/";
  const segments = pathname.split("/").filter(Boolean);

  // If we're inside a project, fetch its name to show instead of the UUID
  const projectIdx = segments.indexOf("projects");
  const projectId =
    projectIdx >= 0 && segments.length > projectIdx + 1
      ? segments[projectIdx + 1]
      : null;
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId!),
    enabled: !!projectId && projectId.length > 8,
  });

  if (segments.length === 0 || segments[0] === "login") return null;

  // Build crumb items with hrefs
  const crumbs: { href: string; label: string }[] = [];
  let path = "";
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    path += `/${seg}`;
    if (seg === "projects" && segments.length === 1) {
      crumbs.push({ href: path, label: "Projects" });
    } else if (seg === "projects") {
      crumbs.push({ href: "/projects", label: "Projects" });
    } else if (i === projectIdx + 1 && projectId) {
      crumbs.push({ href: path, label: project?.name ?? seg.slice(0, 8) });
    } else {
      crumbs.push({ href: path, label: fmtSegment(seg) });
    }
  }

  return (
    <nav
      aria-label="Breadcrumb"
      className="flex items-center gap-1.5 text-sm text-muted-foreground"
    >
      {crumbs.map((c, i) => {
        const isLast = i === crumbs.length - 1;
        return (
          <span key={c.href} className="flex items-center gap-1.5">
            {i > 0 && <ChevronRight className="size-3.5 shrink-0 opacity-60" />}
            {isLast ? (
              <span className="font-medium text-foreground">{c.label}</span>
            ) : (
              <Link
                href={c.href}
                className="hover:text-foreground transition-colors"
              >
                {c.label}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}
