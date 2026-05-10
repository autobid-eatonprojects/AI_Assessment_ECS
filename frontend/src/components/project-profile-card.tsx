"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  CheckCircle2,
  Layers,
  MapPin,
  RefreshCw,
  ScrollText,
  Sparkles,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

interface Props {
  projectId: string;
}

function StatLine({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="text-muted-foreground">{label}:</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

export function ProjectProfileCard({ projectId }: Props) {
  const qc = useQueryClient();

  const profileQuery = useQuery({
    queryKey: ["project-profile", projectId],
    queryFn: () => api.getProjectProfile(projectId),
  });

  const run = useMutation({
    mutationFn: () => api.runProjectProfiler(projectId),
    onSuccess: (p) => {
      toast.success(`Profile generated: ${p.building_type ?? "(unknown type)"}`);
      qc.invalidateQueries({ queryKey: ["project-profile", projectId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const profile = profileQuery.data;

  if (profileQuery.isLoading) return null;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <Sparkles className="size-4 text-purple-600" />
            Project profile
          </h3>
          <p className="text-xs text-muted-foreground">
            Building type, size, codes — extracted from cover + general notes (Phase 4.1)
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => run.mutate()}
          disabled={run.isPending}
        >
          <RefreshCw
            className={`size-4 ${run.isPending ? "animate-spin" : ""}`}
          />
          <span className="ml-1.5">
            {profile ? "Re-profile" : "Generate profile"}
          </span>
        </Button>
      </div>

      {!profile ? (
        <p className="rounded-md border border-dashed py-4 text-center text-sm text-muted-foreground">
          No profile yet. Click <strong>Generate profile</strong> to extract
          building type, size, occupancy, codes, etc.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {profile.building_type && (
            <StatLine
              label="Type"
              value={profile.building_type}
              icon={Building2}
            />
          )}
          {profile.size_sf != null && (
            <StatLine
              label="Size"
              value={`${profile.size_sf.toLocaleString()} SF`}
              icon={Layers}
            />
          )}
          {profile.occupancy && (
            <StatLine label="Occupancy" value={profile.occupancy} icon={Layers} />
          )}
          {profile.construction_type && (
            <StatLine
              label="Construction"
              value={profile.construction_type}
              icon={Layers}
            />
          )}
          {profile.sprinklered != null && (
            <StatLine
              label="Sprinklered"
              value={profile.sprinklered ? "Yes" : "No"}
              icon={profile.sprinklered ? CheckCircle2 : XCircle}
            />
          )}
          {profile.stories != null && (
            <StatLine label="Stories" value={String(profile.stories)} icon={Layers} />
          )}
          {profile.location && (
            <StatLine label="Location" value={profile.location} icon={MapPin} />
          )}
          {profile.project_number && (
            <StatLine
              label="Project #"
              value={profile.project_number}
              icon={ScrollText}
            />
          )}
          {profile.codes && profile.codes.length > 0 && (
            <div className="sm:col-span-2 lg:col-span-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <ScrollText className="size-4 shrink-0 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Codes:</span>
                {profile.codes.map((c) => (
                  <span
                    key={c}
                    className="rounded bg-muted px-1.5 py-0.5 text-xs font-mono"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {profile?.reasoning && (
        <p className="mt-3 border-t pt-3 text-xs italic text-muted-foreground">
          {profile.reasoning}
        </p>
      )}
      {profile?.cost_usd != null && (
        <p className="mt-2 text-[10px] text-muted-foreground/70">
          {profile.model} · {profile.latency_ms} ms · $
          {profile.cost_usd.toFixed(4)}
        </p>
      )}
    </div>
  );
}
