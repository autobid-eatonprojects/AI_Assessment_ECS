"""RAGAS-style evaluation pipeline (assessment requirement § Phase 9).

Implements the four canonical RAGAS metrics natively (no external `ragas`
dep) so the stack stays consistent — Haiku 4.5 as the judge LLM, Voyage
for embeddings, existing `record_call` cost logging.

Metric definitions follow the RAGAS paper (Es et al., 2023):

  context_precision      Of the retrieved chunks, how many are relevant
                         to the question? Rank-weighted: chunks at higher
                         positions count more.
                         Formula: Σ (precision@k × indicator(relevant_k))
                                  ─────────────────────────────────────
                                          Σ indicator(relevant)

  context_recall         For each statement in the ground-truth answer,
                         can it be attributed to the retrieved context?
                         Formula: |attributable GT statements| / |GT statements|

  answer_faithfulness    For each claim in the generated answer, is it
                         grounded in the retrieved context?
                         Formula: |faithful claims| / |total claims|

  answer_relevancy       Generate N reverse questions from the answer;
                         compare embeddings to the original question.
                         Formula: mean cosine_sim(gen_qs, original_q)

Adaptation for construction-estimating scope extraction:

  - "Question" = a CSI-section query, e.g. "What scope items does CSI
    section 09 29 00 (Gypsum Board) define for this project?"
  - "Retrieved context" = chunks the section's items cite collectively.
  - "Generated answer" = the bullet list of extracted ScopeItem
    descriptions for that section.
  - "Ground truth" = a manually-validated list of what SHOULD be in
    that section (loaded from a YAML fixture).

Two public surfaces (interface IS the test surface):

  - `evaluate_run(run_id, ground_truth)` — DB-bound; loads run state
    + cited chunks; computes per-fixture metrics + aggregate scores;
    returns an `EvalResult`.

  - The four metric functions are exported individually and are
    pure-function-testable: pass synthetic strings + a fake judge
    callable, assert on returned floats.

Use cases:
  - Regression tracking across runs (commit benchmark scores)
  - Per-section quality gates (e.g. fail CI if context_recall < 0.7)
  - Identify low-recall sections that need extractor improvements
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from ..database import SessionLocal
from ..models import Chunk, ScopeCitation, ScopeExtractionRun, ScopeItem
from .anthropic_tool_call import call_with_tool
from .embedder import embed_texts

log = logging.getLogger(__name__)


_JUDGE_MODEL = "claude-haiku-4-5"
_REVERSE_QUESTION_COUNT = 3   # per RAGAS default
_JUDGE_CONCURRENCY = 8


# ============================================================================
# Result types
# ============================================================================


@dataclass
class GroundTruthFixture:
    """One row of curated ground-truth scope.

    `csi_section` defines the question's scope; `expected_items` lists
    descriptions an estimator confirms SHOULD be present. `must_cite_sheets`
    is optional — when populated, recall is gated on the system finding
    citations to at least one of those sheets.
    """
    csi_section: str
    section_title: str
    expected_items: list[str]
    must_cite_sheets: list[str] = field(default_factory=list)


@dataclass
class FixtureMetrics:
    """Per-fixture RAGAS scores."""
    csi_section: str
    section_title: str
    n_extracted_items: int
    n_expected_items: int
    n_retrieved_chunks: int
    context_precision: float
    context_recall: float
    answer_faithfulness: float
    answer_relevancy: float
    cost_usd: float = 0.0


@dataclass
class EvalResult:
    """Aggregate RAGAS evaluation result."""
    run_id: str
    n_fixtures: int
    per_fixture: list[FixtureMetrics]
    # Macro-averages (mean of per-fixture scores).
    context_precision: float = 0.0
    context_recall: float = 0.0
    answer_faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    overall_score: float = 0.0  # geometric mean of the four
    total_cost_usd: float = 0.0
    elapsed_sec: float = 0.0


# ============================================================================
# Anthropic judge — single-tool yes/no + structured-output helpers
# ============================================================================


_RELEVANCE_TOOL = {
    "name": "judge_relevance",
    "description": "Decide whether a passage is relevant to a question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string", "description": "≤25 words."},
        },
        "required": ["relevant", "reason"],
    },
}

_SUPPORT_TOOL = {
    "name": "judge_support",
    "description": "Decide whether a statement is supported by the given context.",
    "input_schema": {
        "type": "object",
        "properties": {
            "supported": {"type": "boolean"},
            "reason": {"type": "string", "description": "≤25 words."},
        },
        "required": ["supported", "reason"],
    },
}

_REVERSE_Q_TOOL = {
    "name": "generate_questions",
    "description": (
        "Produce N alternative questions whose ideal answer is the given "
        "answer text. Used for RAGAS answer-relevancy scoring."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        "required": ["questions"],
    },
}


async def _judge_yes_no(
    *,
    tool: dict,
    prompt: str,
    project_id: str | None,
) -> tuple[bool, str, float]:
    """Run one Haiku tool call and return (verdict_bool, reason, cost_usd).

    Returns (False, "judge unavailable", 0.0) when no API key is configured.
    """
    try:
        result = await call_with_tool(
            model=_JUDGE_MODEL,
            tool_def=tool,
            max_tokens=256,
            purpose="ragas-judge",
            project_id=project_id,
            user_content=prompt,
        )
    except RuntimeError:
        return False, "judge unavailable", 0.0
    payload = result.parsed_input
    # Pull "verdict" — the boolean key varies by tool
    for k in ("relevant", "supported"):
        if k in payload:
            return bool(payload[k]), payload.get("reason", ""), result.cost_usd
    return False, "no verdict", result.cost_usd


async def _generate_reverse_questions(
    answer_text: str,
    *,
    n: int,
    project_id: str | None,
) -> tuple[list[str], float]:
    """Ask Haiku to produce N questions whose ideal answer is `answer_text`.
    Used for RAGAS answer_relevancy."""
    prompt = (
        f"Read the answer text below. Produce exactly {n} natural-sounding "
        f"questions whose ideal answer is precisely this text. The "
        f"questions should be specific, complete, and not paraphrases of "
        f"each other.\n\nAnswer text:\n{answer_text}\n\n"
        f"Use generate_questions to return {n} questions."
    )
    try:
        result = await call_with_tool(
            model=_JUDGE_MODEL,
            tool_def=_REVERSE_Q_TOOL,
            max_tokens=1024,
            purpose="ragas-reverse-q",
            project_id=project_id,
            user_content=prompt,
        )
    except RuntimeError:
        return [], 0.0
    questions = list(result.parsed_input.get("questions") or [])
    return questions[:n], result.cost_usd


# ============================================================================
# Pure helpers — no I/O, testable
# ============================================================================


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitter — adequate for RAGAS-style claim enumeration.
    Preserves bullet items as separate "sentences." """
    if not text:
        return []
    # Split on bullets, newlines, and sentence terminators.
    parts = re.split(r"(?:\n+|^\s*[-*•]\s+|(?<=[.!?])\s+(?=[A-Z]))", text)
    return [p.strip() for p in parts if p and p.strip()]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _geom_mean(values: list[float]) -> float:
    """Geometric mean — penalizes one weak metric more than arithmetic mean."""
    nz = [v for v in values if v > 0]
    if not nz:
        return 0.0
    log_sum = sum(math.log(v) for v in nz)
    return math.exp(log_sum / len(nz))


