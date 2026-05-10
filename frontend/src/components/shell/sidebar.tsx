"use client";

import Link from "next/link";
import { useRouter, useParams, usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Building2,
  Check,
  ChevronsUpDown,
  HelpCircle,
  LogOut,
  Settings,
  User as UserIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuthStore } from "@/lib/auth";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "./theme-toggle";
import { GlobalNav, ProjectNav } from "./project-nav";

export function Sidebar() {
  const router = useRouter();
  const pathname = usePathname() || "";
  const email = useAuthStore((s) => s.email);
  const clearAuth = useAuthStore((s) => s.clearAuth);

  // Detect active project id from URL (/projects/<id>/...)
  const segs = pathname.split("/").filter(Boolean);
  const projectIdx = segs.indexOf("projects");
  const projectId =
    projectIdx >= 0 && segs.length > projectIdx + 1 ? segs[projectIdx + 1] : null;

  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(),
    staleTime: 60_000,
  });
  const activeProject = projects?.find((p) => p.id === projectId) ?? null;

  const logout = () => {
    clearAuth();
    router.replace("/login");
  };

  return (
    <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r bg-card lg:flex">
      <div className="flex h-14 items-center px-3.5">
        <Link href="/projects" className="flex items-center gap-2 text-sm font-semibold">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Building2 className="size-4" />
          </div>
          ECS Estimator
        </Link>
      </div>

      {/* Project switcher (only on project routes) */}
      {projectId && (
        <div className="px-3 pb-3">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="outline"
                  className="w-full justify-between font-normal"
                >
                  <span className="flex items-center gap-2 truncate">
                    <Building2 className="size-3.5 shrink-0 opacity-70" />
                    <span className="truncate text-sm">
                      {activeProject?.name ?? "Loading…"}
                    </span>
                  </span>
                  <ChevronsUpDown className="size-3.5 opacity-50" />
                </Button>
              }
            />
            <DropdownMenuContent className="w-56" align="start">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Switch project</DropdownMenuLabel>
                {(projects ?? []).map((p) => (
                  <DropdownMenuItem
                    key={p.id}
                    onClick={() => router.push(`/projects/${p.id}/dashboard`)}
                  >
                    <span className="truncate">{p.name}</span>
                    {p.id === projectId && (
                      <Check className="ml-auto size-3.5" />
                    )}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => router.push("/projects")}>
                All projects…
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-2 pb-4">
        {projectId ? (
          <>
            <ProjectNav projectId={projectId} />
            <div className="mt-6 border-t pt-4">
              <p className="mb-1 px-2.5 text-[11px] uppercase tracking-wider text-muted-foreground">
                Workspace
              </p>
              <GlobalNav />
            </div>
          </>
        ) : (
          <GlobalNav />
        )}
      </div>

      <div className="border-t p-2">
        <div className="flex items-center gap-1">
          <Link
            href="/settings"
            className={cn(
              "flex flex-1 items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors",
              pathname.startsWith("/settings")
                ? "bg-secondary text-secondary-foreground font-medium"
                : "text-muted-foreground hover:text-foreground hover:bg-secondary/50",
            )}
          >
            <Settings className="size-4" /> Settings
          </Link>
          <ThemeToggle compact />
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                className="mt-1 w-full justify-start px-2.5 font-normal"
              >
                <UserIcon className="mr-2 size-4 shrink-0" />
                <span className="truncate text-sm">{email ?? "Account"}</span>
              </Button>
            }
          />
          <DropdownMenuContent align="start" className="w-52">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{email}</DropdownMenuLabel>
              <DropdownMenuItem onClick={() => router.push("/settings")}>
                <Settings className="mr-2 size-4" /> Settings
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => window.open("/health", "_blank")}
              >
                <HelpCircle className="mr-2 size-4" /> Backend health
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={logout}>
              <LogOut className="mr-2 size-4" /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );
}
