"""
Deterministic assessment blueprint, used on the batched generation path.

Splitting generation into parallel batches leaves nothing able to author the
blueprint: no single call sees the whole assessment, and four independently
written blueprints cannot be merged. Rather than spend an extra LLM call on it,
this module assembles the blueprint in Python — which turns out to be what most
of it wanted anyway.

Of the twelve blueprint fields in resources/schemas.json, only three are read
anywhere in the codebase (`prompt_version`, `api_version` and
`assessment_scope_summary`, all in exporters.py), and two of those were already
substituted from Python before the model ever saw them. `blooms_taxonomy_mapping`
duplicates what `generator.compute_blooms_by_type` computes, and
`smart_learning_objectives` restates the course's own `instructions` list — the
list questions are required to quote verbatim, so the model's rewrite was never
the version questions used.

The one field that genuinely needs judgement is `unified_competency_map`:
deciding which of the 109 KCM competencies apply to a body of course content.
That judgement is left to the model, exactly as it is on the single-call path —
every batch receives the full KCM framework and chooses freely — and the map is
then read back off the competencies the questions actually used. Reporting what
was used rather than what was permitted is what makes the field honest.

Three fields are counted from the questions that were actually generated
(`unified_competency_map`, `blooms_taxonomy_mapping`, `difficulty_distribution`),
so the blueprint describes the assessment rather than predicting it.

Everything here is pure — no I/O, no LLM, no DB. Note that a Python-built
blueprint does not pass through Vertex's `response_schema`, so nothing validates
its shape; it is written to match `full_schema.properties.blueprint` by hand.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from .config import PROMPT_VERSION

logger = logging.getLogger(__name__)

API_VERSION = "api/v1"

# `competency_area` on a question carries the KCM `Type`, which is spelled
# several ways across the 109 entries ("Behavioural", "Behavioral",
# "Behavioural - Core") and is absent on two. Normalised here so grouping into
# the schema's functional/behavioral keys does not depend on the spelling.
_BEHAVIOURAL_PREFIXES = ("behaviour", "behavior")
_FUNCTIONAL_PREFIXES = ("functional",)


def _as_list(value: Any) -> List[str]:
    """Accept the list-or-comma-separated-string shape used throughout the API."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _competency_bucket(competency_area: Any) -> str:
    """Group a question's `competency_area` into the schema's three keys."""
    kind = str(competency_area or "").strip().lower()
    if kind.startswith(_BEHAVIOURAL_PREFIXES):
        return "behavioral"
    if kind.startswith(_FUNCTIONAL_PREFIXES):
        return "functional"
    return "domain"


def _question_kcm(question: Any) -> Optional[Dict[str, Any]]:
    """The `reasoning.competency_alignment.kcm` object off one question, if present."""
    if not isinstance(question, dict):
        return None
    reasoning = question.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    alignment = reasoning.get("competency_alignment")
    if not isinstance(alignment, dict):
        return None
    kcm = alignment.get("kcm")
    return kcm if isinstance(kcm, dict) else None


def competency_map(
    assessment: Optional[Dict[str, Any]] = None,
    topic_names: Optional[Iterable[str]] = None,
) -> Dict[str, List[str]]:
    """
    The schema's `unified_competency_map` — functional / behavioral / domain,
    each a list of strings.

    Read off the competencies the questions actually used rather than a list of
    permitted ones, so the map reports what the assessment covers instead of what
    it was allowed to cover. Every batch is given the full KCM framework and
    chooses freely, exactly as the single call does, so there is no permitted
    list to report in the first place.

    Order follows first appearance in `CANONICAL_BUCKETS` question order, which
    keeps the map stable for identical payloads.

    `domain` is documented as coming from course content rather than KCM; the
    assessment's topics are appended as the closest honest stand-in. The key is
    optional in the schema, so an empty list is valid.
    """
    grouped: Dict[str, List[str]] = {"functional": [], "behavioral": [], "domain": []}

    buckets = (assessment or {}).get("questions")
    if isinstance(buckets, dict):
        for questions in buckets.values():
            if not isinstance(questions, list):
                continue
            for question in questions:
                kcm = _question_kcm(question)
                if not kcm:
                    continue
                sub_theme = str(kcm.get("competency_sub_theme") or "").strip()
                if not sub_theme:
                    continue
                theme = str(kcm.get("competency_theme") or "").strip()
                rendered = f"{theme} > {sub_theme}" if theme else sub_theme
                bucket = _competency_bucket(kcm.get("competency_area"))
                if rendered not in grouped[bucket]:
                    grouped[bucket].append(rendered)

    for topic in _as_list(list(topic_names) if topic_names else None):
        if topic not in grouped["domain"]:
            grouped["domain"].append(topic)

    return grouped


def _count_field(assessment: Optional[Dict[str, Any]], field: str) -> Dict[str, int]:
    """Tally one per-question field across every bucket of a merged payload."""
    counts: Counter = Counter()
    buckets = (assessment or {}).get("questions")
    if not isinstance(buckets, dict):
        return {}
    for questions in buckets.values():
        if not isinstance(questions, list):
            continue
        for question in questions:
            if isinstance(question, dict):
                value = str(question.get(field) or "").strip()
                if value:
                    counts[value] += 1
    return dict(counts)


def _total_questions(assessment: Optional[Dict[str, Any]]) -> int:
    buckets = (assessment or {}).get("questions")
    if not isinstance(buckets, dict):
        return 0
    return sum(len(v) for v in buckets.values() if isinstance(v, list))


