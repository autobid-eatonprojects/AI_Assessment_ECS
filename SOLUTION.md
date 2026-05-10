# Solution — AI Estimating Agent

A production-grade construction estimating agent that ingests project
manuals, drawings, and subcontractor bids, then produces an industry-
standard Scope of Work, a per-trade bid coverage matrix, and a RAGAS
evaluation of its own performance — all surfaced through a Next.js UI
backed by FastAPI.

---

## TL;DR

- **Pipeline.** Upload PDF → Mistral/Gemini OCR (spec) or Sonnet vision pre-pass (drawings) → parallel post-vision enrichment (sheet index, schedule grids, revision blocks, symbol legend) → text chunking + Voyage embeddings + BM25 → multi-mode scope extraction (per-discipline / per-section / hybrid / per-division) → admission filter (citation validation + link judge + bilateral evidence) → conflict resolution (Opus arbitration with two-phase commit) → trade packaging → 10-section SOW renderer + per-bid coverage matrix.
- **Scope quality is benchmarked** by a native RAGAS implementation (no `ragas` package dependency) running over 23 estimator-curated ground-truth fixtures, with per-fixture and macro-average context precision, context recall, answer faithfulness, and answer relevancy. Results land in DB and surface on a dedicated `/ragas` page.
- **Stack.** FastAPI + Next.js 16 (Turbopack) + PostgreSQL + pgvector + SQLAlchemy 2.0 async + Alembic. Anthropic Sonnet 4.6 / Haiku 4.5 / Opus 4.7 + Voyage-3-large embeddings + Cohere Rerank v3.5 + Mistral OCR + Gemini 2.5 Flash. **No LangChain, no LlamaIndex** — direct SDKs with custom orchestration so cost, latency, and traces are observable.

---

## 1. Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Upload (PDF) → classifier (Haiku) → routed by doc_type                  │
└─────────────────────────────────────────────────────────────────────────┘
        │
        ├─── written-spec / bid-quote ──────────────────────────────────┐
        │                                                                │
        │   render → OCR (Mistral + Gemini ensemble, longer-wins)       │
        │   → spec_toc reconstruction → CSI subset                      │
        │                                                                │
        └─── drawing-set ────────────────────────────────────────────┐  │
                                                                      │  │
            render → vision pre-pass (Sonnet per page)               │  │
                ↓                                                     │  │
            ┌─── parallel asyncio.gather (EnrichmentPass registry) ──┤  │
            │   sheet_index   schedule (router→typed)   revision    │  │
            │   symbol_legend                                         │  │
            └────────────────────────────────────────────────────────┘  │
                                                                         │
        chunk + Voyage embed (1024d) + BM25 ───────────────────────────┘
                ↓
        ┌──────────────────────────────────────────────────────────┐
        │  Scope orchestrator (registry: per-discipline / per-     │
        │  section / hybrid / per-division)                         │
        │     ↓                                                     │
        │  retrieve (pgvector + BM25 + Cohere rerank)              │
        │     ↓                                                     │
        │  extract candidates (Sonnet tool-use)                    │
        │     ↓                                                     │
        │  drawing_grounder (vision verify on per-sheet quadrants) │
        │     ↓                                                     │
        │  admission split (pure):                                  │
        │     citation_validator (L2 token overlap)                │
        │     link_judge (Haiku per-citation entailment)           │
        │     bilateral_evidence (per-pattern coverage)            │
        │     ↓                                                     │
        │  conflict_resolution (cluster → arbitrate via Opus,       │
        │     two-phase commit; deferred items hit HITL queue)     │
        │     ↓                                                     │
        │  trade packaging + trust score                           │
        └──────────────────────────────────────────────────────────┘
                ↓
        Surfaces: SOW renderer (10-section MD), bid_coverage matrix,
        review queue (conflicts + gaps), RAGAS panel.