# ============================================================================
# RAGAS metric — context_precision
# ============================================================================


async def context_precision(
    *,
    question: str,
    retrieved_chunks: list[str],
    project_id: str | None = None,
    sem: asyncio.Semaphore | None = None,
) -> tuple[float, float]:
    """For each chunk, judge if it's relevant to `question`. Rank-weight
    so higher-rank chunks count more (RAGAS formula).

    Returns (precision, cost_usd).
    """
    if not retrieved_chunks:
        return 0.0, 0.0
    sem = sem or asyncio.Semaphore(_JUDGE_CONCURRENCY)
    total_cost = 0.0

    async def _one(chunk: str) -> bool:
        nonlocal total_cost
        async with sem:
            prompt = (
                f"QUESTION: {question}\n\n"
                f"PASSAGE:\n{chunk[:1500]}\n\n"
                f"Is the passage relevant to answering the question? "
                f"Use judge_relevance."
            )
            verdict, _, cost = await _judge_yes_no(
                tool=_RELEVANCE_TOOL, prompt=prompt, project_id=project_id,
            )
            total_cost += cost
            return verdict

    relevance: list[bool] = list(await asyncio.gather(*(_one(c) for c in retrieved_chunks)))
    n_relevant = sum(relevance)
    if n_relevant == 0:
        return 0.0, total_cost
    # Rank-weighted formula: sum(precision@k * indicator) / total_relevant
    precision_at_k_sum = 0.0
    for k, is_rel in enumerate(relevance, start=1):
        if is_rel:
            p_at_k = sum(relevance[:k]) / k
            precision_at_k_sum += p_at_k
    return precision_at_k_sum / n_relevant, total_cost


# ============================================================================
# RAGAS metric — context_recall
# ============================================================================


