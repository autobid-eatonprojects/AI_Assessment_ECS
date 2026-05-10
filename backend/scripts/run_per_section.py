"""One-shot: trigger a scope extraction on the live project in any mode.

Runs in a separate process from the live uvicorn so settings is fresh —
SCOPE_ORCHESTRATION_MODE must be set BEFORE importing app.config.

Usage:
  cd backend
  SCOPE_ORCHESTRATION_MODE=hybrid PYTHONPATH=. python scripts/run_per_section.py
  SCOPE_ORCHESTRATION_MODE=per-section PYTHONPATH=. python scripts/run_per_section.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Sanity: env must be set before app.config is imported anywhere.
mode = os.environ.get("SCOPE_ORCHESTRATION_MODE", "").strip()
if mode not in ("per-section", "hybrid", "per-discipline", "per-division"):
    print(
        "abort: SCOPE_ORCHESTRATION_MODE must be one of "
        "'per-section' | 'hybrid' | 'per-discipline' | 'per-division' "
        f"(got {mode!r})",
        file=sys.stderr,
    )
    sys.exit(2)

from app.services.scope_runner import run_scope_extraction  # noqa: E402

PROJECT_ID = "e10f10a2-40e4-46cc-a152-03fa76f84393"


async def main() -> None:
    t0 = time.perf_counter()
    print(f"[run_per_section] starting on project {PROJECT_ID}", flush=True)
    run = await run_scope_extraction(PROJECT_ID)
    elapsed = time.perf_counter() - t0
    print(
        f"[run_per_section] done in {elapsed:.1f}s — run_id={run.id} "
        f"status={run.status}",
        flush=True,
    )
    print(f"NEW_RUN_ID={run.id}")


if __name__ == "__main__":
    asyncio.run(main())
