# ECS Estimating Agent — Development Setup

## Prerequisites

- **Node.js** 20+ (tested on 24.10)
- **uv** (Python package manager) — install: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- That's it. Python 3.12 is auto-installed by uv.

No Docker, Postgres, Redis, or Qdrant are required for Phase 0 — those will be introduced in later phases when we need them.

## First-time setup

```bash
# Backend
cd backend
cp .env.example .env
uv sync

# Frontend
cd ../frontend
cp .env.local.example .env.local
npm install
```

## Run dev servers

In two terminals:

```bash
# Terminal 1 — backend (http://localhost:8000)
cd backend
uv run uvicorn app.main:app --port 8000

# Terminal 2 — frontend (http://localhost:3000)
cd frontend
npm run dev
```

> **Note:** don't use `--reload` on the backend. Uvicorn's filesystem
> watcher can interfere with the long-running classification + rendering
> tasks and cause them to stall. Restart manually after backend code edits.

Open http://localhost:3000 and sign in with the dev credentials shown on the login page (`admin@ecs.local` / `admin`).

## API documentation

Once the backend is running, OpenAPI docs are at:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Project layout

```
.
├── backend/              FastAPI + SQLAlchemy + SQLite
│   ├── app/
│   │   ├── api/          Route handlers (auth, projects, documents)
│   │   ├── core/         Cross-cutting concerns (security/JWT)
│   │   ├── models/       SQLAlchemy ORM models
│   │   ├── schemas/      Pydantic request/response schemas
│   │   ├── services/     Business logic (file storage)
│   │   ├── config.py     Settings via pydantic-settings
│   │   ├── database.py   Async session factory
│   │   └── main.py       FastAPI app entrypoint
│   ├── data/             SQLite DB + uploaded files (gitignored)
│   └── pyproject.toml
│
├── frontend/             Next.js 16 + React 19 + Tailwind + shadcn/ui
│   ├── src/
│   │   ├── app/          App Router pages (login, projects, [id])
│   │   ├── components/   UI components (auth-guard, header, upload, list)
│   │   └── lib/          API client, auth store, format utils, types
│   └── package.json
│
├── bids/                 Sample bid PDFs (assessment data)
├── trade_list/           CSI MasterFormat (assessment data)
├── *.pdf                 Sample drawings/manual (assessment data)
├── PHASE_LOG.md          Build progress log
└── README-DEV.md         This file
```

## Phase 0 test scenario

After both servers are up:

1. Open http://localhost:3000 — should redirect to `/login`
2. Sign in (defaults pre-filled)
3. Click **New project**, name it "Elks Test", create
4. On the project page, drag-drop these files:
   - `25026 Elks Community Enrichment Center 11.26.25 - TO.pdf` (root of repo)
   - `Elks Community Enrichment Center - Drawings Combined.pdf` (root of repo)
   - A few bid PDFs from `bids/`
   - `trade_list/Trade_List.xlsx`
5. Each file should appear in the list with name + size
6. Refresh the page — files persist
7. Click the download icon on a file → it downloads
8. Click the trash icon on a file → confirm → file is removed
9. Go back to projects list → "Elks Test" shows correct document count

If all 9 steps pass, Phase 0 is good.

## Phase 1 test scenario — classification + page rendering

**Pre-requisite:** put your Anthropic API key in `backend/.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```
Without it, files still upload and pages still render, but they land in the
`needs-api-key` status (no classification).

Restart the backend after editing `.env`.

1. Sign in, create a fresh project ("Elks Phase 1")
2. Drag-drop a mix of files:
   - the 54-page drawings PDF
   - a clear bid (e.g. `ELKS COMMUNITY ENRICHMENT CENTER-EATON-SRM CONCRETE...pdf`)
   - an insurance certificate (e.g. `ACORD_25_Parkmium_LLC.pdf`)
   - a safety manual (e.g. `TLC 12 25 HEALTH SAFETY...pdf`)
   - the business license JPG
