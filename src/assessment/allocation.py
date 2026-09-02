"""
Course-level question allocation for Comprehensive assessments.

A Comprehensive assessment draws from several courses at once. Previously the
only control was a percentage `course_weightage` that was formatted into the
prompt and left to the LLM to honour — nothing computed an integer split and
nothing verified the result. This module makes the split deterministic:

  * `compute_equal_allocation` is the default — the configured total is
    divided equally, with the remainder distributed by a fixed rule so the sum
    always matches.
  * `validate_allocation` is the guarantee behind a user override — the caller's
    own numbers are used, but they must still sum to the configured total.
  * `count_questions_by_course` tallies what the LLM actually produced, so a
    drift between the requested allocation and the generated assessment is
    observable rather than silent, reported as a Configuration Mismatch.

Everything here is pure — no I/O, no LLM, no DB — so the acceptance criteria
are directly unit-testable.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# The `course_name` field the LLM populates on every question, across all five
# canonical buckets. This is the only per-question course attribution we have,
# so it is what allocation-drift verification counts.
COURSE_FIELD = "course_name"


def compute_equal_allocation(total: int, course_ids: List[str]) -> Dict[str, int]:
    """
    The default: distribute `total` questions equally across `course_ids`.

    Where an equal split is not possible the remainder is handed out one
    question at a time to the first `remainder` courses **in selection order**.
    Selection order is used rather than, say, sorted course IDs
    so the result is stable and predictable from the user's point of view — the
    courses they listed first absorb the extras.

    The sum is exact by construction:

        >>> compute_equal_allocation(100, ["a", "b", "c", "d"])
        {'a': 25, 'b': 25, 'c': 25, 'd': 25}
        >>> compute_equal_allocation(100, ["a", "b", "c"])
        {'a': 34, 'b': 33, 'c': 33}
    """
    if total < 0:
        raise ValueError("total questions cannot be negative")
    if not course_ids:
        return {}

    # De-duplicate while preserving order — a repeated course must not receive
    # two shares of the total.
    ordered: List[str] = []
    for cid in course_ids:
        if cid and cid not in ordered:
            ordered.append(cid)
    if not ordered:
        return {}

    base, remainder = divmod(total, len(ordered))
    return {cid: base + (1 if i < remainder else 0) for i, cid in enumerate(ordered)}


def parse_allocation(raw: Union[str, Dict[str, Any], None]) -> Optional[Dict[str, int]]:
    """
    Read a user-supplied allocation from a JSON string or a dict.

    Returns None when nothing usable was supplied — which the caller reads as
    "the user did not define an allocation", the condition for falling back to
    the equal default. A malformed payload also yields None rather than raising,
    so a bad override degrades to the default instead of failing the request;
    the shape is then re-checked by `validate_allocation` anyway.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("string", "{}", "null"):
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Could not parse course_allocation %r as JSON", text)
            return None
    if not isinstance(raw, dict) or not raw:
        return None

    parsed: Dict[str, int] = {}
    for cid, count in raw.items():
        try:
            parsed[str(cid).strip()] = int(count)
        except (TypeError, ValueError):
            logger.warning("Non-integer allocation %r for course %r", count, cid)
            return None
    return parsed or None