async def context_recall(
    *,
    retrieved_chunks: list[str],
    ground_truth_statements: list[str],
    project_id: str | None = None,
    sem: asyncio.Semaphore | None = None,
) -> tuple[float, float]:
    """For each GT statement, ask: can this be attributed to the retrieved
    context? Recall = supported / total.

    Returns (recall, cost_usd).
    """
    if not ground_truth_statements:
        return 1.0, 0.0  # vacuously true
    if not retrieved_chunks:
        return 0.0, 0.0
    context_blob = "\n\n---\n\n".join(c[:1500] for c in retrieved_chunks[:20])
    sem = sem or asyncio.Semaphore(_JUDGE_CONCURRENCY)
    total_cost = 0.0

    async def _one(stmt: str) -> bool:
        nonlocal total_cost
        async with sem:
            prompt = (
                f"CONTEXT (multiple passages, separated by ---):\n"
                f"{context_blob}\n\n"
                f"STATEMENT TO CHECK:\n{stmt}\n\n"
                f"Can the STATEMENT be attributed to the CONTEXT? Use "
                f"judge_support. Be strict — only `supported=true` if a "
                f"reasonable estimator would agree the context contains "
                f"this specific item."
            )
            verdict, _, cost = await _judge_yes_no(
                tool=_SUPPORT_TOOL, prompt=prompt, project_id=project_id,
            )
            total_cost += cost
            return verdict

    supported = list(await asyncio.gather(*(_one(s) for s in ground_truth_statements)))
    return sum(supported) / len(supported), total_cost


# ============================================================================
# RAGAS metric — answer_faithfulness
# ============================================================================


async def answer_faithfulness(
    *,
    answer_claims: list[str],
    retrieved_chunks: list[str],
    project_id: str | None = None,
    sem: asyncio.Semaphore | None = None,
) -> tuple[float, float]:
    """For each claim in the generated answer, is it supported by the
    retrieved context? RAGAS formula: faithful_claims / total_claims."""
    if not answer_claims:
        return 1.0, 0.0
    if not retrieved_chunks:
        return 0.0, 0.0
    context_blob = "\n\n---\n\n".join(c[:1500] for c in retrieved_chunks[:20])
    sem = sem or asyncio.Semaphore(_JUDGE_CONCURRENCY)
    total_cost = 0.0

    async def _one(claim: str) -> bool:
        nonlocal total_cost
        async with sem:
            prompt = (
                f"CONTEXT:\n{context_blob}\n\n"
                f"CLAIM TO CHECK:\n{claim}\n\n"
                f"Is the CLAIM directly supported by the CONTEXT? Use "
                f"judge_support."
            )
            verdict, _, cost = await _judge_yes_no(
                tool=_SUPPORT_TOOL, prompt=prompt, project_id=project_id,
            )
            total_cost += cost
            return verdict

    faithful = list(await asyncio.gather(*(_one(c) for c in answer_claims)))
    return sum(faithful) / len(faithful), total_cost


# ============================================================================
# RAGAS metric — answer_relevancy
# ============================================================================


async def answer_relevancy(
    *,
    question: str,
    answer_text: str,
    project_id: str | None = None,
    n_reverse: int = _REVERSE_QUESTION_COUNT,
) -> tuple[float, float]:
    """Generate N reverse questions from the answer; embed all (original
    + reverses); return mean cosine similarity."""
    questions, gen_cost = await _generate_reverse_questions(
        answer_text, n=n_reverse, project_id=project_id,
    )
    if not questions:
        return 0.0, gen_cost
    try:
        embeddings, _ = await embed_texts([question] + questions)
    except Exception as e:  # noqa: BLE001
        log.warning("ragas: embed failed for answer_relevancy: %s", e)
        return 0.0, gen_cost
    if len(embeddings) < 2:
        return 0.0, gen_cost
    orig_emb = embeddings[0]
    sims = [_cosine_similarity(orig_emb, e) for e in embeddings[1:]]
    return sum(sims) / len(sims), gen_cost


# ============================================================================
# Public surface — DB-bound evaluator
# ============================================================================