3. Each row should immediately show **status = Queued / Classifying** then transition through Rendering → Ready over a few seconds
4. Each row gets a coloured **classification badge** with confidence % (e.g. "Drawings · 98%", "Bid · 99%", "License / Insurance · 95%")
5. Click the drawings doc → opens detail page with a **grid of 54 thumbnails**
6. Click any thumbnail → fullscreen viewer; **arrow keys** navigate, **ESC** closes
7. The detail page shows the classifier's reasoning sentence
8. Click **Re-process** on any doc → status loops back through and lands on Ready again

**Quality gate:** Classifier ≥ 95% accuracy on the supplied `bids/` folder. We're at 100% (8/8) on a representative sample.

## Phase 2 test scenario — vision pre-pass on drawings

After Phase 1 is in place and a drawing-set PDF has been classified, Phase 2 kicks in automatically. For each page of every drawing-set document, the system runs a Claude Sonnet 4.6 vision pass to extract:

- Sheet metadata (sheet number, title, discipline, scale)
- Schedules (footing, door, finish, fixture, equipment, etc.) — full rows verbatim
- General notes (paragraph text, code references)
- Cross-references (e.g. "see S2.1", "detail 5/A5.2")
- Entities (materials, manufacturers, codes, dimensions, room labels, equipment) — each with bbox

Cost is logged per call in the `llm_calls` table.

1. Sign in, create a fresh project ("Elks Phase 2")
2. Upload one of the 54-page drawing PDFs
3. Watch the document detail page — status flows: `Classifying → Rendering pages → Vision pre-pass → Ready`
4. The "Vision pre-pass" panel appears with a per-page mini-grid (54 dots, each linking to that page's extraction view) and a running cost total
5. Once a page lands in **Ready**, click "Inspect extraction →" beneath any thumbnail
6. The page-detail view shows:
   - Full-resolution drawing on the left
   - Tabbed extraction panel on the right (Schedules / Notes / Refs / Entities / Raw)
   - Hover any item → its bounding box highlights on the drawing
7. **Schedules** render as proper HTML tables — footing schedule rows F3.0/F4.0/F6.0 should match the original drawing exactly
8. Click the **Re-extract** button to re-run vision on a single page (failure isolation: one bad page never fails the whole doc)

**Quality gate:** ≥ 90% schedule rows extracted correctly across 5 sample pages. Manually verify against:
- **S1.1 Foundation Plan** — Footing Schedule
- **S0.1 Structural Notes** — Design Loads / Lumber Species / Reinforcing schedules
- **M0.1 HVAC** — Equipment schedules
- **A2.1 Door Schedule**
- **FP0.1 Sprinkler Schedule + Notes**

Cost expectation: ~$5–7 per 54-page drawing set on Sonnet 4.6.

> **First-time setup for Phase 2:** the same `ANTHROPIC_API_KEY` from Phase 1 is reused. Vision concurrency is configurable via `VISION_CONCURRENCY=5` in `backend/.env`.

## What's NOT in Phase 0 (intentionally)

These are added in later phases when needed:

- Postgres, Redis, MinIO, Qdrant — Phase 2/3
- Document classification (LLM call) — Phase 1
- PDF page rendering — Phase 1
- Vision pre-pass on drawings — Phase 2
- Indexing + retrieval — Phase 3
- Trade-driven scope extraction — Phase 4
- Bounding-box source highlighting — Phase 5
- Quantity takeoff — Phase 6
- Verification + confidence — Phase 7
- Bid analysis (inclusions/exclusions) — Phase 8
- RAGAS evaluation — Phase 9
- Multi-tenancy, real auth — only if needed

## Troubleshooting

- **`uv: command not found`** — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Frontend can't reach backend** — confirm `NEXT_PUBLIC_API_URL` in `frontend/.env.local` matches the backend port (default `http://localhost:8000`)
- **CORS error** — confirm backend `CORS_ORIGINS` in `.env` includes `http://localhost:3000`
- **SQLite locked** — close any other process holding `backend/data/app.db`
- **Reset all data** — `rm -rf backend/data` and restart backend
