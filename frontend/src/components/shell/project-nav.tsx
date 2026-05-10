"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AlertTriangle,
  ClipboardCheck,
  FileText,
  FolderTree,
  Gauge,
  HelpCircle,
  Home,
  Layers,
  ListChecks,
  Receipt,
  ScrollText,
  Target,
  Users,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface ProjectSection {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

function sections(projectId: string): ProjectSection[] {
  return [
    { href: `/projects/${projectId}/dashboard`, label: "Dashboard", icon: Gauge },
    { href: `/projects/${projectId}`, label: "Overview & Uploads", icon: Home },
    { href: `/projects/${projectId}/documents`, label: "Documents", icon: FileText },
    { href: `/projects/${projectId}/scope`, label: "Scope", icon: ListChecks },
    { href: `/projects/${projectId}/ragas`, label: "RAGAS Eval", icon: Target },
    { href: `/projects/${projectId}/packages`, label: "Trade Packages", icon: Layers },
    { href: `/projects/${projectId}/review`, label: "Review", icon: ClipboardCheck },
    { href: `/projects/${projectId}/rfi`, label: "RFI List", icon: HelpCircle },
    { href: `/projects/${projectId}/bids`, label: "Bid Analysis", icon: Receipt },
    { href: `/projects/${projectId}/vendors`, label: "Vendors", icon: Users },
    { href: `/projects/${projectId}/outputs`, label: "Outputs", icon: ScrollText },
  ];
}

export function ProjectNav({ projectId }: { projectId: string }) {
  const pathname = usePathname() || "";

  // Pull review-queue depth so the nav can show counts beside Review
  const { data: conflicts } = useQuery({
    queryKey: ["conflicts", projectId, "open"],
    queryFn: () => api.listConflicts(projectId, "open"),
    refetchInterval: 8_000,
  });
  const { data: gaps } = useQuery({
    queryKey: ["gaps", projectId, "open", ["blocker", "warn"]],
    queryFn: () => api.listGaps(projectId, { status: "open", severity: ["blocker", "warn"] }),
    refetchInterval: 8_000,
  });
  const reviewCount = (conflicts?.length ?? 0) + (gaps?.length ?? 0);

  return (
    <nav className="flex flex-col gap-0.5">
      {sections(projectId).map((s) => {
        // The "Overview" entry is the project root; only mark active when the
        // pathname is exactly the project root (otherwise it would match
        // every nested route under /projects/[id]/...).
        const isProjectRoot = s.href === `/projects/${projectId}`;
        const active = isProjectRoot
          ? pathname === s.href
          : pathname === s.href || pathname.startsWith(s.href + "/");
        const Icon = s.icon;
        const isReview = s.label === "Review";
        return (
          <Link
            key={s.href}
            href={s.href}
            className={cn(
              "flex items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors",
              active
                ? "bg-secondary text-secondary-foreground font-medium"
                : "text-muted-foreground hover:text-foreground hover:bg-secondary/50",
            )}
          >
            <span className="flex items-center gap-2">
              <Icon className="size-4 shrink-0" />
              {s.label}
            </span>
            {isReview && reviewCount > 0 && (
              <span
                className={cn(
                  "rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums",
                  reviewCount > 0
                    ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
                    : "bg-muted text-muted-foreground",
                )}
              >
                {reviewCount}
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

export function GlobalNav() {
  const pathname = usePathname() || "";
  const items = [
    { href: "/projects", label: "Projects", icon: FolderTree },
  ];
  return (
    <nav className="flex flex-col gap-0.5">
      {items.map((s) => {
        const active = pathname === s.href || pathname.startsWith(s.href + "/");
        const Icon = s.icon;
        return (
          <Link
            key={s.href}
            href={s.href}
            className={cn(
              "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors",
              active
                ? "bg-secondary text-secondary-foreground font-medium"
                : "text-muted-foreground hover:text-foreground hover:bg-secondary/50",
            )}
          >
            <Icon className="size-4 shrink-0" />
            {s.label}
          </Link>
        );
      })}
    </nav>
  );
}

export { AlertTriangle };