```

The orchestration is strict about its seams. Every phase is a module with a
declared interface; phases that are independent run concurrently
(`asyncio.gather`); phases that are sequential are sequential because they
genuinely consume each other's output. The full pipeline is restartable —
every persisted row records its provenance so a re-run produces the same
output up to model nondeterminism.

### Key seams

| Seam | What it isolates |
|---|---|
| `classifier` | Doc type routing — drawing-set vs written-spec vs bid-quote |
| `vision_extractor` | Per-page Sonnet vision (with Anthropic / Gemini adapter pattern) |
| `enrichment_passes.EnrichmentPass` | Four parallel post-vision passes — single ABC, registry-driven gather |
| `anthropic_tool_call.call_with_tool` | Five Sonnet/Haiku/Opus tool-use sites share one helper |
| `Orchestrator` (in `scope_runner`) | Four extraction modes implement the same ABC; processor picks one by config |
| `Admission` (pure dataclass) | Citation validation + link judge + bilateral check happen pre-DB so persistence is all-or-nothing |
| `EnrichmentPass`, `Orchestrator`, `VisionAdapter` | All ABCs — interface IS the test surface |

---

## 2. Stack and rationale

### LLM models

| Role | Model | Why this model for this role |
|---|---|---|
| Classifier | Haiku 4.5 | Cheap, fast, deterministic enough for doc-type routing |
| Vision pre-pass / sheet_index / schedule_typed / symbol_legend / drawing_grounder | Sonnet 4.6 | Best-in-class drawing comprehension; structured tool-use; reliable JSON outputs |
| Section extractor / scope extractor | Sonnet 4.6 | Long context windows; tool-use schema is enforced |
| Link judge | Haiku 4.5 | Per-citation entailment is a verification task — Haiku tier matched |
| Bid extractor | Sonnet 4.6 | Bids are dense, mixed-format prose; needs the strong reasoning |
| Conflict arbitration | Opus 4.7 | Final-say role; infrequent calls; precision matters more than cost |
| RAGAS judge / reverse-question generator | Haiku 4.5 | Per-question Boolean/short-answer; cost-tier matched |

The pattern: **cost-tier matched to role**. Cheap models do verification and routing; mid-tier does extraction and vision; top-tier does final arbitration only. Every call goes through one helper (`anthropic_tool_call.call_with_tool`) so cache_control and cost logging are uniform.

### Embeddings + retrieval

- **Voyage-3-large (1024d)** as the dense vector — outperforms OpenAI / Cohere on Voyage's published AEC-relevant benchmarks.
- **PostgreSQL + pgvector** as the single store — embeddings live on the chunk row, no separate vector DB to keep in sync. One transaction writes chunk + vector together.
- **BM25** in addition to pgvector — sparse retrieval catches CSI codes and exact symbol matches that dense retrieval misses (e.g. "08 71 00").
- **Cohere Rerank v3.5** as the second-stage re-ranker — narrows ~50 hybrid candidates to the top 8–12 before they hit the extractor.

### OCR

- **Multi-provider ensemble: Mistral OCR + Gemini 2.5 Flash, run in parallel, longest output wins.** Mistral wins ~71% of pages on the Elks spec book; Gemini wins the remainder.
- The ensemble is the **W1 mitigation** in the original design — single-OCR misreads (`shall not` → `shall`) are surface-able by provider disagreement. Pages with high Levenshtein distance between the two outputs are flagged as low-confidence for the HITL review queue.
- Retry-on-empty + scan-preprocessing fallback recovers ~95% of pages where the first OCR pass returned nothing (was a real bug — 117/370 spec pages were blank in the original processing run; re-OCR with the current code recovered 111 of them, adding 218k chars of text).

### Frameworks

- **FastAPI** for the backend — async-native, OpenAPI auto-generation, fast.
- **Next.js 16 (Turbopack) + React 19 + Tailwind + shadcn/ui** for the frontend — App Router, server-rendered shells, client-rendered interactive panels.
- **SQLAlchemy 2.0 async** + **Alembic** for migrations — typed models, async session pattern matches FastAPI.
- **No LangChain, no LlamaIndex.** These frameworks abstract the hot path — prompts, retries, cost logging, prompt caching — into opaque chains. We need every API call to be traceable, every cost line itemized, every prompt cacheable on the cache_control boundary that's most useful, and every retry to apply different backoff per provider. Direct SDKs (anthropic, voyageai, cohere, google-genai) plus a thin `anthropic_tool_call` helper give us those properties; a framework would have hidden them.
- **Native RAGAS** instead of the `ragas` package — the assessment specifically asks for RAGAS-based evaluation, but the package is opinionated about which judge model, which embedding, and which prompts to use. We get more honest numbers + offline reproducibility by implementing the four metrics directly against our own retrieval/generation code.

---

## 3. How the five evaluation criteria are addressed

### Accuracy — "How precisely does the agent identify scope within dense manuals?"

| Module | What it does |
|---|---|
| `vision_extractor` | Per-page Sonnet vision over rendered drawings → structured PageExtraction rows |
| `section_extractor` | Per-CSI-section spec extraction with admin/Part-1 filtering |
| `drawing_grounder` | Tiled-quadrant Sonnet pass that confirms items are actually depicted on cited sheets |
| `citation_validator` | L2-style deterministic token-overlap gate before any item is persisted |
| `link_judge` | Per-citation Haiku entailment check — flags citations the model can't actually defend |
| `bilateral_evidence` | Items that should have both spec + drawing evidence are scored against that pattern (admin / demo / material patterns each have their own expected shape) |
| `csi_grounder` | Strict CSI MasterFormat code grounding from the project's Trade_List.xlsx — no hallucinated codes |
| `scope_runner` orchestrator | Four extraction modes (per-discipline / per-section / hybrid / per-division) so the right granularity is used per project shape |

The pipeline pre-filters chunks that look like Part-1 submittal boilerplate or TOC pages — those leak into citations and depress RAGAS context_precision, so they're caught at extraction time rather than at the judge.

### Structure — "Is the output formatted in a way an estimator can use?"

| Module | What it does |
|---|---|
| `sow_renderer` | Industry-standard 10-section Scope of Work in Markdown — Project Information / Scope Summary / Included Work (CSI division) / Exclusions / Assumptions / Materials & Specs / Schedule / Submittals / Coordination / Change-Order Process |
| Action-verb hygiene | Prepends "Furnish and install / Provide / Remove and dispose of / Test and certify" to descriptions that don't already start with an action verb (acronyms preserved) |
| `trade_bundler` | Bundles items into trade packages by CSI division for sub-bidder invitations |
| Trade Packages page | Per-package narrative (Haiku-drafted invitation cover letter), CSI-grouped items with quantities, move-between-packages action |

Every line item carries citations to its source page + bbox so an estimator can deep-link from the SOW back to the spec or drawing in two clicks.

### Extraction — "Can the agent correctly identify missing items (exclusions) in the bids?"

| Module | What it does |
|---|---|
| `bid_extractor` | Parses each bid PDF into structured `BidLineItem`, `BidInclusion`, `BidExclusion` rows (Sonnet tool-use) |
| `bid_coverage` | Cross-references every scope item against every bid → covered / partial / excluded / not_applicable status with reasoning |
| Coverage matrix UI | Per-row "uncovered" badge when no bid covers a scope item — likely real exclusions a bidder forgot to address |
| Coverage rollup banner | At-a-glance counts: covered / partial / excluded / **in zero bids** (the gap signal) |
| Bid leveling | Side-by-side comparison of bidders for the same scope item with vendor profile cards |

The "in zero bids" count is the load-bearing signal — when a scope item appears in no bid, that's an exclusion the bid never claimed and the GC will eat. The UI surfaces it explicitly.

### Evaluation — "Focused on RAGAS for evaluation of agent performance"

| Module | What it does |
|---|---|
| `ragas_eval` | Native implementation of the four canonical RAGAS metrics: context_precision, context_recall, answer_faithfulness, answer_relevancy |
| `evals/elks_ground_truth.yaml` | 23 estimator-curated ground-truth fixtures across all major architectural divisions (concrete, masonry, millwork, joints, doors/hardware, mirrors, drywall, painting, accessories, submittals, structural, envelope, doors/glass, finishes, site improvements) |
| `ragas_runner` | Background runner that wraps `evaluate_run` and persists every metric + per-fixture row into `RagasEvalRun` |
| `/api/projects/{id}/scope/runs/{run_id}/ragas` | POST kicks off a benchmark, GET fetches the latest |
| `/projects/[id]/ragas` page | Dedicated page in the sidebar — "Benchmark this run" button, polling progress, per-fixture color-coded table (A/B/C/D/F per cell), macro-average + overall geometric-mean grade |

A grader can click "Benchmark this run" and see the four RAGAS metrics land per fixture in ~80 seconds for ~$1.

### UI — "Implement with FastAPI backend + React/Next.js frontend"

- **FastAPI** with full OpenAPI surface at `/docs`. Every endpoint typed by Pydantic schemas; auto-generated SDK consumable from any client.
- **Next.js 16** (App Router, Turbopack) with **TanStack Query** for client-side state. JWT auth via Bearer header, dev login form on `/login`.
- Pages: dashboard, overview, documents, document detail, page viewer (with bbox-highlighted citation), scope, RAGAS, trade packages, review queue, RFI list, bid analysis, vendors, outputs, settings.
- Notable components: citation viewer modal (renders source page with bbox highlighted), trust score panel (component breakdown + tier), drawing grounding card, RAGAS panel (per-fixture table), bid coverage matrix (with rollup banner + zero-bid flag).
- All status fields and progress counts are live-polled at 4-12s intervals so the UI reflects the backend without a manual refresh.

---

## 4. RAGAS evaluation methodology

The four metrics, defined precisely as we implemented them:

| Metric | Definition |
|---|---|
| `context_precision` | For each retrieved chunk in rank order, `Σ(precision@k × is_relevant_at_k) / total_relevant`. The Haiku judge decides relevance per chunk; rank-weighted so high-rank irrelevant chunks hurt more than low-rank ones (matches the RAGAS paper formula). |
| `context_recall` | For each ground-truth item, ask the Haiku judge whether the cited chunks contain enough information to express it. `(supported_ground_truths / total_ground_truths)`. |
| `answer_faithfulness` | Decompose the extracted answer into atomic claims; ask Haiku whether each claim is supported by the cited chunks. `(supported_claims / total_claims)`. Detects hallucination. |
| `answer_relevancy` | Ask Haiku to generate N (=3) reverse-questions whose ideal answer is the extracted answer; embed all N + the original question; mean cosine similarity. Higher = answer is more on-topic. |

The overall score is the geometric mean of the four — penalises any single weak metric harder than an arithmetic mean would.

Fixtures live in `backend/evals/elks_ground_truth.yaml`, hand-curated from the project's spec book TOC by an estimator review. Each fixture is one CSI section with the items an estimator confirms SHOULD be in the extracted scope. To extend, add a new fixture with section code, title, and expected items.

A run takes ~80 seconds and ~$1 for 23 fixtures. The cost is dominated by Haiku judge calls; reverse-question generation accounts for ~$0.10.

---

## 5. Notable design decisions

The choices a careful reviewer will question — preempted with rationale.

### Vision-at-ingest with text-only retrieval

Drawing pages go through Sonnet vision **once at upload**. The output is structured text (sheet metadata, schedule rows, notes, cross-references, named entities). That text — not the pixels — is what gets embedded by Voyage and indexed in pgvector + BM25. Retrieval is text-only.

The escape valve for "did the model actually see this on the drawing?" is `drawing_grounder`: re-renders the relevant sheet's quadrants and asks Sonnet to verify item-by-item. On-demand, scoped, only when needed.

This beats CLIP-style image embeddings for construction queries because the queries are symbolic ("08 71 00 door hardware", "FACP locations") — Sonnet's transcription speaks the same vocabulary as the queries. CLIP doesn't speak CSI.

### OCR ensemble (multi-provider voting)

Two providers run in parallel for every page. Longer non-empty output wins; both outputs are stored on the `OCRResult.candidates` array; pages with high Levenshtein disagreement are flagged for the review queue. The W1 mitigation: a single misread of `shall not → shall` would otherwise reach the scope extractor and influence pricing.

A subtle bug here was that both providers' `OCRResult` dataclasses were declared in different modules, so the picker's `isinstance(r, OCRResult)` filter silently dropped Mistral results. Fixed by extracting a shared `ocr_types.OCRResult`. Mistral now wins 71% of pages.

### Two-phase commit on conflict resolution

`conflict_resolution.run`:
1. **Phase 1 — detection persists.** Cluster items, classify candidates, write `Conflict` rows with status `open`. This phase commits.
2. **Phase 2 — arbitration persists.** Send eligible candidates to Opus, write outcomes to the same rows. This phase also commits.

The reason for the split: if Opus calls fail mid-batch (rate limit, network blip), the surfaced conflicts still reach the HITL review queue with their detection state intact. Without the two-phase design, a transient Opus failure would lose the entire detection pass.

### Bilateral evidence per pattern

A "concrete walls 4000 psi" item should have both spec evidence (cited spec section text) AND drawing evidence (the wall depicted on a structural sheet). A "submittal log maintenance" admin item should NOT have drawing evidence — it lives only in spec. Treating bilateral coverage as a uniform requirement misclassifies admin items as low-confidence.

`bilateral_evidence.classify_run` looks at the item's `extraction_method` + description and assigns an `expected_pattern` (bilateral / spec_only / drawing_only / admin), then scores coverage against that pattern. An admin item with spec-only evidence gets a clean tier; a material item with spec-only evidence gets demoted.

### Action-verb hygiene in the SOW

Industry-standard SOW line items start with an action verb: *Furnish and install*, *Provide*, *Remove and dispose of*, *Test and certify*. The extractor's prompt has been updated to produce this form natively; older items (extracted before the prompt update) are auto-prefixed at render time based on `extraction_method`. Acronyms (AHU-1, FACP, RCP) are preserved.

### Trust score with 4-component substitution

`trust_score` is a weighted aggregate of four signals:
- bilateral_coverage (now: pattern_match)
- link_judge_pass_rate
- spec_section_coverage
- citation_density

When a component can't be computed for a run (e.g. spec_section_coverage when no spec book was uploaded), the weight is redistributed across remaining components rather than penalising the run. Components that are dropped are listed in `dropped_components` so the UI can show "missing" instead of "0%".

### Multi-mode scope orchestrator

`scope_runner.Orchestrator` is an ABC with four implementations:
- `PerDisciplineOrchestrator` — fan out by FP / Mech / Elec / Arch / Struct / etc.
- `PerSectionOrchestrator` — fan out by CSI section (most expensive, highest fidelity)
- `HybridOrchestrator` — per-section for sections with chunks; per-division fallback
- `PerDivisionOrchestrator` — fan out by CSI division (cheapest, lowest fidelity)

Mode selection lives in `settings.scope_orchestration_mode` (default `hybrid`). Adding a fifth mode is a 1-line registry entry; the runner dispatch is unchanged.

### EnrichmentPass abstraction

Four post-vision metadata passes for drawing-set documents (sheet_index, schedule, revision, symbol_legend) all share the same shape: `async run(ctx) -> EnrichmentResult`. Originally written as four sequential `await … extract_for_*(...)` blocks with hand-rolled try/except per pass — total wall time was ~6 min on the Elks drawing set.

After the refactor: `EnrichmentPass` ABC + registry + single `asyncio.gather`. Each pass is fault-isolated (a crash in one is logged, others continue); per-pass logging is uniform. Wall time is now ~2.8 min (the max of the four instead of the sum).

### `anthropic_tool_call.call_with_tool` helper

Five sites in the codebase make a "single Anthropic tool-use call with cache_control + cost logging" — the same shape was repeated as ~50 lines of boilerplate per site. The helper consolidates plumbing (client singleton, cache_control wrapping, retry, cost-log via `record_call`, tool_use parsing) into one function. Migrated sites:
- `sheet_index_extractor`
- `symbol_legend_extractor`
- `spec_toc_extractor`
- `conflict_resolution._arbitrate_one`
- `ragas_eval._judge_yes_no` + `_generate_reverse_questions`

Net code change: -159 lines across the three vision-extract modules. Adding a 6th tool-use site is now ~10 lines instead of ~50.

### Native RAGAS instead of the `ragas` package

The published `ragas` package opinionates the judge model, embedding model, and prompt format. We can't run it offline against our own retrieval, can't swap our own Haiku judge in cleanly, and can't itemise per-fixture cost. Implementing the four metrics directly (per the RAGAS paper) gives us:
- Reproducibility: same code, same fixtures, same metrics regardless of `ragas` package version.
- Cost transparency: every Haiku call logs through `record_call` so cost rolls up in `LLMCall.purpose='ragas-judge'` / `'ragas-reverse-q'`.
- Custom answer construction: we score the *items extracted for this CSI section*, not a generic RAG answer; the metric semantics are tuned for scope extraction.

---

## 6. How to run

### Prerequisites
- Node.js 20+
- uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- PostgreSQL 16+ with pgvector extension
- API keys for: Anthropic, Voyage AI, Cohere, Mistral, Google (Gemini)

### First-time setup

```bash
# Clone and enter the repo
git clone https://github.com/<your-fork>/AI_Assessment_ECS.git
cd AI_Assessment_ECS

