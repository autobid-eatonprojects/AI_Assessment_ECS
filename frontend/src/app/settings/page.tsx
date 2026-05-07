"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, Loader2, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { AuthGuard } from "@/components/auth-guard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { api } from "@/lib/api";
import type { AppSettings } from "@/lib/types";

const CLAUDE_MODELS = [
  "claude-haiku-4-5",
  "claude-sonnet-4-6",
  "claude-opus-4-7",
];
const VISION_PROVIDERS = ["anthropic", "google"];
const VOYAGE_EMBED_MODELS = [
  "voyage-3-large",
  "voyage-3",
  "voyage-3-lite",
];
const COHERE_RERANK_MODELS = ["rerank-v3.5", "rerank-english-v3.0"];

function ProviderRow({
  name,
  configured,
  onTest,
  testing,
  testResult,
}: {
  name: string;
  configured: boolean;
  onTest: () => void;
  testing: boolean;
  testResult?: { ok: boolean; detail: string | null } | null;
}) {
  return (
    <div className="flex items-center justify-between rounded-md border p-3">
      <div className="flex items-center gap-2">
        <div
          className={
            configured
              ? "size-2 rounded-full bg-emerald-500"
              : "size-2 rounded-full bg-rose-500"
          }
        />
        <span className="text-sm font-medium capitalize">{name}</span>
        <span className="text-xs text-muted-foreground">
          {configured ? "key configured" : "no key set in .env"}
        </span>
      </div>
      <div className="flex items-center gap-2">
        {testResult && (
          <span
            className={
              testResult.ok
                ? "inline-flex items-center text-xs text-emerald-600 dark:text-emerald-400"
                : "inline-flex items-center text-xs text-rose-600 dark:text-rose-400"
            }
          >
            {testResult.ok ? <Check className="mr-1 size-3" /> : <X className="mr-1 size-3" />}
            {testResult.detail ?? (testResult.ok ? "ok" : "failed")}
          </span>
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={onTest}
          disabled={!configured || testing}
        >
          {testing ? <Loader2 className="size-3.5 animate-spin" /> : "Test"}
        </Button>
      </div>
    </div>
  );
}

function ModelDropdown({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            className="w-64 justify-between font-normal"
          >
            {value} <ChevronDown className="ml-2 size-3.5 opacity-50" />
          </Button>
        }
      />
      <DropdownMenuContent className="w-64">
        {options.map((opt) => (
          <DropdownMenuItem key={opt} onClick={() => onChange(opt)}>
            {opt}
            {opt === value && <Check className="ml-auto size-3.5" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function SettingsView() {
  const qc = useQueryClient();
  const { data: settings, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.getSettings(),
  });
  const { data: status } = useQuery({
    queryKey: ["system-status"],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 12_000,
  });

  const patch = useMutation({
    mutationFn: (body: Partial<AppSettings>) => api.patchSettings(body),
    onSuccess: () => {
      toast.success("Setting updated");
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const [healthState, setHealthState] = useState<
    Record<string, { ok: boolean; detail: string | null } | null>
  >({});
  const [testing, setTesting] = useState<Record<string, boolean>>({});

  const testProvider = async (provider: string) => {
    setTesting((s) => ({ ...s, [provider]: true }));
    try {
      const result = await api.providerHealth(provider);
      setHealthState((s) => ({
        ...s,
        [provider]: { ok: result.ok, detail: result.detail },
      }));
    } catch (e) {
      setHealthState((s) => ({
        ...s,
        [provider]: { ok: false, detail: (e as Error).message },
      }));
    } finally {
      setTesting((s) => ({ ...s, [provider]: false }));
    }
  };

  if (isLoading || !settings) {
    return (
      <div className="text-sm text-muted-foreground">Loading settings…</div>
    );
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Hot-reloadable configuration. API keys live in{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-xs">
            backend/.env
          </code>
          ; everything else can be changed here.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>API providers</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {Object.entries(settings.provider_keys_configured).map(
              ([name, configured]) => (
                <ProviderRow
                  key={name}
                  name={name}
                  configured={configured}
                  onTest={() => testProvider(name)}
                  testing={!!testing[name]}
                  testResult={healthState[name]}
                />
              ),
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>System status</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <Row k="Backend version" v={status?.backend_version ?? "—"} />
            <Row k="Database" v={status?.db_path ?? "—"} mono />
            <Row k="Projects" v={status?.project_count ?? 0} />
            <Row k="Documents" v={status?.document_count ?? 0} />
            <Row k="Scope runs" v={status?.scope_run_count ?? 0} />
            <Row k="LLM calls" v={status?.llm_call_count ?? 0} />
            <Row
              k="Total cost"
              v={`$${(status?.total_cost_usd ?? 0).toFixed(2)}`}
            />
            <Row k="Audit log entries" v={status?.audit_log_count ?? 0} />
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Model overrides</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <SettingRow label="Classifier (Haiku)">
              <ModelDropdown
                value={settings.classifier_model}
                options={CLAUDE_MODELS}
                onChange={(v) => patch.mutate({ classifier_model: v })}
              />
            </SettingRow>
            <SettingRow label="Vision (Sonnet)">
              <ModelDropdown
                value={settings.vision_model}
                options={CLAUDE_MODELS}
                onChange={(v) => patch.mutate({ vision_model: v })}
              />
            </SettingRow>
            <SettingRow label="Vision provider">
              <ModelDropdown
                value={settings.vision_provider}
                options={VISION_PROVIDERS}
                onChange={(v) => patch.mutate({ vision_provider: v })}
              />
            </SettingRow>
            <SettingRow label="Contextualizer (Haiku)">
              <ModelDropdown
                value={settings.contextualizer_model}
                options={CLAUDE_MODELS}
                onChange={(v) => patch.mutate({ contextualizer_model: v })}
              />
            </SettingRow>
            <SettingRow label="Embedding model (Voyage)">
              <ModelDropdown
                value={settings.embedding_model}
                options={VOYAGE_EMBED_MODELS}
                onChange={(v) => patch.mutate({ embedding_model: v })}
              />
            </SettingRow>
            <SettingRow label="Rerank model (Cohere)">
              <ModelDropdown
                value={settings.rerank_model}
                options={COHERE_RERANK_MODELS}
                onChange={(v) => patch.mutate({ rerank_model: v })}
              />
            </SettingRow>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Concurrency limits</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <NumericSetting
              label="Vision concurrency"
              value={settings.vision_concurrency}
              onSave={(v) => patch.mutate({ vision_concurrency: v })}
              min={1}
              max={20}
            />
            <NumericSetting
              label="Index concurrency"
              value={settings.index_concurrency}
              onSave={(v) => patch.mutate({ index_concurrency: v })}
              min={1}
              max={32}
            />
            <NumericSetting
              label="Page render DPI"
              value={settings.page_dpi}
              onSave={(v) => patch.mutate({ page_dpi: v })}
              min={75}
              max={400}
            />
            <NumericSetting
              label="Thumbnail max dimension"
              value={settings.thumbnail_max_dim}
              onSave={(v) => patch.mutate({ thumbnail_max_dim: v })}
              min={120}
              max={1024}
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Appearance</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">Theme</div>
                <div className="text-xs text-muted-foreground">
                  Choose light, dark, or follow system.
                </div>
              </div>
              <ThemeToggle />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Active overrides</CardTitle>
          </CardHeader>
          <CardContent>
            {settings.overrides_in_use.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No DB-stored overrides — all settings come from{" "}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">.env</code>
                .
              </p>
            ) : (
              <ul className="space-y-1 text-sm">
                {settings.overrides_in_use.map((k) => (
                  <li key={k} className="flex items-center gap-2">
                    <Check className="size-3.5 text-emerald-500" />
                    <code className="rounded bg-muted px-1 py-0.5 text-xs">
                      {k}
                    </code>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Row({
  k,
  v,
  mono = false,
}: {
  k: string;
  v: string | number;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center justify-between border-b pb-1.5 last:border-b-0 last:pb-0">
      <span className="text-muted-foreground">{k}</span>
      <span className={mono ? "font-mono text-xs" : "tabular-nums"}>{v}</span>
    </div>
  );
}

function SettingRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b pb-2 last:border-b-0 last:pb-0">
      <Label className="text-sm font-medium">{label}</Label>
      {children}
    </div>
  );
}

function NumericSetting({
  label,
  value,
  onSave,
  min,
  max,
}: {
  label: string;
  value: number;
  onSave: (v: number) => void;
  min: number;
  max: number;
}) {
  const [draft, setDraft] = useState(String(value));
  const dirty = String(value) !== draft;
  return (
    <div className="flex items-center justify-between border-b pb-2 last:border-b-0 last:pb-0">
      <Label className="text-sm font-medium">{label}</Label>
      <div className="flex items-center gap-1">
        <Input
          type="number"
          className="w-20"
          min={min}
          max={max}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        {dirty && (
          <Button
            size="sm"
            onClick={() => {
              const num = Number(draft);
              if (!Number.isFinite(num)) return;
              onSave(Math.max(min, Math.min(max, num)));
            }}
          >
            Save
          </Button>
        )}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AuthGuard>
      <SettingsView />
    </AuthGuard>
  );
}
