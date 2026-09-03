"""
Batch planning and merging for large assessments.

A single LLM call degrades once it is asked for a large number of questions at
once, so an assessment bigger than `QUESTION_BATCH_SIZE` is generated as several
calls that run in parallel and are merged back into one payload.

Batching is along the **question-type axis only**. Course-scoped batches were
deliberately rejected: a comprehensive assessment is instructed to prefer
cross-course scenario questions (see resources/prompts.yaml), which a batch that
can only see one course is structurally incapable of writing. Every batch
therefore receives the full content context.

Within those batches the types are *grouped*: a type is kept whole in a single
batch whenever the batch count allows it, and MTF is grouped ahead of every
other type. Splitting a type across calls is what lets two batches that cannot
see each other write near-duplicate questions from the same slice of content,
and MTF is the worst case because its pairs are drawn from one narrow topic.
The batch count is never raised to achieve this — it is fixed at whatever the
plain sequential packing needs, and grouping only redistributes within it. Where
grouping is impossible (see `_pack_grouped`) the sequential plan is used
unchanged.

One invariant has to survive the split, and it is structural here rather than
checked after the fact:

  * **Bloom's levels.** `generator.compute_blooms_by_type` already produces the
    exact ordered level list per question type for the whole assessment, and the
    prompt reads it positionally. Batches consume those lists from a queue, so
    the slices are contiguous, non-overlapping, and reassemble into the original
    by construction. Index arithmetic across a type that is split over several
    batches *and* packed alongside other types is precisely where an off-by-one
    hides, and a wrong Bloom's level is close to invisible in review.

Everything here is pure — no I/O, no LLM, no DB.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .config import BATCH_SIZE_BY_TYPE, QUESTION_BATCH_SIZE
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

# Types that are grouped ahead of everything else, most important first. An MTF
# question carries several pairs drawn from one narrow slice of content, so two
# batches independently writing MTFs over the same corpus is the shape most
# likely to produce overlapping pairings — and the most expensive to review.
PRIORITY_TYPES: List[str] = ["mtf"]


@dataclass
class Batch:
    """One LLM call's worth of work."""

    index: int
    type_counts: Dict[str, int]
    blooms_by_type: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.type_counts.values())

    def describe(self) -> str:
        counts = ", ".join(f"{k}={v}" for k, v in self.type_counts.items())
        return f"batch {self.index + 1} ({self.total} questions: {counts})"


def batch_size_for(type_key: str) -> int:
    """
    Chunk size for one question type.

    `BATCH_SIZE_BY_TYPE` is a provision — every entry is None by default, which
    falls back to the global size. It exists because output volume per question
    differs sharply by type, so the safe chunk size genuinely is not uniform.
    """
    override = BATCH_SIZE_BY_TYPE.get(type_key)
    if override and override > 0:
        return int(override)
    return max(1, QUESTION_BATCH_SIZE)


def _ordered_types(question_type_counts: Dict[str, int]) -> List[str]:
    """Types with a positive count, canonical order first, unknowns appended."""
    known = [t for t in TYPE_ORDER if int(question_type_counts.get(t) or 0) > 0]
    extra = [
        t for t in question_type_counts
        if t not in TYPE_ORDER and int(question_type_counts.get(t) or 0) > 0
    ]
    return known + extra