def parse_weightage(raw: Union[str, Dict[str, Any], None]) -> Optional[Dict[str, float]]:
    """
    Read a percentage weightage from a JSON string or a dict.

    Separate from `parse_allocation` because the two are different kinds of
    number: an allocation is a count and must be an integer, a weightage is a
    share and is routinely fractional — an equal split across three courses is
    33.33% each. Parsing a weightage with the integer reader would silently
    truncate it.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("string", "{}", "null"):
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Could not parse course_weightage %r as JSON", text)
            return None
    if not isinstance(raw, dict) or not raw:
        return None

    parsed: Dict[str, float] = {}
    for cid, weight in raw.items():
        try:
            parsed[str(cid).strip()] = float(weight)
        except (TypeError, ValueError):
            logger.warning("Non-numeric weightage %r for course %r", weight, cid)
            return None
    return parsed or None


def validate_allocation(
    allocation: Dict[str, int],
    total: int,
    course_ids: Optional[List[str]] = None,
) -> List[str]:
    """
    Check an allocation — default or user-defined — against the configuration.

    Returns a list of human-readable problems; empty means valid. The sum check
    is the one that matters most: a user override is
    honoured, but only if it still adds up to the configured question count.
    """
    errors: List[str] = []
    if not allocation:
        errors.append("Course allocation is empty.")
        return errors

    negative = [cid for cid, n in allocation.items() if n < 0]
    if negative:
        errors.append(f"Negative question count for course(s): {', '.join(sorted(negative))}.")

    if course_ids:
        selected = {cid for cid in course_ids if cid}
        allocated = set(allocation)
        unknown = allocated - selected
        missing = selected - allocated
        if unknown:
            errors.append(f"Allocation refers to unselected course(s): {', '.join(sorted(unknown))}.")
        if missing:
            errors.append(f"No allocation given for selected course(s): {', '.join(sorted(missing))}.")

    actual = sum(allocation.values())
    if actual != total:
        errors.append(
            f"Course allocation sums to {actual} but the configured question count is {total}. "
            "The two must match."
        )
    return errors


def weightage_from_allocation(allocation: Dict[str, int], total: int) -> Dict[str, float]:
    """
    Derive display percentages from the allocation.

    Percentages are presentation only — the counts are authoritative. This is
    the inverse of the previous model, where the percentage was the input and
    the count was whatever the LLM decided.
    """
    if not allocation or total <= 0:
        return {cid: 0.0 for cid in allocation}
    return {cid: round(n * 100 / total, 1) for cid, n in allocation.items()}


def allocation_from_weightage(
    weightage: Union[str, Dict[str, Any], None],
    total: int,
    course_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, int]]:
    """
    Convert a legacy percentage `course_weightage` into integer counts.

    Kept so callers that predate the course-allocation feature — the Postman collections, any existing
    Kong consumer — still get an enforced allocation that sums to the total
    instead of a prompt hint. Uses largest-remainder, the same technique the
    Bloom's split in `generator.compute_blooms_by_type` uses, so the rounding
    behaviour is consistent across the codebase.
    """
    parsed = parse_weightage(weightage)
    if not parsed:
        return None

    if course_ids:
        # Preserve selection order; drop weights for courses that are not selected.
        parsed = {cid: parsed[cid] for cid in course_ids if cid in parsed}
        if not parsed:
            return None

    weight_total = sum(parsed.values())
    if weight_total <= 0:
        return None

    exact = {cid: w * total / weight_total for cid, w in parsed.items()}
    allocation = {cid: int(value) for cid, value in exact.items()}

    # Hand the shortfall to the largest fractional parts, ties broken by the
    # course's position in the selection so the outcome is deterministic.
    order = list(exact)
    shortfall = total - sum(allocation.values())
    ranked = sorted(order, key=lambda cid: (-(exact[cid] - allocation[cid]), order.index(cid)))
    for i in range(shortfall):
        allocation[ranked[i % len(ranked)]] += 1
    return allocation


def count_questions_by_course(assessment: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """
    Tally generated questions by their `course_name`, across every bucket.

    Used to check what the LLM actually produced against what was asked for.
    Questions with no course attribution are counted under the empty
    string so they are visible in the comparison rather than silently dropped.
    """
    counts: Dict[str, int] = {}
    if not isinstance(assessment, dict):
        return counts
    buckets = assessment.get("questions")
    if not isinstance(buckets, dict):
        return counts

    for questions in buckets.values():
        if not isinstance(questions, list):
            continue
        for question in questions:
            if not isinstance(question, dict):
                continue
            name = question.get(COURSE_FIELD)
            key = str(name).strip() if name is not None else ""
            counts[key] = counts.get(key, 0) + 1
    return counts


def compare_allocation(
    requested: Dict[str, int],
    generated: Dict[str, int],
    label_by_id: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Diff a requested allocation against what was generated.

    The requested allocation is keyed by course ID while the LLM labels each
    question with a course *name*, so `label_by_id` supplies the mapping when
    one is known. Courses whose name never made it into the metadata fall back
    to matching on the ID itself.

    Returns `{course: {"requested": n, "generated": m, "delta": m - n}}` for
    every course where the two disagree — empty when the generation matched.
    """
    label_by_id = label_by_id or {}
    drift: Dict[str, Dict[str, int]] = {}

    remaining = dict(generated)
    for cid, want in requested.items():
        label = label_by_id.get(cid) or cid
        got = remaining.pop(label, None)
        if got is None:
            got = remaining.pop(cid, 0)
        if got != want:
            drift[label] = {"requested": want, "generated": got, "delta": got - want}

    # Anything the LLM attributed to a course that was never requested.
    for label, got in remaining.items():
        if got:
            drift[label or "(unattributed)"] = {"requested": 0, "generated": got, "delta": got}
    return drift
