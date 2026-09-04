"""
Sequential batch planning and merging for large assessments.

A single LLM call degrades once it is asked for a large number of questions at
once, so an assessment bigger than `QUESTION_BATCH_SIZE` is generated as several
calls that run **one after another** and are merged back into one payload.

Sequential rather than parallel, and that single fact is what makes this module
small. Each call is given the questions the earlier calls already produced, so
a batch can see what has been asked and avoid restating it. Everything the
parallel design needed in order to stop two blind batches colliding — keeping a
question type whole inside one batch, prioritising MTF, scoring one packing
against another — is gone, because the collision it guarded against can no
longer happen.

Batching is along the **question-type axis only**. Course-scoped batches were
deliberately rejected: a comprehensive assessment is instructed to prefer
cross-course scenario questions (see resources/prompts.yaml), which a batch that
can only see one course is structurally incapable of writing. Every batch
therefore receives the full content context.

Three whole-assessment properties have to survive the split. All three are
divided here, in Python, before the first call — never recomputed per batch,
because re-running a percentage split inside each batch rounds independently and
the parts stop summing to the whole:

  * **Question types and counts.** `plan_type_counts` pours the requested counts
    into bins, so the plan matches the request by construction.

  * **Bloom's levels.** `generator.compute_blooms_by_type` produces the exact
    ordered level list per question type for the whole assessment — the same
    call the single-call path makes — and batches consume those lists from a
    queue. The slices are therefore contiguous, non-overlapping, and reassemble
    into the original by construction.

  * **Course counts.** The requested percentages are converted once into exact
    integer per-course counts, then apportioned across batches by largest
    remainder, so the per-batch counts sum to the whole-assessment target.

Everything here is pure — no I/O, no LLM, no DB.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional

from .config import QUESTION_BATCH_SIZE
from .questions import (
    CANONICAL_BUCKETS,
    TYPE_KEY_BY_BUCKET,
    new_question_id,
    resolve_bucket,
)

logger = logging.getLogger(__name__)

# Types are planned in canonical bucket order so a batch's contents line up with
# the order the merged payload lays its buckets out in.
TYPE_ORDER: List[str] = [TYPE_KEY_BY_BUCKET[b] for b in CANONICAL_BUCKETS]

# How much of a question's stem is carried into the next batch's "already
# generated" list. Enough to recognise the concept, not the whole item.
STEM_MAX_CHARS = 200


@dataclass
class Batch:
    """One LLM call's worth of work."""

    index: int
    type_counts: Dict[str, int]
    blooms_by_type: Dict[str, List[str]] = field(default_factory=dict)
    course_counts: Dict[str, int] = field(default_factory=dict)
    # The last batch authors the blueprint: it is the only call that has seen
    # every question, via the "already generated" list.
    is_final: bool = False

    @property
    def total(self) -> int:
        return sum(self.type_counts.values())

    def describe(self) -> str:
        counts = ", ".join(f"{k}={v}" for k, v in self.type_counts.items())
        return f"batch {self.index + 1} ({self.total} questions: {counts})"


def _ordered_types(question_type_counts: Dict[str, int]) -> List[str]:
    """Types with a positive count, canonical order first, unknowns appended."""
    known = [t for t in TYPE_ORDER if int(question_type_counts.get(t) or 0) > 0]
    extra = [
        t for t in question_type_counts
        if t not in TYPE_ORDER and int(question_type_counts.get(t) or 0) > 0
    ]
    return known + extra


def plan_type_counts(question_type_counts: Dict[str, int]) -> List[Dict[str, int]]:
    """
    Pour the types into batches in canonical order, flushing at the cap.

    Types are packed together rather than given a batch each, so five types
    totalling 100 questions become four calls instead of six or more. A type
    whose count exceeds the cap is split across batches — which is harmless now
    that the second half can see the first.

    Every batch but the last comes out exactly full, so this produces the
    smallest number of batches the request can be served in.
    """
    batch_cap = max(1, QUESTION_BATCH_SIZE)
    batches: List[Dict[str, int]] = []
    current: Dict[str, int] = {}

    def flush() -> None:
        nonlocal current
        if current:
            batches.append(current)
            current = {}

    for type_key in _ordered_types(question_type_counts):
        remaining = int(question_type_counts.get(type_key) or 0)
        while remaining > 0:
            space = batch_cap - sum(current.values())
            take = min(remaining, space)
            if take <= 0:
                # Only reachable while `current` holds something — with an empty
                # batch `space` is positive. So the flush always makes progress
                # and this cannot spin.
                flush()
                continue
            current[type_key] = current.get(type_key, 0) + take
            remaining -= take

    flush()
    return batches


