"use client";

import { useMutation } from "@tanstack/react-query";
import { Building2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const setAuth = useAuthStore((s) => s.setAuth);
  const token = useAuthStore((s) => s.token);
  const [email, setEmail] = useState("admin@ecs.local");
  const [password, setPassword] = useState("admin");

  useEffect(() => {
    if (token) router.replace("/projects");
  }, [token, router]);

  const login = useMutation({
    mutationFn: () => api.login(email, password),
    onSuccess: (data) => {
      setAuth(data.access_token, email.toLowerCase());
      toast.success("Signed in");
      router.replace("/projects");
    },
    onError: (e: Error) => toast.error(e.message || "Login failed"),
  });

  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader className="space-y-1 text-center">
          <div className="mx-auto flex size-10 items-center justify-center rounded-full bg-primary/10">
            <Building2 className="size-5 text-primary" />
          </div>
          <CardTitle>ECS Estimating Agent</CardTitle>
          <CardDescription>Sign in to continue</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              login.mutate();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </div>
            <Button type="submit" className="w-full" disabled={login.isPending}>
              {login.isPending ? "Signing in…" : "Sign in"}
            </Button>
            <p className="text-center text-xs text-muted-foreground">
              Dev credentials: admin@ecs.local / admin
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
