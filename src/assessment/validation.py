"""
Validation gate.

The validation gate blocks saving an invalid question. Nothing reaches the
database until `validate_question` / `validate_assessment` return no errors,
so a failed validation can never leave a half-written assessment behind.

Field-level rules come from the source specification, with two clarifications
applied:
  * Answer-option counts have a floor of two on every save, and a ceiling of
    five that applies only to a question being added. The generation prompt still
    asks for four, but that is a generation target, not a save-time constraint on
    a human reviewer. See `MIN_OPTION_COUNT` / `MAX_OPTION_COUNT_ON_ADD` in
    questions.py for why the ceiling is add-only.
  * An assessment must always contain at least one question.

Errors carry machine-readable facts only — `code`, `field`, `question_id` and a
`params` bag holding every value a message needs to interpolate. They carry no
human-readable sentence: the client owns the copy because it owns the user's
language, and this service serves twelve of them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .questions import (
    BLOOMS_LEVELS,
    MAX_OPTION_COUNT_ON_ADD,
    MIN_OPTION_COUNT,
    BUCKET_FTB,
    BUCKET_MCQ,
    BUCKET_MTF,
    BUCKET_MULTICHOICE,
    BUCKET_TRUEFALSE,
    VALID_PROVENANCE,
    iter_questions_in_order,
    question_count,
)

# Minimum number of left/right pairs for a Match-the-Following question.
MIN_MTF_PAIRS = 2

# --------------------------------------------------------------------------
# Editable field allowlist
# --------------------------------------------------------------------------
# Dotted paths a reviewer is permitted to change. Anything
# outside this set is rejected rather than silently written, which keeps
# server-owned fields — `question_id`, `question_type`, `provenance` — out of
# client control. Changing a question's type is deliberately absent:
# it is out of scope for this release.

_COMMON_EDITABLE = {
    "answer_rationale.correct_answer_explanation",
    "answer_rationale.why_factor",
    "answer_rationale.logic_justification",
    "blooms_level",
    "relevance_percentage",
    # Not named in the change list, but a manually added question has
    # no difficulty unless its author sets one, and the CSV export derives
    # QuestionTagging from it. Editable on both add and edit so AI-generated
    # and human-authored questions behave identically.
    "difficulty_level",
    "reasoning.learning_objective_alignment",        # learning outcome
    "reasoning.competency_alignment.kcm.competency_area",
    "reasoning.competency_alignment.kcm.competency_theme",
    "reasoning.competency_alignment.kcm.competency_sub_theme",
    "reasoning.competency_alignment.domain",
    "course_name",                                   # course mapping
}

EDITABLE_FIELDS: Dict[str, set] = {
    BUCKET_MCQ: _COMMON_EDITABLE | {
        "question_text",
        "options",
        "correct_option_index",
    },
    BUCKET_MULTICHOICE: _COMMON_EDITABLE | {
        "question_text",
        "options",
        "correct_option_index",
    },
    BUCKET_FTB: _COMMON_EDITABLE | {
        "question_text",
        "correct_answer",
    },
    BUCKET_TRUEFALSE: _COMMON_EDITABLE | {
        "question_text",
        "correct_answer",
    },
    BUCKET_MTF: _COMMON_EDITABLE | {
        "matching_context",        # MTF has no question_text of its own
        "pairs",
    },
}

# Fields the server owns; silently ignored on input rather than 400, because
# clients naturally echo back the whole question object they were served.
SERVER_OWNED_FIELDS = {"question_id", "question_type", "provenance", "position",
                       "question_bucket", "question_type_key"}

# Answer-key paths — a change here is the answer-key change (Correct Answer
# Changed) and is recorded as its own audit event.
ANSWER_KEY_FIELDS = {"correct_option_index", "correct_answer", "pairs"}

# Mapping paths — a change here is a mapping update (Mapping Updated).
KCM_AREA_FIELD = "reasoning.competency_alignment.kcm.competency_area"
KCM_THEME_FIELD = "reasoning.competency_alignment.kcm.competency_theme"
KCM_SUB_THEME_FIELD = "reasoning.competency_alignment.kcm.competency_sub_theme"
LEARNING_OUTCOME_FIELD = "reasoning.learning_objective_alignment"

KCM_FIELDS = (KCM_AREA_FIELD, KCM_THEME_FIELD, KCM_SUB_THEME_FIELD)

MAPPING_FIELDS = set(KCM_FIELDS) | {
    LEARNING_OUTCOME_FIELD,
    "reasoning.competency_alignment.domain",
    "course_name",
}


class ValidationError(Exception):
    """Raised when a question or assessment fails the validation gate."""

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


def _err(code: str, field: Optional[str] = None,
         question_id: Optional[str] = None, **params: Any) -> Dict[str, Any]:
    """
    One validation failure, as machine-readable facts only.

    `code` selects the message the client renders, `field` and `question_id`
    locate it, and `params` carries every value that message needs to
    interpolate. So a client can render "must have at least 2 options (found 1)"
    in any language from `{"code": "option_count_invalid", "params":
    {"minimum": 2, "found": 1}}` — without this module knowing English.
    """
    error: Dict[str, Any] = {"code": code, "field": field, "question_id": question_id}
    if params:
        error["params"] = params
    return error


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def set_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------
# Per-question validation
# --------------------------------------------------------------------------

def validate_question(
    bucket: str,
    question: Dict[str, Any],
    *,
    enable_blooms: bool = True,
    edited_paths: Optional[set] = None,
    is_new_question: bool = False,
) -> List[Dict[str, Any]]:
    """
    Return every rule a single question violates. Empty list == valid.

    Covers the five validation limbs: question, answer, option,
    mapping and (via `validate_assessment`) assessment-level validation.
    `edited_paths` scopes the vocabulary half of mapping validation — see
    `validate_mapping`.

    `is_new_question` marks a question this save is *authoring* rather than
    changing, which is the only case the option ceiling applies to — see
    `MAX_OPTION_COUNT_ON_ADD` in questions.py. It defaults to False so an edit
    path that knows nothing about the distinction gets the permissive rule.
    """
    qid = question.get("question_id")
    errors: List[Dict[str, Any]] = []

    # --- text ---------------------------------------------------------
    if bucket == BUCKET_MTF:
        if _is_blank(question.get("matching_context")):
            errors.append(_err("matching_context_required", "matching_context", qid))
    else:
        if _is_blank(question.get("question_text")):
            errors.append(_err("question_text_required", "question_text", qid))

    # --- options and answer key --------------------------------------
    if bucket in (BUCKET_MCQ, BUCKET_MULTICHOICE):
        errors.extend(_validate_options(bucket, question, qid,
                                        enforce_ceiling=is_new_question))
    elif bucket == BUCKET_FTB:
        if _is_blank(question.get("correct_answer")):
            errors.append(_err("correct_answer_required", "correct_answer", qid))
    elif bucket == BUCKET_TRUEFALSE:
        if str(question.get("correct_answer")) not in ("True", "False"):
            errors.append(_err("correct_answer_invalid", "correct_answer", qid,
                               allowed=["True", "False"]))
    elif bucket == BUCKET_MTF:
        errors.extend(_validate_pairs(question, qid))

    # --- rationale -----------------------------------------------------
    if _is_blank(get_path(question, "answer_rationale.correct_answer_explanation")):
        errors.append(_err("rationale_required",
                           "answer_rationale.correct_answer_explanation", qid))

    # --- Bloom's level ---------------------------------------------
    blooms = question.get("blooms_level")
    if enable_blooms and _is_blank(blooms):
        errors.append(_err("blooms_level_required", "blooms_level", qid))
    elif not _is_blank(blooms) and str(blooms).strip().capitalize() not in BLOOMS_LEVELS:
        errors.append(_err("blooms_level_invalid", "blooms_level", qid,
                           allowed=list(BLOOMS_LEVELS), found=blooms))

    # --- relevance -----------------------------------------------------
    relevance = question.get("relevance_percentage")
    if relevance is None:
        errors.append(_err("relevance_required", "relevance_percentage", qid))
    else:
        try:
            value = int(relevance)
            if not 0 <= value <= 100:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(_err("relevance_invalid", "relevance_percentage", qid,
                               minimum=0, maximum=100, found=relevance))

    # --- provenance ------------------------------------------------
    if question.get("provenance") not in VALID_PROVENANCE:
        errors.append(_err("provenance_invalid", "provenance", qid,
                           allowed=sorted(VALID_PROVENANCE),
                           found=question.get("provenance")))

    # --- mapping ---------------------------------------------------
    errors.extend(validate_mapping(question, edited_paths=edited_paths))

    return errors


def _validate_options(bucket: str, question: Dict[str, Any],
                      qid: Optional[str], *,
                      enforce_ceiling: bool = False) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    options = question.get("options")

    if not isinstance(options, list):
        return [_err("options_required", "options", qid)]

    # The floor always applies; the ceiling only when the question is being
    # authored. `ceiling is None` therefore covers both a bucket with no ceiling
    # at all and every edit of an existing question.
    count = len(options)
    ceiling = MAX_OPTION_COUNT_ON_ADD.get(bucket) if enforce_ceiling else None
    if count < MIN_OPTION_COUNT or (ceiling is not None and count > ceiling):
        errors.append(_err(
            "option_count_invalid", "options", qid,
            minimum=MIN_OPTION_COUNT, maximum=ceiling, found=count,
        ))

    indexes: List[int] = []
    for i, option in enumerate(options):
        if not isinstance(option, dict):
            errors.append(_err("option_malformed", f"options.{i}", qid, option_position=i + 1))
            continue
        if _is_blank(option.get("text")):
            errors.append(_err("option_text_required", f"options.{i}.text", qid,
                               option_position=i + 1))
        try:
            indexes.append(int(option["index"]))
        except (KeyError, TypeError, ValueError):
            errors.append(_err("option_index_invalid", f"options.{i}.index", qid,
                               option_position=i + 1))

    if len(set(indexes)) != len(indexes):
        errors.append(_err("option_index_duplicate", "options", qid))

    valid_indexes = set(indexes)
    correct = question.get("correct_option_index")

    if bucket == BUCKET_MCQ:
        if isinstance(correct, list):
            errors.append(_err("correct_option_index_invalid", "correct_option_index", qid,
                               expects="single_index"))
        elif correct is None:
            errors.append(_err("correct_option_index_required", "correct_option_index", qid))
        else:
            try:
                if int(correct) not in valid_indexes:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(_err("correct_option_index_out_of_range",
                                   "correct_option_index", qid,
                                   found=correct, valid_indexes=sorted(valid_indexes)))
    else:  # BUCKET_MULTICHOICE
        if not isinstance(correct, list) or not correct:
            errors.append(_err("correct_option_index_required", "correct_option_index", qid,
                               expects="index_array"))
        else:
            try:
                chosen = {int(x) for x in correct}
            except (TypeError, ValueError):
                errors.append(_err("correct_option_index_invalid", "correct_option_index", qid,
                                   expects="index_array"))
                return errors
            if not chosen <= valid_indexes:
                errors.append(_err("correct_option_index_out_of_range",
                                   "correct_option_index", qid,
                                   found=sorted(chosen),
                                   valid_indexes=sorted(valid_indexes)))
            if len(chosen) != len(correct):
                errors.append(_err("correct_option_index_duplicate",
                                   "correct_option_index", qid))

    return errors


# --------------------------------------------------------------------------
# Mapping validation
# --------------------------------------------------------------------------

def validate_mapping(
    question: Dict[str, Any],
    *,
    edited_paths: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Mapping validation — "Mapping validation must run before save", and the
    mapping-validation limb of the validation gate.

    Structural rules only: a mapping field cannot be blanked out once edited,
    and the KCM triple is all-or-nothing, because a partial area/theme/
    sub-theme is not a usable mapping. The triple's values are free text and
    are not checked against resources/competencies.json — a reviewer (or the
    generator) may record any area/theme/sub-theme label.
    """
    qid = question.get("question_id")
    errors: List[Dict[str, Any]] = []
    edited = edited_paths or set()

    kcm = get_path(question, "reasoning.competency_alignment.kcm") or {}
    area = str(kcm.get("competency_area") or "").strip()
    theme = str(kcm.get("competency_theme") or "").strip()
    sub_theme = str(kcm.get("competency_sub_theme") or "").strip()

    # --- structural ---------------------------------------------------
    for path in (LEARNING_OUTCOME_FIELD, "course_name"):
        if path in edited and _is_blank(get_path(question, path)):
            errors.append(_err("mapping_required", path, qid))

    present = [bool(area), bool(theme), bool(sub_theme)]
    if any(present) and not all(present):
        errors.append(_err("competency_mapping_incomplete",
                           "reasoning.competency_alignment.kcm", qid,
                           missing=[
                               name for name, ok in (
                                   ("competency_area", bool(area)),
                                   ("competency_theme", bool(theme)),
                                   ("competency_sub_theme", bool(sub_theme)),
                               ) if not ok
                           ]))

    return errors