def apportion(
    amount: int,
    remaining: Dict[str, float],
    order: Iterable[str],
) -> Dict[str, int]:
    """
    Split `amount` over `remaining` in proportion, by largest remainder.

    Sums to exactly `amount` (or to `sum(remaining)` when that is smaller), which
    is the whole point: giving each batch a rounded *percentage* lets every batch
    round independently, and the parts then stop adding up to the whole.

    Ties break on `order` so the split is identical from run to run.
    """
    positions = {key: i for i, key in enumerate(order)}
    pool = {k: float(v) for k, v in remaining.items() if float(v) > 0}
    total = sum(pool.values())
    amount = int(amount)
    if amount <= 0 or total <= 0:
        return {}
    if amount > total:
        amount = int(total)

    exact = {k: amount * v / total for k, v in pool.items()}
    out = {k: int(v) for k, v in exact.items()}
    shortfall = amount - sum(out.values())
    if shortfall > 0:
        ranked = sorted(
            exact,
            key=lambda k: (-(exact[k] - int(exact[k])), positions.get(k, len(positions))),
        )
        for key in ranked[:shortfall]:
            out[key] += 1
    return {k: v for k, v in out.items() if v > 0}


def slice_blooms(
    blooms_queues: Dict[str, Deque[str]],
    type_counts: Dict[str, int],
) -> Dict[str, List[str]]:
    """
    Take one batch's Bloom's levels off the front of each type's queue.

    Consuming from a queue rather than indexing with an offset is what makes the
    slices provably contiguous and non-overlapping: a level can only be handed
    out once, because taking it removes it.
    """
    out: Dict[str, List[str]] = {}
    for type_key, count in type_counts.items():
        queue = blooms_queues.get(type_key)
        if not queue:
            continue
        levels = [queue.popleft() for _ in range(min(int(count), len(queue)))]
        if levels:
            out[type_key] = levels
    return out


def plan_batches(
    question_type_counts: Dict[str, int],
    blooms_by_type: Optional[Dict[str, List[str]]] = None,
    course_targets: Optional[Dict[str, int]] = None,
) -> List[Batch]:
    """
    Build the full batch plan.

    `blooms_by_type` is empty when Bloom's is disabled, and `course_targets` is
    None whenever no course weightage was supplied — both are handled.
    """
    type_counts_per_batch = plan_type_counts(question_type_counts or {})
    if not type_counts_per_batch:
        return []

    blooms_queues: Dict[str, Deque[str]] = {
        type_key: deque(levels)
        for type_key, levels in (blooms_by_type or {}).items()
        if levels
    }

    course_remaining: Dict[str, float] = {
        cid: float(count) for cid, count in (course_targets or {}).items() if count > 0
    }
    course_order = list(course_remaining)

    batches: List[Batch] = []
    for i, counts in enumerate(type_counts_per_batch):
        batch = Batch(
            index=i,
            type_counts=counts,
            blooms_by_type=slice_blooms(blooms_queues, counts),
            is_final=(i == len(type_counts_per_batch) - 1),
        )
        if course_remaining:
            batch.course_counts = apportion(batch.total, course_remaining, course_order)
            for cid, taken in batch.course_counts.items():
                course_remaining[cid] -= taken
        batches.append(batch)

    _assert_plan_is_complete(batches, question_type_counts, blooms_by_type, course_targets)
    return batches


def _assert_plan_is_complete(
    batches: List[Batch],
    question_type_counts: Dict[str, int],
    blooms_by_type: Optional[Dict[str, List[str]]],
    course_targets: Optional[Dict[str, int]],
) -> None:
    """
    Confirm the plan accounts for everything that was requested.

    Cheap and inline rather than a test, because the failure mode it guards
    against — a Bloom's slice that silently drops or repeats a level, or a course
    split that no longer sums to the target — produces an assessment that looks
    entirely correct.
    """
    planned_types: Counter = Counter()
    for batch in batches:
        planned_types.update(batch.type_counts)
    requested_types = {
        k: int(v) for k, v in (question_type_counts or {}).items() if int(v or 0) > 0
    }
    if dict(planned_types) != requested_types:
        raise ValueError(
            f"Batch plan does not match the request: planned {dict(planned_types)}, "
            f"requested {requested_types}"
        )

    for type_key, levels in (blooms_by_type or {}).items():
        if not levels:
            continue
        reassembled: List[str] = []
        for batch in batches:
            reassembled.extend(batch.blooms_by_type.get(type_key, []))
        # Slices are taken off the front, so the concatenation must equal the
        # leading run of the original list. Its length is bounded by whichever
        # runs out first: the levels available, or the questions requested.
        expected = list(levels)[:min(len(levels), requested_types.get(type_key, 0))]
        if reassembled != expected:
            raise ValueError(
                f"Bloom's slices for '{type_key}' do not reassemble into the "
                f"original assignment (got {len(reassembled)} levels, "
                f"expected {len(expected)})"
            )

    if course_targets:
        planned_courses: Counter = Counter()
        for batch in batches:
            planned_courses.update(batch.course_counts)
        expected_courses = {k: int(v) for k, v in course_targets.items() if int(v or 0) > 0}
        if dict(planned_courses) != expected_courses:
            raise ValueError(
                f"Course counts do not sum to the target: planned "
                f"{dict(planned_courses)}, target {expected_courses}"
            )


