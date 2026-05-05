"use client";

import { useState } from "react";
import { DocumentUpload } from "@/components/document-upload";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface Props {
  projectId: string;
  disabled?: boolean;
  disabledHint?: string;
}

export function BidUpload({ projectId, disabled = false, disabledHint }: Props) {
  const [vendor, setVendor] = useState("");

  return (
    <div className="space-y-3">
      <div>
        <Label htmlFor="vendor">Vendor / subcontractor name</Label>
        <Input
          id="vendor"
          value={vendor}
          onChange={(e) => setVendor(e.target.value)}
          placeholder="e.g. SRM Concrete, V&M Granite, ParKmium…"
          className="mt-1.5 max-w-md"
          disabled={disabled}
        />
        <p className="mt-1 text-xs text-muted-foreground">
          Files uploaded below will be tagged to this vendor. You can switch vendors
          between uploads — each upload remembers the vendor entered above.
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
            : !vendor.trim()
              ? "Enter a vendor name above to enable upload"
              : undefined
        }
      />
    </div>
  );
}