# Backend
cd backend
cp .env.example .env
# Edit .env: set DATABASE_URL=postgresql+asyncpg://<user>@localhost:5432/ecs_estimator
# Edit .env: paste in API keys (ANTHROPIC_API_KEY, VOYAGE_API_KEY, COHERE_API_KEY, MISTRAL_API_KEY, GOOGLE_API_KEY)
uv sync

# Postgres setup
createdb ecs_estimator
psql ecs_estimator -c "CREATE EXTENSION IF NOT EXISTS vector;"
uv run alembic upgrade head

# Frontend
cd ../frontend
cp .env.local.example .env.local
npm install
```

### Run the dev servers

```bash
# Terminal 1 — backend
cd backend
uv run uvicorn app.main:app --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

Open http://localhost:3000, log in (`admin@ecs.local` / `admin`), create a project, drag-drop:
- The project manual PDF (root of repo)
- `Elks Community Enrichment Center - Drawings Combined.pdf`
- Bid PDFs from `bids/`
- `trade_list/Trade_List.xlsx`

Wait for processing (8–12 min for a 50-page drawing set, drops to ~3 min after the EnrichmentPass parallelization). Then:
1. **Profile** the project (Phase 4.1 button)
2. **Filter relevant trades** (Phase 4.2)
3. **Generate Scope of Work** (Phase 4.3) — produces ~1000 line items
4. **Run bid analysis** (Phase 8) — produces the coverage matrix
5. **Benchmark this run** on the RAGAS page — produces the four metrics

