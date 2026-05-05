"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Building2, LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuthStore } from "@/lib/auth";

export function AppHeader() {
  const router = useRouter();
  const email = useAuthStore((s) => s.email);
  const clearAuth = useAuthStore((s) => s.clearAuth);

  const logout = () => {
    clearAuth();
    router.replace("/login");
  };

  return (
    <header className="border-b bg-card">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
        <Link href="/projects" className="flex items-center gap-2 font-semibold">
          <Building2 className="size-5" />
          <span>ECS Estimating Agent</span>
        </Link>

        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" size="sm" />}>
            {email ?? "Account"}
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>{email}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={logout}>
              <LogOut className="mr-2 size-4" /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
