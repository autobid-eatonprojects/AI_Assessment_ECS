"use client";

import { Sparkles } from "lucide-react";
import { useState } from "react";
import { DocumentUpload } from "@/components/document-upload";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface Props {
  projectId: string;
  disabled?: boolean;
  disabledHint?: string;
}

/**
 * Phase 11 — bulk-drop UX. Vendor name is optional now: the classifier
 * extracts the vendor from each document's letterhead and the canonicalizer
 * groups spelling variants automatically. The operator can still type a
 * vendor name to override (useful when one vendor uploads files in
 * multiple batches under different aliases), but it's no longer required.
 */
export function BidUpload({ projectId, disabled = false, disabledHint }: Props) {
  const [vendor, setVendor] = useState("");

  return (
    <div className="space-y-3">
      <div>
        <Label htmlFor="vendor" className="flex items-center gap-1.5">
          Vendor / subcontractor name
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
            optional
          </span>
        </Label>
        <Input
          id="vendor"
          value={vendor}
          onChange={(e) => setVendor(e.target.value)}
          placeholder="leave blank to auto-detect from each file"
          className="mt-1.5 max-w-md"
          disabled={disabled}
        />
        <p className="mt-1 flex items-start gap-1 text-xs text-muted-foreground">
          <Sparkles className="mt-0.5 size-3 shrink-0 text-violet-500" />
          <span>
            If left blank, each document&apos;s vendor is auto-extracted from
            its letterhead. Spelling variants (TLC vs Tennessee Lawn Care)
            are grouped automatically.
          </span>
        </p>
      </div>
      <DocumentUpload
        projectId={projectId}
        source="bid_submission"
        vendorName={vendor}
        disabled={disabled}
        hint={
          disabled
            ? disabledHint || "Lock scope first to accept bid submissions"
            : undefined
        }
      />
    </div>
  );
}