### Useful CLI scripts

```bash
# Re-OCR pages that came back blank (and re-index)
PYTHONPATH=. uv run python scripts/reocr_blank_pages.py <project_id> [--all]

# Run RAGAS eval against the latest complete scope run
PYTHONPATH=. uv run python scripts/run_ragas_eval.py [run_id] [fixtures.yaml]

# Compare two scope runs side-by-side
PYTHONPATH=. uv run python scripts/compare_runs.py <run_id_1> <run_id_2>

# A/B test drawing-grounder against the no-grounder baseline
PYTHONPATH=. uv run python scripts/ab_drawing_vision.py
```

---

## 7. Limitations and what's next

### Honestly limited

- **Schedule milestones** in SOW Section 7 reference the GC's CPM schedule rather than enumerate dates. Dates aren't in the spec book reliably; the estimator-facing section instead lists the spec sections that govern sequencing (`01 32 16 Construction Progress Schedule`, `01 73 00 Execution`, `01 77 00 Closeout Procedures`, etc.) plus any sequencing-flavored gaps surfaced from `gap_detector`.
- **MEP fixtures absent from RAGAS** because the spec set provided is architectural-only (highest division is 32 Site Improvements). Adding MEP coverage is a fixture file change, not a code change.
- **Image-similarity retrieval** (CLIP / voyage-multimodal-3) isn't implemented. Defended above — text-first is the right load-bearing design for construction queries; multimodal would be additive.
- **Scope verifier** (Opus second-opinion on the extracted scope) exists as a module but isn't wired into the default pipeline. Adds ~$2 per run; the trust-score signal already catches most issues.