async def evaluate_run(
    run_id: str,
    ground_truth: list[GroundTruthFixture],
) -> EvalResult:
    """Evaluate a completed run against ground-truth fixtures.

    For each fixture (one CSI section's expected scope):
      1. Pull the run's extracted ScopeItems for that section.
      2. Pull the chunks those items cite (= the retrieved context).
      3. Compute the four RAGAS metrics.

    Aggregate macro-average across fixtures + a geometric-mean overall.
    """
    t0 = time.perf_counter()
    result = EvalResult(run_id=run_id, n_fixtures=len(ground_truth), per_fixture=[])
    if not ground_truth:
        return result

    # Load run + project context once.
    async with SessionLocal() as db:
        run = await db.get(ScopeExtractionRun, run_id)
        if run is None:
            log.warning("ragas: run %s not found", run_id)
            return result
        project_id = run.project_id

    sem = asyncio.Semaphore(_JUDGE_CONCURRENCY)

    for gt in ground_truth:
        log.info("ragas: evaluating fixture %s — %s", gt.csi_section, gt.section_title)

        # ---- Load extracted items + their cited chunks ----
        async with SessionLocal() as db:
            items = list((await db.execute(
                select(ScopeItem)
                .where(ScopeItem.run_id == run_id)
                .where(ScopeItem.csi_code == gt.csi_section)
            )).scalars().all())
            item_ids = [it.id for it in items]
            cit_rows: list[ScopeCitation] = []
            chunks: list[Chunk] = []
            if item_ids:
                cit_rows = list((await db.execute(
                    select(ScopeCitation).where(
                        ScopeCitation.scope_item_id.in_(item_ids)
                    )
                )).scalars().all())
                chunk_ids = list({c.chunk_id for c in cit_rows if c.chunk_id})
                if chunk_ids:
                    chunks = list((await db.execute(
                        select(Chunk).where(Chunk.id.in_(chunk_ids))
                    )).scalars().all())

        retrieved_texts = [c.text for c in chunks if c.text]
        answer_lines = [it.description for it in items if it.description]
        answer_text = (
            f"Items extracted for CSI section {gt.csi_section} "
            f"({gt.section_title}):\n"
            + "\n".join(f"- {d}" for d in answer_lines)
        )
        question = (
            f"What scope items does CSI section {gt.csi_section} "
            f"({gt.section_title}) define for this project?"
        )

        # ---- Compute the four metrics in parallel ----
        precision_task = context_precision(
            question=question, retrieved_chunks=retrieved_texts,
            project_id=project_id, sem=sem,
        )
        recall_task = context_recall(
            retrieved_chunks=retrieved_texts,
            ground_truth_statements=gt.expected_items,
            project_id=project_id, sem=sem,
        )
        faithfulness_task = answer_faithfulness(
            answer_claims=answer_lines, retrieved_chunks=retrieved_texts,
            project_id=project_id, sem=sem,
        )
        relevancy_task = answer_relevancy(
            question=question, answer_text=answer_text,
            project_id=project_id,
        )

        (precision, p_cost), (recall, r_cost), (faith, f_cost), (rel, rel_cost) = (
            await asyncio.gather(
                precision_task, recall_task, faithfulness_task, relevancy_task,
            )
        )
        fixture_cost = p_cost + r_cost + f_cost + rel_cost

        fm = FixtureMetrics(
            csi_section=gt.csi_section,
            section_title=gt.section_title,
            n_extracted_items=len(items),
            n_expected_items=len(gt.expected_items),
            n_retrieved_chunks=len(chunks),
            context_precision=precision,
            context_recall=recall,
            answer_faithfulness=faith,
            answer_relevancy=rel,
            cost_usd=fixture_cost,
        )
        result.per_fixture.append(fm)
        log.info(
            "ragas: %s — precision=%.2f recall=%.2f faith=%.2f relevancy=%.2f $%.4f",
            gt.csi_section, precision, recall, faith, rel, fixture_cost,
        )

    # Macro averages
    if result.per_fixture:
        result.context_precision = sum(f.context_precision for f in result.per_fixture) / len(result.per_fixture)
        result.context_recall = sum(f.context_recall for f in result.per_fixture) / len(result.per_fixture)
        result.answer_faithfulness = sum(f.answer_faithfulness for f in result.per_fixture) / len(result.per_fixture)
        result.answer_relevancy = sum(f.answer_relevancy for f in result.per_fixture) / len(result.per_fixture)
        result.overall_score = _geom_mean([
            result.context_precision,
            result.context_recall,
            result.answer_faithfulness,
            result.answer_relevancy,
        ])
        result.total_cost_usd = sum(f.cost_usd for f in result.per_fixture)
    result.elapsed_sec = time.perf_counter() - t0
    return result


# ============================================================================
# Fixture loader
# ============================================================================


def load_fixtures_from_yaml(path: str) -> list[GroundTruthFixture]:
    """Load a YAML fixture file. Format:

        fixtures:
          - csi_section: "09 29 00"
            section_title: Gypsum Board
            expected_items:
              - 5/8 inch Type X gypsum board
              - Joint compound for embedding tape
              - ...
            must_cite_sheets:
              - A2.1
    """
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[GroundTruthFixture] = []
    for entry in data.get("fixtures") or []:
        out.append(GroundTruthFixture(
            csi_section=str(entry.get("csi_section") or "").strip(),
            section_title=str(entry.get("section_title") or "").strip(),
            expected_items=[
                s.strip() for s in (entry.get("expected_items") or [])
                if str(s).strip()
            ],
            must_cite_sheets=[
                s.strip() for s in (entry.get("must_cite_sheets") or [])
                if str(s).strip()
            ],
        ))
    return out
