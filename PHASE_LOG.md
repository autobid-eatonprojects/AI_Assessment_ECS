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

## Status

| Phase | Status | Quality gate | Notes |
|---|---|---|---|
| 0 — Foundation | ✅ Shipped | ✅ | Skeleton + auth + project/doc CRUD + upload, all persisted |
| 1 — Doc classification + page rendering | Not started | — | — |
| 2 — Vision pre-pass | Not started | — | — |
| 3 — Indexing + search | Not started | — | — |
| 4 — Trade-driven scope extraction | Not started | — | — |
| 5 — Grounding + PDF viewer | Not started | — | — |
| 6 — Quantity takeoff | Not started | — | — |
| 7 — Verification + confidence | Not started | — | — |
| 8 — Bid analysis | Not started | — | — |
| 9 — Evaluation pipeline | Not started | — | — |
| 10 — Polish + SOLUTION.md + video | Not started | — | — |
