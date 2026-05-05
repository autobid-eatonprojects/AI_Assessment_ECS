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
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend (http://localhost:3000)
cd frontend
npm run dev
```

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
