# Phase Log

A running record of what has shipped, been tested, and what remains.

## Phase 0 — Foundation ✅ Shipped

**Goal:** Working skeleton with no AI. User can create a project, upload files, and see them persisted.

**Stack chosen for Phase 0:**
- Backend: FastAPI + SQLAlchemy 2.x async + SQLite + Pydantic v2 (managed by `uv`, Python 3.12)
- Storage: local filesystem (`backend/data/uploads/`)
- Auth: dummy JWT (single dev user, hardcoded credentials)
- Frontend: Next.js 16 (App Router) + React 19 + TypeScript + Tailwind v4 + shadcn/ui (Base UI primitives) + TanStack Query + zustand + react-dropzone + sonner

**What was built:**
- Backend endpoints (16 routes total):
  - `POST /api/auth/login` — email+password, returns JWT
  - `GET /api/auth/me` — returns current user
  - `GET / POST / GET / PATCH / DELETE /api/projects[/{id}]` — full project CRUD
  - `GET / POST / GET .../download / DELETE /api/projects/{id}/documents[/{id}]` — full document CRUD with multi-file upload
  - `GET /health`
- Auto-init: SQLite schema created on first boot (`init_db`)
- File deduplication via SHA-256 checksum
- CORS configured for `http://localhost:3000`
- Frontend pages:
  - `/login` — sign-in form with pre-filled dev creds
  - `/projects` — list with cards, document counts, last updated
  - `/projects/new` — create dialog (modal)
  - `/projects/[id]` — detail with drag-drop upload + document list (download/delete)
- Auth guard redirects unauthenticated users to `/login`; persisted via `localStorage` (zustand)
- Toast notifications for all mutations

**Verified end-to-end:**
- `curl` flow: login → create project → upload `Trade_List.xlsx` → list returns it with correct sha256 + size
- File appears in `backend/data/uploads/{project_id}/{document_id}.xlsx`
- TypeScript build passes (`npm run build`)
- All routes return 200 (`/`, `/login`, `/projects`)
- CORS preflight returns proper headers
- Persistence verified across backend restart

**Deferred (added in later phases):**
- Postgres → introduced when SQLite limitations bite
- MinIO/S3 → only if local FS becomes an issue
- Redis + Celery → introduced in Phase 2 (vision pre-pass needs async jobs)
- Qdrant → introduced in Phase 3 (indexing)
- Real auth (Clerk / Auth0) → only if multi-tenancy becomes a requirement

**Quality gate:** ✅ All 9 manual test scenario steps pass (see README-DEV.md).

---

## Phase 1 — Document Classification + Page Rendering ✅ Shipped

**Goal:** Every uploaded document is classified and (where applicable) rendered to PNGs + thumbnails. UI shows classification badge, status, page thumbnail grid, fullscreen viewer.

**Stack additions:**
- `anthropic` SDK (Claude Haiku 4.5 with tool-use structured output)
- `pymupdf` for PDF page rendering at 150 DPI
- `pillow` for thumbnails
- Idempotent SQLite column-add migration in `init_db` (no Alembic yet — added when we move to Postgres)
- Background processing via `asyncio.create_task` (we'll move to ARQ/Celery in Phase 2)

**Taxonomy (project-agnostic):**
`drawing-set`, `written-spec`, `bid-quote`, `scope-letter`, `license-insurance`, `safety-manual`, `contractor-info`, `other`

**Backend additions:**
- `Document` model: `classification_*`, `page_count`, `processing_status`, `processing_error`, `processed_at`
- New `DocumentPage` model with `image_path` + `thumbnail_path`
- New endpoints:
  - `GET /api/projects/{p}/documents/{d}` — single document
  - `POST /api/projects/{p}/documents/{d}/reclassify` — re-trigger pipeline
  - `GET /api/projects/{p}/documents/{d}/pages` — page list
  - `GET /api/projects/{p}/documents/{d}/pages/{n}/image` — full PNG
  - `GET /api/projects/{p}/documents/{d}/pages/{n}/thumbnail` — small PNG
- Services: `classifier.py`, `renderer.py`, `processor.py` (orchestrator)
- Graceful fallback: if no API key, status becomes `needs-api-key` and pages still render

**Frontend additions:**
- `ClassificationBadge` (8 colored variants + confidence %)
- `ProcessingStatusIndicator` (6 states with icons + tooltips)
- `AuthImage` (authenticated blob-URL `<img>`)
- `PageViewerModal` (fullscreen with arrow-key navigation, ESC to close)
- New page `/projects/[id]/documents/[docId]` — thumbnail grid with click → fullscreen
- Polling: docs list + detail refetch every 2s while anything is processing
- "Re-process" button per document

**Verified end-to-end:**
- Classification accuracy on supplied bids folder: **8/8 correct** (most at 95–99% confidence)
  - SRM Concrete quote → `bid-quote` 99%
  - ACORD insurance → `license-insurance` 99%
  - HSE manual → `safety-manual` 98%
  - Roof quote → `bid-quote` 95%
  - Casework proposal → `bid-quote` 98%
  - TLC business license JPG → `license-insurance` 95%
  - COO applicant doc → `contractor-info` 85%
  - Scope Letter (with $273k pricing) → `bid-quote` 95% (Claude correctly read it as a priced proposal)
- 54-page drawings PDF: classified `drawing-set` 98%, all 54 pages rendered (5401×3601 px @ 150 DPI) in ~28s, all thumbnails (320×213) generated
- Auth-protected image endpoints: thumbnail 41 KB, full page 1.2 MB
- Frontend build passes; all routes return 200
- TypeScript clean

**Quality gate:** ≥95% accuracy on supplied bids folder. Hit **100%** ✅

---

## Status

| Phase | Status | Quality gate | Notes |
|---|---|---|---|
| 0 — Foundation | ✅ Shipped | ✅ | Skeleton + auth + project/doc CRUD + upload, all persisted |
| 1 — Doc classification + page rendering | ✅ Shipped | ✅ | 8/8 classification correct, 54-page render in ~28s, fullscreen viewer working |
| 2 — Vision pre-pass | Not started | — | — |
| 3 — Indexing + search | Not started | — | — |
| 4 — Trade-driven scope extraction | Not started | — | — |
| 5 — Grounding + PDF viewer | Not started | — | — |
| 6 — Quantity takeoff | Not started | — | — |
| 7 — Verification + confidence | Not started | — | — |
| 8 — Bid analysis | Not started | — | — |
| 9 — Evaluation pipeline | Not started | — | — |
| 10 — Polish + SOLUTION.md + video | Not started | — | — |
