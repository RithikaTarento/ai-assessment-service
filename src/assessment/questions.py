"""
Question-level model helpers for the editable assessment.

The LLM returns questions grouped into type buckets:

    assessment_data = {
        "blueprint": {...},
        "questions": {
            "Multiple Choice Question": [...],
            "FTB Question": [...],
            ...
        }
    }

That shape has no notion of a single authoritative question sequence, which
reordering, persisting order, and ordered downloads all need. Rather than
break every existing consumer by flattening the buckets, we keep them and
add a top-level ordering array:

    assessment_data["question_order"] = ["q_abc123", "q_def456", ...]

`question_order` is the authoritative sequence. The buckets remain the
authoritative store of question content.

Every question also carries:
  * `question_id`   — unique within the assessment
  * `provenance`    — ai_generated | ai_assisted | human_authored

`normalize_assessment()` is idempotent and backfills both on legacy rows that
predate these fields, so no database migration of `assessment_data` is needed.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import NORMALIZE_OPTION_INDEX_BASE

# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------

BUCKET_MCQ = "Multiple Choice Question"
BUCKET_FTB = "FTB Question"
BUCKET_MTF = "MTF Question"
BUCKET_MULTICHOICE = "Multi-Choice Question"
BUCKET_TRUEFALSE = "True/False Question"

# Same order as `questions.required` in resources/schemas.json. Used to lay out
# the bucket dict and as a last-resort ordering; it is deliberately NOT the
# fallback used to backfill `question_order` on legacy rows — see pass 2 of
# `normalize_assessment` for why that follows the stored payload's own order.
CANONICAL_BUCKETS: List[str] = [
    BUCKET_MCQ,
    BUCKET_FTB,
    BUCKET_MTF,
    BUCKET_MULTICHOICE,
    BUCKET_TRUEFALSE,
]

# Short type key (the `question_type_counts` / API vocabulary) -> bucket.
BUCKET_BY_TYPE_KEY: Dict[str, str] = {
    "mcq": BUCKET_MCQ,
    "ftb": BUCKET_FTB,
    "mtf": BUCKET_MTF,
    "multichoice": BUCKET_MULTICHOICE,
    "truefalse": BUCKET_TRUEFALSE,
}
TYPE_KEY_BY_BUCKET: Dict[str, str] = {v: k for k, v in BUCKET_BY_TYPE_KEY.items()}

# The `question_type` const each bucket carries inside the question object
# (see resources/schemas.json).
QUESTION_TYPE_CONST_BY_BUCKET: Dict[str, str] = {
    BUCKET_MCQ: "MCQ",
    BUCKET_FTB: "FTB",
    BUCKET_MTF: "MTF",
    BUCKET_MULTICHOICE: "MULTICHOICE",
    BUCKET_TRUEFALSE: "TRUEFALSE",
}

# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

PROV_AI_GENERATED = "ai_generated"
PROV_AI_ASSISTED = "ai_assisted"
PROV_HUMAN_AUTHORED = "human_authored"

VALID_PROVENANCE = {PROV_AI_GENERATED, PROV_AI_ASSISTED, PROV_HUMAN_AUTHORED}

BLOOMS_LEVELS = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]

# Answer-option counts. The floor and the ceiling have deliberately different
# scopes.
#
# The floor applies to every save. A question with fewer than two options is not
# answerable, so it is rejected wherever it came from. This is looser than the
# generation prompt (resources/prompts.yaml), which asks for "4 options" on an
# MCQ and "4 or more" on a MULTICHOICE and is left untouched — the prompt is a
# generation target, not a save-time rule, and enforcing its count made a
# legitimate 2- or 3-option question impossible to save.
MIN_OPTION_COUNT = 2

# The ceiling applies only when a question is *authored* — `apply_question_add`,
# and a question the whole-blob update introduces. Per bucket; None means no
# ceiling even on add.
#
# Editing has no ceiling, and that asymmetry is the point. The MULTICHOICE prompt
# sets no upper bound, so a generated question can legitimately carry six or more
# options; a ceiling on the edit path would reject *every* save of such a
# question — however unrelated the change — over an option count the reviewer
# never chose, leaving it permanently uneditable. A question being authored from
# scratch has no such history, so the ceiling is a fair constraint there.
MAX_OPTION_COUNT_ON_ADD: Dict[str, Optional[int]] = {
    BUCKET_MCQ: 5,
    BUCKET_MULTICHOICE: 5,
}

# The types whose answer options are editable as options. These live
# here rather than in validation.py so the ordered-question projection can
# report the add/remove affordance state without importing validation.
OPTION_BUCKETS = (BUCKET_MCQ, BUCKET_MULTICHOICE)


def resolve_bucket(name: str) -> Optional[str]:
    """Accept a bucket name, a short type key, or a question_type const."""
    if not name:
        return None
    if name in CANONICAL_BUCKETS:
        return name
    key = name.strip().lower()
    if key in BUCKET_BY_TYPE_KEY:
        return BUCKET_BY_TYPE_KEY[key]
    for bucket, const in QUESTION_TYPE_CONST_BY_BUCKET.items():
        if const.lower() == key:
            return bucket
    return None


def new_question_id() -> str:
    """Unique identifier for a manually added question."""
    return f"q_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

# The one convention for MCQ / Multi-Choice option indexes: the first option is
# `index` 0. Stated to the model in resources/prompts.yaml (OPTION INDEXING) and
# in the `description` of both fields in resources/schemas.json, assumed by the
# editing UI, and enforced on fresh LLM output by `_rebase_option_indexes`.
OPTION_INDEX_BASE = 0


def _fill_missing_option_indexes(
    options: List[Any], missing: List[int], known: set,
) -> None:
    """
    Give every option that arrived without a usable `index` one that does not
    collide with its siblings.

    The historical fill was the option's array position. That is right for a
    zero-based question and wrong for a one-based one — position 1 of options
    numbered 1..n is already taken, and the duplicate makes the whole question
    unsaveable through the validation gate.

    So: try the positional fill first and keep it whenever it is collision-free,
    which is every case the old fill already handled. Only when it collides is
    the run one-based, and the fill shifts up by one to match. The result is
    identical to the old behaviour wherever the old behaviour produced a valid
    question, so no assessment that could be edited before can fail to be now.
    """
    for offset in (0, 1):
        proposed = {i: i + offset for i in missing}
        values = set(proposed.values())
        if len(values) == len(missing) and not (values & known):
            break
    else:
        # Neither scale resolves it — an option list too damaged for any fill to
        # rescue. Keep the historical answer and let validation report it.
        proposed = {i: i for i in missing}

    for position, index in proposed.items():
        options[position]["index"] = index


def _rebase_option_indexes(question: Dict[str, Any]) -> None:
    """
    Bring a one-based question onto the zero-based convention (`OPTION_INDEX_BASE`).

    Applied to fresh LLM output only, at the single ingest point in
    worker_service.py — never to an assessment already in the database. See
    `normalize_assessment`'s `rebase_option_indexes` argument for why.

    Identity-preserving by construction: the option indexes and the answer key
    shift together, so the same option stays correct. Applied only when the
    question is provably one-based — a clean 1..n run whose whole answer key
    falls inside it. Anything ambiguous (a gap, a stray index, an answer key
    already out of range) is left untouched rather than guessed at.

    Idempotent: a rebased question is 0..n-1 and no longer matches.
    """
    if not NORMALIZE_OPTION_INDEX_BASE:
        return
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return
    if not all(isinstance(o, dict) and type(o.get("index")) is int for o in options):
        return

    indexes = sorted(o["index"] for o in options)
    if indexes != list(range(1, len(options) + 1)):
        return

    correct = question.get("correct_option_index")
    raw = correct if isinstance(correct, list) else ([] if correct is None else [correct])
    try:
        keys = [int(v) for v in raw]
    except (TypeError, ValueError):
        return
    # An answer key that already points at no option is broken; shifting it would
    # only move it to a different wrong place.
    if any(k not in indexes for k in keys):
        return

    for option in options:
        option["index"] -= 1
    if isinstance(correct, list):
        question["correct_option_index"] = [k - 1 for k in keys]
    elif keys:
        question["correct_option_index"] = keys[0] - 1


def _normalize_option_indexes(question: Dict[str, Any], *, rebase: bool = False) -> None:
    """
    Guarantee every option carries an integer `index` (required by
    resources/schemas.json).

    `correct_option_index` is matched against these values by every exporter.
    When `index` was missing the exporters disagreed on the fallback — PDF and
    DOCX assumed zero-based, CSV assumed one-based — which could mark different
    options correct in different download formats for the same question.
    Filling the field in here removes the fallback path entirely, so all
    formats reflect the same final state.

    `rebase` additionally puts a one-based question on the zero-based
    convention. Off everywhere except the ingest point — see
    `normalize_assessment`.
    """
    options = question.get("options")
    if not isinstance(options, list):
        return

    missing: List[int] = []
    known: set = set()
    for i, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        try:
            option["index"] = int(option["index"])
        except (KeyError, TypeError, ValueError):
            missing.append(i)
        else:
            known.add(option["index"])
    if missing:
        _fill_missing_option_indexes(options, missing, known)

    if rebase:
        _rebase_option_indexes(question)


# Spellings accepted for a True/False answer, mapped to the canonical form.
_TRUE_FALSE_CANON = {
    "true": "True", "t": "True", "yes": "True", "y": "True",
    "false": "False", "f": "False", "no": "False", "n": "False",
}


def _normalize_true_false(question: Dict[str, Any]) -> None:
    """
    Canonicalize a True/False `correct_answer` to the "True"/"False" spelling
    that resources/schemas.json's enum and validation.py both require.

    The generation prompt states no convention for this field, and the enum is
    only enforced by the model's structured output — so a stored answer may be a
    JSON boolean or a lower-cased string. Repairing it here rather than relaxing
    the validation rule keeps one spelling in the exports and in the answer key,
    and the repair persists on the first save like every other normalization.
    """
    value = question.get("correct_answer")
    if isinstance(value, bool):
        question["correct_answer"] = "True" if value else "False"
    elif isinstance(value, str):
        canonical = _TRUE_FALSE_CANON.get(value.strip().lower())
        if canonical:
            question["correct_answer"] = canonical


def normalize_assessment(
    assessment_data: Optional[Dict[str, Any]],
    *,
    default_provenance: str = PROV_AI_GENERATED,
    in_place: bool = False,
    rebase_option_indexes: bool = False,
) -> Dict[str, Any]:
    """
    Bring an assessment payload up to the editable model.

    Idempotent. Guarantees, on return:
      * `questions` holds all five canonical buckets, in canonical key order
      * every question has a unique non-empty `question_id`
      * every question has a valid `provenance`
      * every question has the `question_type` const for its bucket
      * every True/False `correct_answer` is spelled "True" or "False"
      * `question_order` lists exactly the ids present, with no duplicates

    Legacy rows (no `question_id`, no `question_order`) are backfilled following
    the order the stored payload presents its buckets in — the same order the
    exporters used previously — so existing downloads keep the numbering
    they have always had.

    `rebase_option_indexes` additionally moves a one-based MCQ / Multi-Choice
    question onto the zero-based convention. It defaults to **off** and is passed
    only by worker_service.py, on fresh LLM output before the first store.

    It is deliberately not applied to stored assessments. The rebase itself is
    identity-preserving, but a client that read a question before the rebase and
    writes it back after would be sending indexes on the other scale: the edit
    path diffs the incoming payload against the stored question, so a stale
    one-based `correct_option_index` would land on a different option. Confining
    the rebase to ingest means an assessment's indexes never change base after it
    is created, so no client can ever hold a snapshot on the wrong scale.
    Assessments generated before prompt version 4.3 therefore keep whatever base
    they were generated on — which is harmless, because every reader matches
    `correct_option_index` against each option's own `index` value.
    """
    data = assessment_data if in_place else copy.deepcopy(assessment_data or {})
    if not isinstance(data, dict):
        return {"blueprint": {}, "questions": {b: [] for b in CANONICAL_BUCKETS}, "question_order": []}

    raw_questions = data.get("questions")
    if not isinstance(raw_questions, dict):
        raw_questions = {}

    # Rebuild the bucket dict in canonical order, preserving any unexpected
    # extra buckets at the end rather than dropping data on the floor.
    questions: Dict[str, List[Dict[str, Any]]] = {}
    for bucket in CANONICAL_BUCKETS:
        value = raw_questions.get(bucket)
        questions[bucket] = [q for q in value if isinstance(q, dict)] if isinstance(value, list) else []
    for bucket, value in raw_questions.items():
        if bucket not in questions:
            questions[bucket] = [q for q in value if isinstance(q, dict)] if isinstance(value, list) else []

    if default_provenance not in VALID_PROVENANCE:
        default_provenance = PROV_AI_GENERATED

    # Pass 1 — assign ids. Keep whatever the LLM produced when it is usable and
    # unique; otherwise mint a positional id, de-duplicating against ids already
    # taken so a legacy `q_001` from the LLM cannot collide with a backfilled one.
    seen_ids: set[str] = set()
    for bucket in questions:
        for position, question in enumerate(questions[bucket], start=1):
            existing = question.get("question_id")
            candidate = str(existing).strip() if existing not in (None, "") else ""
            if not candidate or candidate in seen_ids:
                base = candidate or f"{TYPE_KEY_BY_BUCKET.get(bucket, 'q')}_{position:03d}"
                candidate = base
                suffix = 2
                while candidate in seen_ids:
                    candidate = f"{base}_{suffix}"
                    suffix += 1
            seen_ids.add(candidate)
            question["question_id"] = candidate

            if question.get("provenance") not in VALID_PROVENANCE:
                question["provenance"] = default_provenance

            const = QUESTION_TYPE_CONST_BY_BUCKET.get(bucket)
            if const and question.get("question_type") != const:
                question["question_type"] = const

            _normalize_option_indexes(question, rebase=rebase_option_indexes)

            if bucket == BUCKET_TRUEFALSE:
                _normalize_true_false(question)

    # Pass 2 — repair `question_order`: keep the recorded sequence for ids that
    # still exist, drop stale ids, append anything new.
    #
    # Anything not already in the recorded order is appended following the order
    # the payload itself presents its buckets in — deliberately NOT
    # `CANONICAL_BUCKETS`. For a legacy row that has no `question_order`, the
    # payload's own order is exactly what the exporters iterated before this
    # field existed, so backfilling from it leaves existing PDF/DOCX/CSV
    # downloads numbered as they have always been.
    #
    # This distinction matters because `assessment_data` is a jsonb column, and
    # jsonb does not preserve insertion order — Postgres returns object keys
    # sorted by length then bytewise. So a stored assessment always comes back
    # as FTB, MTF, True/False, Multi-Choice, Multiple Choice, which is not the
    # canonical order. Backfilling canonically would renumber every question in
    # every download of every assessment saved before `question_order` existed.
    #
    # Freshly generated assessments are unaffected: the worker normalizes the
    # LLM response directly, while it is still an insertion-ordered Python dict
    # following the schema's bucket order, and persists `question_order` at that
    # point — so they keep the canonical sequence and never reach this fallback
    # again.
    recorded = data.get("question_order")
    order: List[str] = []
    if isinstance(recorded, list):
        for qid in recorded:
            qid = str(qid)
            if qid in seen_ids and qid not in order:
                order.append(qid)
    backfill_buckets = list(raw_questions) or CANONICAL_BUCKETS
    for bucket in backfill_buckets + [b for b in questions if b not in backfill_buckets]:
        for question in questions.get(bucket, []):
            qid = question["question_id"]
            if qid not in order:
                order.append(qid)

    data["questions"] = questions
    data["question_order"] = order
    if "blueprint" not in data or not isinstance(data.get("blueprint"), dict):
        data["blueprint"] = data.get("blueprint") if isinstance(data.get("blueprint"), dict) else {}
    return data


# --------------------------------------------------------------------------
# Lookup / iteration
# --------------------------------------------------------------------------

def index_questions(assessment_data: Dict[str, Any]) -> Dict[str, Tuple[str, int, Dict[str, Any]]]:
    """question_id -> (bucket, index within bucket, question object)."""
    index: Dict[str, Tuple[str, int, Dict[str, Any]]] = {}
    for bucket, q_list in (assessment_data.get("questions") or {}).items():
        if not isinstance(q_list, list):
            continue
        for i, question in enumerate(q_list):
            if isinstance(question, dict) and question.get("question_id"):
                index[str(question["question_id"])] = (bucket, i, question)
    return index


def find_question(
    assessment_data: Dict[str, Any], question_id: str
) -> Optional[Tuple[str, int, Dict[str, Any]]]:
    return index_questions(assessment_data).get(str(question_id))


def iter_questions_in_order(
    assessment_data: Dict[str, Any],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """
    Yield `(bucket, question)` following `question_order` — the authoritative
    sequence. Any question missing from the order array is yielded last so
    nothing is ever silently dropped from an export.
    """
    index = index_questions(assessment_data)
    emitted: set[str] = set()
    for qid in assessment_data.get("question_order") or []:
        entry = index.get(str(qid))
        if entry and str(qid) not in emitted:
            emitted.add(str(qid))
            yield entry[0], entry[2]
    for bucket in CANONICAL_BUCKETS + [
        b for b in (assessment_data.get("questions") or {}) if b not in CANONICAL_BUCKETS
    ]:
        for question in (assessment_data.get("questions") or {}).get(bucket) or []:
            qid = str(question.get("question_id"))
            if qid not in emitted:
                emitted.add(qid)
                yield bucket, question


def ordered_questions(assessment_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flat, 1-based, position-annotated view of the assessment — what the editing
    UI renders and what `GET /questions/list/{job_id}` returns.
    """
    out: List[Dict[str, Any]] = []
    total = question_count(assessment_data)
    for position, (bucket, question) in enumerate(iter_questions_in_order(assessment_data), start=1):
        item = copy.deepcopy(question)
        item["position"] = position
        item["question_bucket"] = bucket
        item["question_type_key"] = TYPE_KEY_BY_BUCKET.get(bucket, bucket)

        # Affordance state, so the client can disable controls rather than
        # discovering the rule from a rejected save.
        options = question.get("options")
        option_count = len(options) if isinstance(options, list) else None
        item["option_count"] = option_count
        if bucket in OPTION_BUCKETS and option_count is not None:
            # These describe the *edit* path, which this projection feeds, so
            # they mirror the floor only. `MAX_OPTION_COUNT_ON_ADD` is not
            # consulted: it constrains authoring a new question, and reporting it
            # here would disable a control the edit endpoint accepts.
            item["can_add_option"] = True
            item["can_remove_option"] = option_count > MIN_OPTION_COUNT
        else:
            item["can_add_option"] = False
            item["can_remove_option"] = False
        # The last remaining question cannot be deleted.
        item["can_delete"] = total > 1

        out.append(item)
    return out


def question_count(assessment_data: Dict[str, Any]) -> int:
    return sum(
        len(q_list)
        for q_list in (assessment_data.get("questions") or {}).values()
        if isinstance(q_list, list)
    )


def position_of(assessment_data: Dict[str, Any], question_id: str) -> Optional[int]:
    """1-based position in the authoritative sequence, or None if absent."""
    order = assessment_data.get("question_order") or []
    try:
        return list(map(str, order)).index(str(question_id)) + 1
    except ValueError:
        return None


def question_type_key_of(assessment_data: Dict[str, Any], question_id: str) -> Optional[str]:
    entry = find_question(assessment_data, question_id)
    if not entry:
        return None
    return TYPE_KEY_BY_BUCKET.get(entry[0], entry[0])