def _validate_pairs(question: Dict[str, Any], qid: Optional[str]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    pairs = question.get("pairs")
    if not isinstance(pairs, list):
        return [_err("pairs_required", "pairs", qid)]
    if len(pairs) < MIN_MTF_PAIRS:
        errors.append(_err("pair_count_invalid", "pairs", qid,
                           minimum=MIN_MTF_PAIRS, found=len(pairs)))
    for i, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            errors.append(_err("pair_malformed", f"pairs.{i}", qid, pair_position=i + 1))
            continue
        if _is_blank(pair.get("left")):
            errors.append(_err("pair_left_required", f"pairs.{i}.left", qid,
                               pair_position=i + 1))
        if _is_blank(pair.get("right")):
            errors.append(_err("pair_right_required", f"pairs.{i}.right", qid,
                               pair_position=i + 1))
    return errors


# --------------------------------------------------------------------------
# Assessment-level validation
# --------------------------------------------------------------------------

def validate_assessment(
    assessment_data: Dict[str, Any],
    *,
    enable_blooms: bool = True,
    only_question_ids: Optional[set] = None,
    edited_paths_by_question: Optional[Dict[str, set]] = None,
    new_question_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Validate the assessment-level invariants, plus the field-level rules for
    each question. Used by the whole-blob `PUT /update/{job_id}` path, where the
    client may have changed anything at all.

    `only_question_ids` restricts the per-question rules to the questions this
    save actually adds or changes. Assessment-level invariants — at least one
    question, unique identifiers — are always checked.

    `new_question_ids` names the questions this save introduces, so the option
    ceiling reaches a question added through the whole-blob path exactly as it
    reaches one added through `POST /questions/create`. Questions the assessment
    already held are validated without it.

    The restriction matters for existing data. `blooms_level`, for instance, was
    never a required field in resources/schemas.json, so an assessment generated
    before this release can legitimately hold a question without one. Validating
    the whole payload on every save would reject an edit to question 5 because
    question 12 has a pre-existing gap, leaving the reviewer unable to edit the
    assessment at all. The validation gate blocks saving an *invalid question*,
    so the gate applies to the questions being written.
    """
    errors: List[Dict[str, Any]] = []

    if question_count(assessment_data) < 1:
        errors.append(_err("assessment_empty", "questions"))

    seen: set = set()
    for bucket, question in iter_questions_in_order(assessment_data):
        qid = str(question.get("question_id") or "")
        if not qid:
            errors.append(_err("question_id_required", "question_id"))
        elif qid in seen:
            errors.append(_err("question_id_duplicate", "question_id", qid))
        else:
            seen.add(qid)

        if only_question_ids is not None and qid not in only_question_ids:
            continue
        errors.extend(validate_question(
            bucket, question, enable_blooms=enable_blooms,
            edited_paths=(edited_paths_by_question or {}).get(qid),
            is_new_question=qid in (new_question_ids or set()),
        ))

    return errors


def validate_editable_fields(bucket: str, updates: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Reject dotted paths that are not editable for this question type. Keeps
    server-owned fields (`provenance`, `question_id`, ...) out of client reach.
    """
    allowed = EDITABLE_FIELDS.get(bucket, set())
    return [
        _err("field_not_editable", path, None,
             question_bucket=bucket, editable_fields=sorted(allowed))
        for path in updates
        if path not in allowed and path not in SERVER_OWNED_FIELDS
    ]


# --------------------------------------------------------------------------
# Option-change classification
# --------------------------------------------------------------------------
# Retained server-side because the **audit trail** records it: an
# `options_reordered` / `reindexed_only` flag on the Question Edit Saved and
# Correct Answer Changed events (see editing.py). It is not presentation.

def _option_texts(options: Any) -> Optional[Dict[int, str]]:
    """`{index: text}` for a well-formed option list, or None if it is not one."""
    if not isinstance(options, list):
        return None
    texts: Dict[int, str] = {}
    for i, option in enumerate(options):
        if not isinstance(option, dict):
            return None
        try:
            texts[int(option.get("index", i))] = str(option.get("text") or "")
        except (TypeError, ValueError):
            return None
    return texts if len(texts) == len(options) else None


def _answer_indexes(correct: Any) -> Optional[set]:
    """The answer key as a set of integer indexes, for MCQ and Multi-Choice alike."""
    if correct is None:
        return None
    values = correct if isinstance(correct, list) else [correct]
    try:
        return {int(v) for v in values}
    except (TypeError, ValueError):
        return None


def classify_option_change(
    changed_fields: List[Dict[str, Any]],
) -> Tuple[bool, bool]:
    """
    Tell a pure re-sequencing of the options apart from a genuine content edit.

    Reordering the options renumbers them, so `correct_option_index` changes on
    every reorder even though the option it points at does not. Recorded as-is
    the audit trail would read as a reviewer changing the answer key, which is
    the one thing an audit of an assessment must not get wrong.

    Returns `(options_reordered, answer_reindexed)`:
      * `options_reordered` — the same option texts, in a different order
      * `answer_reindexed`  — the answer key moved only to follow that reorder;
                              the correct option's wording is unchanged
    """
    change = next((c for c in changed_fields if c["field"] == "options"), None)
    if change is None:
        return False, False

    previous = _option_texts(change["previous_value"])
    current = _option_texts(change["new_value"])
    if previous is None or current is None:
        return False, False
    # An added, removed or reworded option is a content change, not a reorder.
    if sorted(previous.values()) != sorted(current.values()):
        return False, False
    # Same texts already in the same index sequence — renumbering only.
    if ([previous[i] for i in sorted(previous)]
            == [current[i] for i in sorted(current)]):
        return False, False

    answer = next(
        (c for c in changed_fields if c["field"] == "correct_option_index"), None)
    if answer is None:
        return True, False

    before_key = _answer_indexes(answer["previous_value"])
    after_key = _answer_indexes(answer["new_value"])
    if (before_key is None or after_key is None
            or len(before_key) != len(after_key)
            or not before_key <= set(previous) or not after_key <= set(current)):
        return True, False

    reindexed = (sorted(previous[i] for i in before_key)
                 == sorted(current[i] for i in after_key))
    return True, reindexed