def _unwrap_questions(result: Any) -> Dict[str, Any]:
    """
    Read the bucket dict out of one batch's response.

    Every batch returns the full `{"blueprint": ..., "questions": {...}}` shape
    the single-call path returns — the final batch genuinely, the earlier ones
    with the `blueprint` key dropped from their schema. A bare bucket object is
    accepted too so a model that volunteers one does not merge as empty.
    """
    if not isinstance(result, dict):
        return {}
    inner = result.get("questions")
    if isinstance(inner, dict):
        return inner
    return {k: v for k, v in result.items() if k != "blueprint"}


def _stem(question: Dict[str, Any], type_key: str) -> str:
    """
    The part of a question that identifies what it tests.

    MTF is the exception that forces this to be a function rather than a field
    read: an MTF question has no `question_text` at all. Its schema is
    `matching_context` plus `pairs[{left, right}]`, and the left-hand column is
    where duplication actually lives — two MTFs mapping the same items to
    differently worded descriptions are the same question.
    """
    if type_key == "mtf":
        parts: List[str] = []
        context = str(question.get("matching_context") or "").strip()
        if context:
            parts.append(f"context: {context}")
        pairs = question.get("pairs")
        if isinstance(pairs, list):
            lefts = [
                str(p.get("left")).strip()
                for p in pairs
                if isinstance(p, dict) and str(p.get("left") or "").strip()
            ]
            if lefts:
                parts.append("left items: " + "; ".join(lefts))
        return " | ".join(parts)

    text = " ".join(str(question.get("question_text") or "").split())
    if len(text) <= STEM_MAX_CHARS:
        return text
    return text[:STEM_MAX_CHARS - 3].rstrip() + "..."


def summarize_for_dedup(result: Any) -> List[Dict[str, str]]:
    """
    One compact entry per question in a batch's response.

    This is what the next batch is shown so it does not restate ground already
    covered. Only the stem and the labels a later batch can act on — never the
    options, reasoning or rationale, which would multiply the prompt without
    telling it anything a duplicate check needs.
    """
    summary: List[Dict[str, str]] = []
    for name, questions in _unwrap_questions(result).items():
        if not isinstance(questions, list):
            continue
        bucket = name if name in CANONICAL_BUCKETS else (resolve_bucket(name) or name)
        type_key = TYPE_KEY_BY_BUCKET.get(bucket, bucket)
        for question in questions:
            if not isinstance(question, dict):
                continue
            stem = _stem(question, type_key)
            if not stem:
                continue
            kcm = (
                ((question.get("reasoning") or {}).get("competency_alignment") or {}).get("kcm")
                if isinstance(question.get("reasoning"), dict)
                else None
            )
            summary.append({
                "type": type_key,
                "stem": stem,
                "blooms_level": str(question.get("blooms_level") or "").strip(),
                "difficulty_level": str(question.get("difficulty_level") or "").strip(),
                "competency_sub_theme": (
                    str((kcm or {}).get("competency_sub_theme") or "").strip()
                    if isinstance(kcm, dict) else ""
                ),
                "course_name": str(question.get("course_name") or "").strip(),
            })
    return summary


def merge_batches(
    batch_results: List[Any],
    *,
    mint_ids: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Combine every batch's questions into one bucket dict.

    Buckets are laid out in `CANONICAL_BUCKETS` order. That is a correctness
    requirement, not presentation: `questions.normalize_assessment` backfills
    `question_order` from the payload's own bucket order, so a merge in any other
    order renumbers every question in every export.

    Fresh `question_id`s are minted because each batch numbers its own questions
    from one — every batch returns a `q_001`. `normalize_assessment` would
    de-duplicate those into `mcq_001_2`-style ids without losing anything, but
    minting here keeps them clean.
    """
    merged: Dict[str, List[Dict[str, Any]]] = {bucket: [] for bucket in CANONICAL_BUCKETS}

    for result in batch_results:
        for name, questions in _unwrap_questions(result).items():
            if not isinstance(questions, list):
                continue
            bucket = name if name in merged else (resolve_bucket(name) or name)
            merged.setdefault(bucket, [])
            for question in questions:
                if not isinstance(question, dict):
                    continue
                if mint_ids:
                    question["question_id"] = new_question_id()
                merged[bucket].append(question)

    return merged
