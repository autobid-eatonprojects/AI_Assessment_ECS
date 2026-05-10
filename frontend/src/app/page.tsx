"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuthStore } from "@/lib/auth";

export default function Home() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);

  useEffect(() => {
    router.replace(token ? "/projects" : "/login");
  }, [token, router]);

  return (
    <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
      Loading…
    </div>
  );
}