def _pack_sequential(question_type_counts: Dict[str, int]) -> List[Dict[str, int]]:
    """
    Pour the types into batches in canonical order, flushing at the cap.

    Types are packed together rather than given a batch each, so five types
    totalling 100 questions become four calls instead of six or more. A type
    whose count exceeds its own chunk size is split across batches.

    Every batch but the last comes out exactly full, so this produces the
    smallest number of batches the request can be served in. That count is what
    `_pack_grouped` is then held to — grouping redistributes within it and never
    adds a call.
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
        type_cap = batch_size_for(type_key)
        while remaining > 0:
            space = batch_cap - sum(current.values())
            type_space = type_cap - current.get(type_key, 0)
            take = min(remaining, space, type_space)
            if take <= 0:
                # Only reachable while `current` holds something — with an empty
                # batch both `space` and `type_space` are positive. So the flush
                # always makes progress and this cannot spin.
                flush()
                continue
            current[type_key] = current.get(type_key, 0) + take
            remaining -= take

    flush()
    return batches


def _priority_order(sizes: Dict[str, int], position: Dict[str, int]) -> List[str]:
    """
    The order types are offered a batch in: `PRIORITY_TYPES`, then largest first.

    Largest-first is what makes the placement work — every type placed after a
    given one is no bigger, so filling the tightest hole that fits cannot strand
    a larger type that had nowhere else to go. Priority types jump the queue
    regardless of size, which is precisely what protects them from being the one
    left over with no hole big enough. Canonical position breaks size ties so the
    plan is identical from run to run.
    """
    priority = [t for t in PRIORITY_TYPES if t in sizes]
    rest = sorted(
        (t for t in sizes if t not in priority),
        key=lambda t: (-sizes[t], position[t]),
    )
    return priority + rest


def _even_split(total: int, caps: List[int]) -> Optional[List[int]]:
    """
    Spread `total` as evenly as possible over slots with the given capacities.

    Used only for a type that has to be split because no single batch has room
    for it whole. Even is the right shape here — greedily filling the biggest
    hole first leaves a one-question tail, and a batch asked for a single
    question still costs a full call carrying the whole content context.

    Returns None if the capacities cannot hold `total`.
    """
    amounts = [0] * len(caps)
    remaining = int(total)
    while remaining > 0:
        open_slots = [i for i in range(len(caps)) if amounts[i] < caps[i]]
        if not open_slots:
            return None
        share, extra = divmod(remaining, len(open_slots))
        moved = 0
        for rank, i in enumerate(open_slots):
            want = share + (1 if rank < extra else 0)
            if want <= 0:
                continue
            give = min(want, caps[i] - amounts[i])
            amounts[i] += give
            moved += give
        if moved <= 0:
            # Unreachable: an open slot has room and at least one `want` is
            # positive while `remaining` is. Guarded anyway so a future change
            # to the shares cannot turn into a hang.
            return None
        remaining -= moved
    return amounts


def _pack_grouped(
    question_type_counts: Dict[str, int],
    bin_count: int,
) -> Optional[List[Dict[str, int]]]:
    """
    Pack the same request into `bin_count` batches, keeping types together.

    Three passes, in this order, for reasons that are not interchangeable:

      1. **Types too big for one batch, packed tight.** These have no choice but
         to split, so they go first and each chunk is filled to the brim. Tight
         rather than even: filling to capacity leaves the largest possible
         contiguous hole behind, and that hole is what lets a later type stay
         whole. Spreading them evenly instead strands every type that follows.

      2. **Everything else, placed whole, into the tightest hole that fits.**
         Priority types first (see `_priority_order`), then largest first.

      3. **Whatever pass 2 could not place whole, split evenly over the fewest
         batches that can hold it.** Fewest batches keeps the type as grouped as
         it can be; evenly avoids the one-question tail.

    Returns None when the request cannot be packed into `bin_count` batches under
    the per-type caps — the caller falls back to the sequential plan.
    """
    batch_cap = max(1, QUESTION_BATCH_SIZE)
    sizes = {
        type_key: int(question_type_counts.get(type_key) or 0)
        for type_key in _ordered_types(question_type_counts)
    }
    if not sizes or bin_count <= 0:
        return None

    position = {type_key: i for i, type_key in enumerate(sizes)}
    # A type can never exceed the batch cap, whatever its own override says.
    type_cap = {t: min(batch_cap, batch_size_for(t)) for t in sizes}

    bins: List[Dict[str, int]] = [{} for _ in range(bin_count)]
    loads: List[int] = [0] * bin_count

    def room(index: int, type_key: str) -> int:
        """How many more of `type_key` batch `index` can take."""
        return min(
            batch_cap - loads[index],
            type_cap[type_key] - bins[index].get(type_key, 0),
        )

    def place(index: int, type_key: str, count: int) -> None:
        bins[index][type_key] = bins[index].get(type_key, 0) + count
        loads[index] += count

    order = _priority_order(sizes, position)
    must_split = [t for t in order if sizes[t] > type_cap[t]]
    placeable = [t for t in order if t not in must_split]

    # Pass 1 — forced splits, packed tight.
    for type_key in must_split:
        remaining = sizes[type_key]
        while remaining > 0:
            index = max(range(bin_count), key=lambda b: (room(b, type_key), -b))
            available = room(index, type_key)
            if available <= 0:
                return None
            take = min(remaining, available)
            place(index, type_key, take)
            remaining -= take

    # Pass 2 — whole placement, best fit.
    deferred: List[str] = []
    for type_key in placeable:
        needed = sizes[type_key]
        fits = [b for b in range(bin_count) if room(b, type_key) >= needed]
        if not fits:
            deferred.append(type_key)
            continue
        # Tightest hole first, so the roomy batches stay available for the types
        # still to come. Lowest index breaks ties.
        index = min(fits, key=lambda b: (batch_cap - loads[b] - needed, b))
        place(index, type_key, needed)

    # Pass 3 — residual splits, fewest batches, spread evenly.
    for type_key in deferred:
        needed = sizes[type_key]
        ranked = sorted(range(bin_count), key=lambda b: (-room(b, type_key), b))
        chosen: List[int] = []
        capacity = 0
        for index in ranked:
            if capacity >= needed:
                break
            available = room(index, type_key)
            if available <= 0:
                break
            chosen.append(index)
            capacity += available
        if capacity < needed:
            return None
        amounts = _even_split(needed, [room(b, type_key) for b in chosen])
        if amounts is None:
            return None
        for index, count in zip(chosen, amounts):
            if count > 0:
                place(index, type_key, count)

    packed = [b for b in bins if b]
    # Order batches the way the sequential packer would have: by the
    # canonical position of the earliest type each one holds. Purely so the
    # merged payload and the logs read the same as before — but it has to happen
    # HERE, before `plan_batches` slices Bloom's levels positionally against this
    # list. Reordering afterwards silently pairs each batch with another batch's
    # levels.
    packed.sort(key=lambda b: min(position[t] for t in b))
    return packed


def _packing_is_valid(
    packed: List[Dict[str, int]],
    question_type_counts: Dict[str, int],
    bin_count: int,
) -> bool:
    """Every rule `_pack_grouped` is supposed to have obeyed, checked directly."""
    batch_cap = max(1, QUESTION_BATCH_SIZE)
    if len(packed) > bin_count:
        return False

    totals: Counter = Counter()
    for batch in packed:
        if not batch or sum(batch.values()) > batch_cap:
            return False
        for type_key, count in batch.items():
            if count <= 0 or count > min(batch_cap, batch_size_for(type_key)):
                return False
        totals.update(batch)

    requested = {
        k: int(v) for k, v in (question_type_counts or {}).items() if int(v or 0) > 0
    }
    return dict(totals) == requested


def _packing_score(packed: List[Dict[str, int]]) -> tuple:
    """
    How well grouped a plan is. Lower is better, compared lexicographically:
    priority types' spread first, then how many types are split at all, then the
    total amount of splitting.
    """
    spans: Counter = Counter()
    for batch in packed:
        spans.update(batch.keys())
    return (
        tuple(spans.get(t, 0) for t in PRIORITY_TYPES)
        + (sum(1 for v in spans.values() if v > 1),)
        + (sum(v - 1 for v in spans.values()),)
    )


def plan_type_counts(question_type_counts: Dict[str, int]) -> List[Dict[str, int]]:
    """
    Split the requested per-type counts into batches of at most
    `QUESTION_BATCH_SIZE` questions each, keeping each type in as few batches as
    the batch count allows.

    The sequential packing is computed first and used for two things: it fixes
    the number of batches, and it is the floor the grouped plan has to beat. A
    grouped plan is only adopted if it is valid and scores no worse, so this can
    reduce splitting but never introduce it.
    """
    counts = question_type_counts or {}
    baseline = _pack_sequential(counts)
    if len(baseline) <= 1:
        # Nothing to redistribute across.
        return baseline

    grouped = _pack_grouped(counts, len(baseline))
    if grouped is None or not _packing_is_valid(grouped, counts, len(baseline)):
        # Reachable with per-type `BATCH_SIZE_BY_TYPE` overrides tight enough
        # that the request will not fit the minimum batch count once whole types
        # are placed. Logged rather than silent: "grouping was impossible" and
        # "grouping is broken" look identical from the outside otherwise.
        logger.info(
            "Batch grouping found no valid plan for %s in %d batches; "
            "using sequential packing %s.",
            {k: v for k, v in counts.items() if int(v or 0) > 0}, len(baseline), baseline,
        )
        return baseline

    if _packing_score(grouped) > _packing_score(baseline):
        logger.info(
            "Batch grouping did not improve on sequential packing for %s; "
            "keeping %s.",
            {k: v for k, v in counts.items() if int(v or 0) > 0}, baseline,
        )
        return baseline

    if grouped != baseline:
        logger.info("Batch grouping: %s -> %s", baseline, grouped)
    return grouped


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
) -> List[Batch]:
    """
    Build the full batch plan.

    `blooms_by_type` is empty when Bloom's is disabled — that is handled.
    """
    type_counts_per_batch = plan_type_counts(question_type_counts or {})
    if not type_counts_per_batch:
        return []

    blooms_queues: Dict[str, Deque[str]] = {
        type_key: deque(levels)
        for type_key, levels in (blooms_by_type or {}).items()
        if levels
    }

    batches = [
        Batch(
            index=i,
            type_counts=counts,
            blooms_by_type=slice_blooms(blooms_queues, counts),
        )
        for i, counts in enumerate(type_counts_per_batch)
    ]

    _assert_plan_is_complete(batches, question_type_counts, blooms_by_type)
    return batches


def _assert_plan_is_complete(
    batches: List[Batch],
    question_type_counts: Dict[str, int],
    blooms_by_type: Optional[Dict[str, List[str]]],
) -> None:
    """
    Confirm the plan accounts for everything that was requested.

    Cheap and inline rather than a test, because the failure mode it guards
    against — a Bloom's slice that silently drops or repeats a level — produces
    an assessment that looks entirely correct.
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


def _unwrap_questions(result: Any) -> Dict[str, Any]:
    """
    Read the bucket dict out of one batch's response.

    The narrowed batch schema makes the response the bucket object itself, but a
    payload wrapped in `{"questions": {...}}` is accepted too so a model that
    volunteers the full envelope does not merge as empty.
    """
    if not isinstance(result, dict):
        return {}
    inner = result.get("questions")
    if isinstance(inner, dict):
        return inner
    return result


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