def _course_names(aggregated_metadata: Optional[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for course in (aggregated_metadata or {}).get("courses", []) or []:
        if not isinstance(course, dict):
            continue
        name = str(course.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _module_structure(aggregated_metadata: Optional[Dict[str, Any]]) -> str:
    """Modules named in the course metadata, or a statement that none were."""
    parts: List[str] = []
    for course in (aggregated_metadata or {}).get("courses", []) or []:
        if not isinstance(course, dict):
            continue
        name = str(course.get("name") or course.get("identifier") or "").strip()
        children = course.get("children") or course.get("modules") or []
        module_names = [
            str(child.get("name")).strip()
            for child in children
            if isinstance(child, dict) and child.get("name")
        ] if isinstance(children, list) else []
        if name and module_names:
            parts.append(f"{name}: {', '.join(module_names)}")
        elif name:
            parts.append(f"{name}: module structure not declared in course metadata")
    return " | ".join(parts) if parts else "No module structure available in the source metadata."


def _time_validation(time_limit: Optional[int], total: int, difficulty: str) -> str:
    """
    Time appropriateness, stated as the arithmetic rather than a judgement.

    A minute and a half per question is the midpoint of the range the prompt's
    pacing guidance implies; it is used only to describe the configured limit,
    never to change the question count.
    """
    if not time_limit or int(time_limit) <= 0 or total <= 0:
        return (
            f"No time limit was configured for this assessment "
            f"({total} question(s), {difficulty} difficulty); standard pacing applies."
        )
    limit = int(time_limit)
    per_question = round(limit / total, 2)
    expected = round(total * 1.5)
    if per_question < 1:
        verdict = "tight — below one minute per question"
    elif per_question > 3:
        verdict = "generous — over three minutes per question"
    else:
        verdict = "appropriate"
    return (
        f"{limit} minute(s) for {total} question(s) is {per_question} minute(s) "
        f"per question, which is {verdict} at {difficulty} difficulty "
        f"(reference pacing for this volume is approximately {expected} minute(s))."
    )


def _scope_summary(
    assessment_type: str,
    difficulty: str,
    language: str,
    total: int,
    course_names: List[str],
    topics: List[str],
    competency_area: Optional[str],
) -> str:
    courses = ", ".join(course_names) if course_names else "user-provided content"
    summary = (
        f"{str(assessment_type).capitalize()} assessment of {total} question(s) at "
        f"{difficulty} difficulty, authored in {language}, drawn from {courses}."
    )
    if topics:
        summary += f" Prioritised topics: {', '.join(topics)}."
    if competency_area:
        summary += f" Scoped to the {competency_area} competency area."
    return summary


def _type_suitability(question_type_counts: Dict[str, int]) -> str:
    labels = {
        "mcq": "Multiple Choice",
        "ftb": "Fill in the Blank",
        "mtf": "Match the Following",
        "multichoice": "Multi-Choice",
        "truefalse": "True/False",
    }
    parts = [
        f"{labels.get(key, key)}: {int(count)}"
        for key, count in (question_type_counts or {}).items()
        if int(count or 0) > 0
    ]
    if not parts:
        return "No question types were requested."
    return (
        "Question types were selected by the requester and generated in the "
        "configured quantities — " + "; ".join(parts) + "."
    )


def build_blueprint(
    *,
    aggregated_metadata: Optional[Dict[str, Any]],
    assessment: Optional[Dict[str, Any]],
    assessment_type: str,
    difficulty_level: str,
    input_language: str,
    question_type_counts: Optional[Dict[str, int]] = None,
    learning_objectives: Optional[List[str]] = None,
    topic_names: Optional[Iterable[str]] = None,
    time_limit: Optional[int] = None,
    competency_area: Optional[str] = None,
    enable_blooms: bool = True,
) -> Dict[str, Any]:
    """
    Assemble the blueprint for a batched assessment.

    `assessment` is the merged payload, so the three counted fields describe what
    was generated rather than what was intended. Everything else comes from the
    request and the course metadata.
    """
    total = _total_questions(assessment)
    courses = _course_names(aggregated_metadata)
    topics = _as_list(list(topic_names) if topic_names else None)

    blooms_counts = _count_field(assessment, "blooms_level") if enable_blooms else {}
    difficulty_counts = _count_field(assessment, "difficulty_level")

    return {
        "assessment_scope_summary": _scope_summary(
            assessment_type, difficulty_level, input_language,
            total, courses, topics, competency_area,
        ),
        "courses_covered": courses or ["User Uploaded Content"],
        "unified_competency_map": competency_map(assessment, topics),
        "module_structure": _module_structure(aggregated_metadata),
        "smart_learning_objectives": list(learning_objectives or []),
        "blooms_taxonomy_mapping": blooms_counts if enable_blooms else {
            "status": "Bloom's taxonomy mapping was disabled for this assessment.",
        },
        "difficulty_distribution": (
            ", ".join(f"{level}: {count}" for level, count in difficulty_counts.items())
            if difficulty_counts
            else f"All questions generated at the configured {difficulty_level} difficulty."
        ),
        "question_type_suitability": _type_suitability(question_type_counts or {}),
        "evaluation_passing_policy": (
            "No marking scheme or passing threshold was supplied with this "
            "request. Each question carries equal weight; the passing threshold "
            "is set by the publishing authority."
        ),
        "time_appropriateness_validation": _time_validation(
            time_limit, total, str(difficulty_level),
        ),
        "prompt_version": PROMPT_VERSION,
        "api_version": API_VERSION,
    }
