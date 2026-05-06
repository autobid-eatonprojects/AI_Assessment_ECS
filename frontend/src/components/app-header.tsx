"use client";

// Stage 8 — replaced by the persistent sidebar in AppShell.
// Kept as a no-op export so existing pages that import + render
// <AppHeader /> don't break; the new shell renders the chrome instead.
//
// Pages can drop the AppHeader import any time; this is just transition
// scaffolding.

export function AppHeader() {
  return null;
}