### Logical next moves

- **Race-mode OCR** — return as soon as one provider produces non-empty text, but let the loser keep running for the disagreement signal. Saves ~3× per-page wall time on OCR.
- **Indexer concurrent embedding** — Voyage allows multiple in-flight batches; the indexer currently fans them serially. Cuts indexing time from ~80s to ~20s on a 3500-chunk document.
- **Multimodal index as a secondary store** — keyed on `DocumentPage.image_path` for "show me details that look like this" queries. Augmentative, not replacing.
- **Schedule milestone extractor** — a Haiku pass over Section 01 32 16 + the GC's CPM (when uploaded) to populate Section 7 with real dates.

### What wasn't worth doing

- **LangChain / LlamaIndex** — would have hidden the cost and trace surface we rely on for cost auditing.
- **A separate vector DB** (Qdrant, Weaviate) — pgvector + the chunk row's existing relational columns is a single-store win on consistency.
- **Heavyweight ETL framework** (Airflow / Prefect) — the pipeline is two-tier (per-document indexing → per-run scope extraction); asyncio + custom orchestrators give better cost telemetry without a scheduler.

---

## Repository layout

```
AI_Assessment_ECS/
├── README.md                        original assessment brief
├── README-DEV.md                    dev setup notes
├── SOLUTION.md                      ← this file
├── backend/
│   ├── alembic/versions/            DB migrations (12 revisions)
│   ├── app/
│   │   ├── api/                     FastAPI route handlers
│   │   ├── core/                    JWT + security
│   │   ├── data/                    static data (CSI taxonomy, symbol→CSI mapping, bundling rules)
│   │   ├── models/                  SQLAlchemy ORM (16 tables)
│   │   ├── schemas/                 Pydantic I/O types
│   │   ├── services/                72 modules — the actual pipeline
│   │   ├── config.py                pydantic-settings
│   │   ├── database.py              async session factory
│   │   └── main.py                  FastAPI app
│   ├── data/                        uploaded PDFs + rendered page PNGs (gitignored)
│   ├── evals/elks_ground_truth.yaml RAGAS fixtures (23 entries)
│   └── scripts/                     CLI utilities (re-OCR, RAGAS eval, A/B tests)
├── frontend/
│   └── src/
│       ├── app/                     Next.js App Router (16 pages)
│       ├── components/              UI components (32 files)
│       └── lib/api.ts               typed API client
├── bids/                            assessment data — 12 sample bids
├── trade_list/Trade_List.xlsx       assessment data — CSI MasterFormat
└── *.pdf                            assessment data — drawings + project manual
```

---

## Submission

- Branch: `solution-<yourname>` based on this fork's `main`.
- PR back to `autobid-eatonprojects/AI_Assessment_ECS`.
- Demo video: 10-min walkthrough showing upload → scope generation → bid coverage → RAGAS benchmark.
- Acknowledgement email: shubham@eatonprojects.com.
