"""
Editing operations on a saved assessment — Groups A, B and C.

Each operation is a pure function: it takes the current `assessment_data` and
returns a new copy plus the audit rows the change produced. Nothing here talks
to the database or to FastAPI, so the validation gate, the provenance rules and
the audit content are all testable in isolation and are identical no matter
which endpoint drove the change.

Operations:
  * `apply_question_edit`
  * `apply_question_add`
  * `apply_question_delete`
  * `apply_question_reorder`
  * `diff_assessments`      whole-blob PUT support — derives the same audit
                            rows from a before/after comparison

Changing a question's type in place is out of scope for this release and is
not implemented; a type change is rejected by the field allowlist.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import telemetry
from .questions import (
    PROV_AI_ASSISTED,
    PROV_AI_GENERATED,
    PROV_HUMAN_AUTHORED,
    QUESTION_TYPE_CONST_BY_BUCKET,
    TYPE_KEY_BY_BUCKET,
    find_question,
    index_questions,
    new_question_id,
    normalize_assessment,
    position_of,
    question_count,
    resolve_bucket,
)
from .validation import (
    ANSWER_KEY_FIELDS,
    EDITABLE_FIELDS,
    MAPPING_FIELDS,
    SERVER_OWNED_FIELDS,
    ValidationError,
    build_change_alerts,
    build_delete_alerts,
    classify_option_change,
    get_path,
    set_path,
    validate_editable_fields,
    validate_question,
)


@dataclass
class EditResult:
    """Outcome of one editing operation."""

    assessment_data: Dict[str, Any]
    # Every telemetry event this operation produced, in emission order.
    events: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    question: Optional[Dict[str, Any]] = None
    announcement: Optional[str] = None

    @property
    def audit_rows(self) -> List[Dict[str, Any]]:
        """
        The subset of events that is persisted to the audit trail — exactly
        six feeds: Question Edit Saved, Question Added, Question Deleted,
        Question Reordered, Correct Answer Changed and Mapping Updated — so
        Option Added and Option Deleted are observability-only.
        """
        return [e for e in self.events if e["event_code"] in telemetry.AUDITED_EVENTS]

    @property
    def changed(self) -> bool:
        return bool(self.events)


def _event(
    event_code: str,
    *,
    editor_id: str,
    question_id: Optional[str] = None,
    question_type: Optional[str] = None,
    previous_position: Optional[int] = None,
    new_position: Optional[int] = None,
    changed_fields: Optional[List[Dict[str, Any]]] = None,
    original_question: Optional[Dict[str, Any]] = None,
    question_snapshot: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "event_code": event_code,
        "event_name": telemetry.EVENT_NAMES.get(event_code, event_code),
        "editor_id": editor_id,
        "question_id": question_id,
        "question_type": question_type,
        "previous_position": previous_position,
        "new_position": new_position,
        "changed_fields": changed_fields,
        "original_question": original_question,
        "question_snapshot": question_snapshot,
        "details": details,
    }


def _raise_if_invalid(errors: List[Dict[str, Any]]) -> None:
    """A question that fails validation is never saved."""
    if errors:
        raise ValidationError(errors)


def _original_ai_question(
    ai_original_data: Optional[Dict[str, Any]], question_id: str
) -> Optional[Dict[str, Any]]:
    """
    The pristine AI-generated version of a question, for the audit record.
    Returns None for human-authored questions, which have no AI original by
    definition.
    """
    if not isinstance(ai_original_data, dict):
        return None
    entry = find_question(ai_original_data, question_id)
    return copy.deepcopy(entry[2]) if entry else None


# --------------------------------------------------------------------------
# Edit an existing question in place
# --------------------------------------------------------------------------

def apply_question_edit(
    assessment_data: Dict[str, Any],
    question_id: str,
    updates: Dict[str, Any],
    *,
    editor_id: str,
    enable_blooms: bool = True,
    ai_original_data: Optional[Dict[str, Any]] = None,
) -> EditResult:
    """
    Apply field-level updates to one question.

    `updates` is keyed by dotted path, e.g.::

        {
            "question_text": "Revised stem?",
            "correct_option_index": 2,
            "reasoning.competency_alignment.kcm.competency_theme": "Integrity",
        }

    Only paths in the allowlist are accepted; anything else is a
    validation error rather than a silent write. A no-op edit (every submitted
    value already equal to the stored one) produces no audit row and no version
    bump.

    Raises `ValidationError` if the resulting question would be invalid
    or if `question_id` does not exist.
    """
    data = normalize_assessment(assessment_data)
    entry = find_question(data, question_id)
    if entry is None:
        raise ValidationError([{
            "code": "question_not_found",
            "message": f"No question with id '{question_id}' in this assessment.",
            "field": "question_id", "question_id": question_id,
        }])

    bucket, _, question = entry
    before = copy.deepcopy(question)

    _raise_if_invalid(validate_editable_fields(bucket, updates))

    # Build the diff first so a no-op save is distinguishable from a real one.
    changed_fields: List[Dict[str, Any]] = []
    for path, new_value in updates.items():
        if path in SERVER_OWNED_FIELDS:
            continue  # clients echo these back; never client-controlled
        old_value = get_path(question, path)
        if old_value != new_value:
            changed_fields.append({
                "field": path,
                "previous_value": copy.deepcopy(old_value),
                "new_value": copy.deepcopy(new_value),
            })

    if not changed_fields:
        return EditResult(assessment_data=data, question=copy.deepcopy(question))

    for change in changed_fields:
        set_path(question, change["field"], change["new_value"])

    # An edited AI-generated question becomes AI-assisted.
    # A human-authored question stays human-authored no matter how often it is
    # edited, and an already AI-assisted question does not change again.
    provenance_before = before.get("provenance")
    if provenance_before == PROV_AI_GENERATED:
        question["provenance"] = PROV_AI_ASSISTED

    # Option indexes may have been rewritten by the edit; re-normalize before
    # validating so `correct_option_index` is checked against final values.
    data = normalize_assessment(data, in_place=True)
    _, _, question = find_question(data, question_id)
    edited_paths = {c["field"] for c in changed_fields}
    _raise_if_invalid(validate_question(
        bucket, question, enable_blooms=enable_blooms, edited_paths=edited_paths,
    ))

    original = None
    if provenance_before == PROV_AI_GENERATED:
        # First human touch — capture the AI original alongside the change.
        original = _original_ai_question(ai_original_data, question_id) or before

    q_type_key = TYPE_KEY_BY_BUCKET.get(bucket, bucket)
    position = position_of(data, question_id)
    snapshot = copy.deepcopy(question)
    answer_key_changed = bool(edited_paths & ANSWER_KEY_FIELDS)
    mapping_changed = sorted(edited_paths & MAPPING_FIELDS)
    # Reordering the options renumbers them, so the answer key moves
    # with them. The audit trail records that as a re-index rather than leaving
    # it indistinguishable from a reviewer picking a different correct option.
    options_reordered, answer_reindexed = classify_option_change(changed_fields)

    # Question Edit Saved — the umbrella event, carrying the changed
    # fields with their previous and new values.
    events: List[Dict[str, Any]] = [_event(
        telemetry.TEL_QUESTION_EDIT_SAVED,
        editor_id=editor_id,
        question_id=question_id,
        question_type=q_type_key,
        previous_position=position,
        new_position=position,
        changed_fields=changed_fields,
        original_question=original,
        question_snapshot=snapshot,
        details={
            "provenance_before": provenance_before,
            "provenance_after": question.get("provenance"),
            "answer_key_changed": answer_key_changed,
            "mapping_fields_changed": mapping_changed,
            "options_reordered": options_reordered,
        },
    )]

    # Option Added / Deleted. Option add and remove are
    # expressed as a full replacement of the option list, so the count decides
    # which event it was. `pairs` is the MTF equivalent of options.
    for field_name in ("options", "pairs"):
        change = next((c for c in changed_fields if c["field"] == field_name), None)
        if change is None:
            continue
        before_count = len(change["previous_value"] or []) \
            if isinstance(change["previous_value"], list) else 0
        after_count = len(change["new_value"] or []) \
            if isinstance(change["new_value"], list) else 0
        if after_count == before_count:
            continue
        code = (telemetry.TEL_OPTION_ADDED if after_count > before_count
                else telemetry.TEL_OPTION_DELETED)
        events.append(_event(
            code,
            editor_id=editor_id,
            question_id=question_id,
            question_type=q_type_key,
            details={"field": field_name,
                     "previous_option_count": before_count,
                     "new_option_count": after_count},
        ))

    # Correct Answer Changed — carries the previous and updated
    # answer, and feeds the audit trail in its own right.
    if answer_key_changed:
        answer_change = next(c for c in changed_fields if c["field"] in ANSWER_KEY_FIELDS)
        events.append(_event(
            telemetry.TEL_CORRECT_ANSWER_CHANGED,
            editor_id=editor_id,
            question_id=question_id,
            question_type=q_type_key,
            previous_position=position,
            new_position=position,
            changed_fields=[answer_change],
            question_snapshot=snapshot,
            details={"field": answer_change["field"],
                     "previous_answer": answer_change["previous_value"],
                     "updated_answer": answer_change["new_value"],
                     "reindexed_only": answer_reindexed},
        ))

    # Mapping Updated — which mapping field changed. Also an
    # audit feed in its own right.
    if mapping_changed:
        events.append(_event(
            telemetry.TEL_MAPPING_UPDATED,
            editor_id=editor_id,
            question_id=question_id,
            question_type=q_type_key,
            previous_position=position,
            new_position=position,
            changed_fields=[c for c in changed_fields if c["field"] in MAPPING_FIELDS],
            question_snapshot=snapshot,
            details={"mapping_fields_changed": mapping_changed},
        ))

    return EditResult(
        assessment_data=data,
        events=events,
        alerts=build_change_alerts(bucket, before, changed_fields),
        question=copy.deepcopy(question),
    )


def preview_question_edit(
    assessment_data: Dict[str, Any],
    question_id: str,
    updates: Dict[str, Any],
    *,
    editor_id: str,
    enable_blooms: bool = True,
) -> Dict[str, Any]:
    """
    Compute the alerts and validation errors a pending edit would
    produce, without applying it. Backs `dry_run=true` so the client can show
    the pre-update alert before committing the save.
    """
    try:
        result = apply_question_edit(
            assessment_data, question_id, updates,
            editor_id=editor_id, enable_blooms=enable_blooms,
        )
    except ValidationError as exc:
        return {"valid": False, "errors": exc.errors, "alerts": [], "changed_fields": []}

    return {
        "valid": True,
        "errors": [],
        "alerts": result.alerts,
        "changed_fields": next(
            (e["changed_fields"] for e in result.events
             if e["event_code"] == telemetry.TEL_QUESTION_EDIT_SAVED), []),
        "question": result.question,
    }


# --------------------------------------------------------------------------
# Add a question manually
# --------------------------------------------------------------------------

# Top-level keys accepted when authoring a new question, derived from the same
# allowlist used for edits.
def _addable_keys(bucket: str) -> set:
    return {path.split(".")[0] for path in EDITABLE_FIELDS.get(bucket, set())}


def apply_question_add(
    assessment_data: Dict[str, Any],
    question_type: str,
    payload: Dict[str, Any],
    *,
    editor_id: str,
    position: Optional[int] = None,
    enable_blooms: bool = True,
) -> EditResult:
    """
    Author a new question and place it in the assessment sequence.

    The server assigns the identifier and the human-authored provenance —
    neither is client-controlled. `position` is 1-based;
    omitting it appends the question at the end.

    No AI generation happens here: the question is authored entirely by the
    user, which the source specification places explicitly out of scope.
    """
    data = normalize_assessment(assessment_data)

    bucket = resolve_bucket(question_type)
    if bucket is None:
        raise ValidationError([{
            "code": "question_type_invalid",
            "message": f"Unknown question type '{question_type}'. "
                       f"Expected one of: {', '.join(sorted(TYPE_KEY_BY_BUCKET.values()))}.",
            "field": "question_type", "question_id": None,
        }])

    allowed = _addable_keys(bucket)
    rejected = [
        key for key in payload
        if key not in allowed and key not in SERVER_OWNED_FIELDS
    ]
    if rejected:
        raise ValidationError([{
            "code": "field_not_editable",
            "message": f"'{key}' cannot be set on a new {bucket}. "
                       f"Accepted fields: {', '.join(sorted(allowed))}.",
            "field": key, "question_id": None,
        } for key in rejected])

    question: Dict[str, Any] = {
        key: copy.deepcopy(value) for key, value in payload.items() if key in allowed
    }
    question["question_id"] = new_question_id()
    question["question_type"] = QUESTION_TYPE_CONST_BY_BUCKET[bucket]
    question["provenance"] = PROV_HUMAN_AUTHORED

    data["questions"].setdefault(bucket, []).append(question)

    # Place it in the authoritative sequence.
    order: List[str] = data["question_order"]
    if question["question_id"] in order:
        order.remove(question["question_id"])
    total = len(order) + 1
    target = total if position is None else max(1, min(int(position), total))
    order.insert(target - 1, question["question_id"])

    data = normalize_assessment(data, in_place=True)
    _, _, stored = find_question(data, question["question_id"])
    # `is_new_question` turns on the option ceiling, which applies to a question
    # being authored and not to later edits of it.
    _raise_if_invalid(validate_question(
        bucket, stored, enable_blooms=enable_blooms, is_new_question=True))

    events = [_event(
        telemetry.TEL_QUESTION_ADDED,
        editor_id=editor_id,
        question_id=stored["question_id"],
        question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
        new_position=position_of(data, stored["question_id"]),
        question_snapshot=copy.deepcopy(stored),
        details={"provenance": PROV_HUMAN_AUTHORED},
    )]

    return EditResult(
        assessment_data=data,
        events=events,
        alerts=[{
            "code": "question_added", "severity": "info",
            "message": f"A human-authored question was added at position "
                       f"{position_of(data, stored['question_id'])}.",
        }],
        question=copy.deepcopy(stored),
        announcement=f"Question added at position {position_of(data, stored['question_id'])} "
                     f"of {question_count(data)}.",
    )


# --------------------------------------------------------------------------
# Delete a question
# --------------------------------------------------------------------------

def apply_question_delete(
    assessment_data: Dict[str, Any],
    question_id: str,
    *,
    editor_id: str,
    confirmed: bool = False,
) -> EditResult:
    """
    Remove a question from the assessment.

    Deletion requires explicit confirmation — the caller must
    pass `confirmed=True`, which the API surfaces as `confirm=true`. The last
    remaining question cannot be deleted.
    """
    data = normalize_assessment(assessment_data)
    entry = find_question(data, question_id)
    if entry is None:
        raise ValidationError([{
            "code": "question_not_found",
            "message": f"No question with id '{question_id}' in this assessment.",
            "field": "question_id", "question_id": question_id,
        }])

    bucket, bucket_index, question = entry
    previous_position = position_of(data, question_id)
    remaining = question_count(data) - 1

    if remaining < 1:
        raise ValidationError([{
            "code": "last_question_cannot_be_deleted",
            "message": "An assessment must contain at least one question, so the "
                       "last remaining question cannot be deleted.",
            "field": "questions", "question_id": question_id,
        }])

    if not confirmed:
        raise ValidationError([{
            "code": "confirmation_required",
            "message": "Deleting a question requires explicit confirmation. "
                       "Retry with confirm=true.",
            "field": "confirm", "question_id": question_id,
        }])

    snapshot = copy.deepcopy(question)
    del data["questions"][bucket][bucket_index]
    data["question_order"] = [q for q in data["question_order"] if str(q) != str(question_id)]

    events = [_event(
        telemetry.TEL_QUESTION_DELETED,
        editor_id=editor_id,
        question_id=question_id,
        question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
        previous_position=previous_position,
        question_snapshot=snapshot,
        details={"remaining_questions": remaining,
                 "provenance": snapshot.get("provenance")},
    )]

    return EditResult(
        assessment_data=data,
        events=events,
        alerts=[{
            "code": "question_deleted", "severity": "info",
            "message": f"Question removed from position {previous_position}. "
                       f"{remaining} question(s) remain.",
        }],
        question=snapshot,
        announcement=f"Question removed from position {previous_position}. "
                     f"{remaining} question(s) remain.",
    )


def preview_question_delete(
    assessment_data: Dict[str, Any], question_id: str
) -> Dict[str, Any]:
    """The confirmation content for a pending deletion."""
    data = normalize_assessment(assessment_data)
    entry = find_question(data, question_id)
    if entry is None:
        return {
            "valid": False,
            "errors": [{
                "code": "question_not_found",
                "message": f"No question with id '{question_id}' in this assessment.",
                "field": "question_id", "question_id": question_id,
            }],
            "alerts": [],
        }
    bucket, _, question = entry
    remaining = question_count(data) - 1
    return {
        "valid": remaining >= 1,
        "errors": [] if remaining >= 1 else [{
            "code": "last_question_cannot_be_deleted",
            "message": "An assessment must contain at least one question, so the "
                       "last remaining question cannot be deleted.",
            "field": "questions", "question_id": question_id,
        }],
        "alerts": build_delete_alerts(bucket, question, remaining),
        "position": position_of(data, question_id),
        "remaining_questions": remaining,
    }


# --------------------------------------------------------------------------
# Reorder questions
# --------------------------------------------------------------------------

def apply_question_reorder(
    assessment_data: Dict[str, Any],
    *,
    editor_id: str,
    question_order: Optional[List[str]] = None,
    question_id: Optional[str] = None,
    position: Optional[int] = None,
) -> EditResult:
    """
    Re-sequence the assessment. Two forms are supported:

      * `question_order` — the complete new sequence. Must be a permutation of
        the ids currently in the assessment: a partial or padded list is
        rejected rather than partially applied, so a stale client cannot drop
        questions by sending an out-of-date array.
      * `question_id` + `position` — move a single question to a 1-based
        position. This is the form a keyboard reorder produces, where
        the client only knows "this one, one step up".

    One audit row is written per question whose position actually changed, and
    `announcement` carries text suitable for a screen-reader live region.
    """
    data = normalize_assessment(assessment_data)
    current: List[str] = [str(q) for q in data["question_order"]]

    if question_order is not None:
        requested = [str(q) for q in question_order]
        if sorted(requested) != sorted(current):
            missing = sorted(set(current) - set(requested))
            unknown = sorted(set(requested) - set(current))
            raise ValidationError([{
                "code": "question_order_invalid",
                "message": "question_order must list every question in the assessment "
                           "exactly once."
                           + (f" Missing: {', '.join(missing)}." if missing else "")
                           + (f" Unknown: {', '.join(unknown)}." if unknown else "")
                           + (" Duplicate ids were supplied."
                              if len(set(requested)) != len(requested) else ""),
                "field": "question_order", "question_id": None,
            }])
        new_order = requested
        moved_label = None
    elif question_id is not None and position is not None:
        qid = str(question_id)
        if qid not in current:
            raise ValidationError([{
                "code": "question_not_found",
                "message": f"No question with id '{qid}' in this assessment.",
                "field": "question_id", "question_id": qid,
            }])
        target = max(1, min(int(position), len(current)))
        new_order = [q for q in current if q != qid]
        new_order.insert(target - 1, qid)
        moved_label = qid
    else:
        raise ValidationError([{
            "code": "reorder_input_required",
            "message": "Provide either question_order, or question_id together with "
                       "position.",
            "field": "question_order", "question_id": None,
        }])

    if new_order == current:
        return EditResult(assessment_data=data, announcement="Question order unchanged.")

    data["question_order"] = new_order
    index = index_questions(data)

    events: List[Dict[str, Any]] = []
    for new_index, qid in enumerate(new_order, start=1):
        old_index = current.index(qid) + 1
        if old_index == new_index:
            continue
        entry = index.get(qid)
        bucket = entry[0] if entry else None
        events.append(_event(
            telemetry.TEL_QUESTION_REORDERED,
            editor_id=editor_id,
            question_id=qid,
            question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket) if bucket else None,
            previous_position=old_index,
            new_position=new_index,
        ))

    if moved_label:
        new_index = new_order.index(moved_label) + 1
        old_index = current.index(moved_label) + 1
        announcement = (f"Question moved from position {old_index} to position "
                        f"{new_index} of {len(new_order)}.")
    else:
        announcement = (f"Question order updated. {len(events)} of "
                        f"{len(new_order)} questions changed position.")

    return EditResult(
        assessment_data=data,
        events=events,
        alerts=[{"code": "order_changed", "severity": "info", "message": announcement}],
        announcement=announcement,
    )


# --------------------------------------------------------------------------
# Whole-blob update support
# --------------------------------------------------------------------------

def diff_assessments(
    before: Dict[str, Any],
    after: Dict[str, Any],
    *,
    editor_id: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Derive audit rows from a before/after comparison, and apply the
    provenance transition to every question the comparison shows as edited.

    This backs the legacy whole-blob `PUT /update/{job_id}`, where the client
    replaces the entire payload and the server has to work out what changed.
    Granular endpoints record the reviewer's actual intent; this path can only
    report the observable difference, so an edit that happens to restore a
    previous value is invisible to it.

    Returns `(normalized_after, events)`.
    """
    old = normalize_assessment(before)
    new = normalize_assessment(after)

    old_index = index_questions(old)
    new_index = index_questions(new)
    old_order = [str(q) for q in old["question_order"]]
    new_order = [str(q) for q in new["question_order"]]

    events: List[Dict[str, Any]] = []

    # Deleted -------------------------------------------------------------
    for qid in old_order:
        if qid in new_index:
            continue
        bucket, _, question = old_index[qid]
        events.append(_event(
            telemetry.TEL_QUESTION_DELETED,
            editor_id=editor_id,
            question_id=qid,
            question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
            previous_position=old_order.index(qid) + 1,
            question_snapshot=copy.deepcopy(question),
            details={"source": "bulk_update"},
        ))

    # Added ---------------------------------------------------------------
    for qid in new_order:
        if qid in old_index:
            continue
        bucket, _, question = new_index[qid]
        # A question the server has never seen is human-authored, no
        # matter what provenance the client claimed for it.
        question["provenance"] = PROV_HUMAN_AUTHORED
        events.append(_event(
            telemetry.TEL_QUESTION_ADDED,
            editor_id=editor_id,
            question_id=qid,
            question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
            new_position=new_order.index(qid) + 1,
            question_snapshot=copy.deepcopy(question),
            details={"source": "bulk_update", "provenance": PROV_HUMAN_AUTHORED},
        ))

    # Edited --------------------------------------------------------------
    for qid in new_order:
        if qid not in old_index:
            continue
        bucket, _, old_question = old_index[qid]
        _, _, new_question = new_index[qid]

        allowed = EDITABLE_FIELDS.get(bucket, set())
        changed_fields = []
        for path in sorted(allowed):
            old_value = get_path(old_question, path)
            new_value = get_path(new_question, path)
            if old_value != new_value:
                changed_fields.append({
                    "field": path,
                    "previous_value": copy.deepcopy(old_value),
                    "new_value": copy.deepcopy(new_value),
                })

        # Server-owned fields are restored from the stored copy so a client
        # cannot relabel provenance or retype a question through this path.
        new_question["question_type"] = old_question.get(
            "question_type", QUESTION_TYPE_CONST_BY_BUCKET.get(bucket)
        )
        provenance_before = old_question.get("provenance", PROV_AI_GENERATED)
        new_question["provenance"] = provenance_before

        if not changed_fields:
            continue

        if provenance_before == PROV_AI_GENERATED:
            new_question["provenance"] = PROV_AI_ASSISTED

        edited_paths = {c["field"] for c in changed_fields}
        answer_key_changed = bool(edited_paths & ANSWER_KEY_FIELDS)
        mapping_changed = sorted(edited_paths & MAPPING_FIELDS)
        options_reordered, answer_reindexed = classify_option_change(changed_fields)

        events.append(_event(
            telemetry.TEL_QUESTION_EDIT_SAVED,
            editor_id=editor_id,
            question_id=qid,
            question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
            previous_position=old_order.index(qid) + 1,
            new_position=new_order.index(qid) + 1,
            changed_fields=changed_fields,
            original_question=(copy.deepcopy(old_question)
                               if provenance_before == PROV_AI_GENERATED else None),
            question_snapshot=copy.deepcopy(new_question),
            details={
                "source": "bulk_update",
                "provenance_before": provenance_before,
                "provenance_after": new_question["provenance"],
                "answer_key_changed": answer_key_changed,
                "mapping_fields_changed": mapping_changed,
                "options_reordered": options_reordered,
            },
        ))

        # Correct Answer Changed — an audit feed in its own right.
        if answer_key_changed:
            answer_change = next(
                c for c in changed_fields if c["field"] in ANSWER_KEY_FIELDS)
            events.append(_event(
                telemetry.TEL_CORRECT_ANSWER_CHANGED,
                editor_id=editor_id,
                question_id=qid,
                question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
                previous_position=old_order.index(qid) + 1,
                new_position=new_order.index(qid) + 1,
                changed_fields=[answer_change],
                question_snapshot=copy.deepcopy(new_question),
                details={"source": "bulk_update",
                         "field": answer_change["field"],
                         "previous_answer": answer_change["previous_value"],
                         "updated_answer": answer_change["new_value"],
                         "reindexed_only": answer_reindexed},
            ))

        # Mapping Updated — an audit feed in its own right.
        if mapping_changed:
            events.append(_event(
                telemetry.TEL_MAPPING_UPDATED,
                editor_id=editor_id,
                question_id=qid,
                question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
                previous_position=old_order.index(qid) + 1,
                new_position=new_order.index(qid) + 1,
                changed_fields=[c for c in changed_fields
                                if c["field"] in MAPPING_FIELDS],
                question_snapshot=copy.deepcopy(new_question),
                details={"source": "bulk_update",
                         "mapping_fields_changed": mapping_changed},
            ))

    # Reordered -----------------------------------------------------------
    # An absolute position changes whenever a question earlier in the sequence
    # is added or deleted, which is not a reorder. Compare the *relative* order
    # of the questions present in both versions instead, so only a genuine
    # re-sequencing produces Question Reordered rows.
    old_common = [qid for qid in old_order if qid in new_index]
    new_common = [qid for qid in new_order if qid in old_index]

    if old_common != new_common:
        old_rank = {qid: i for i, qid in enumerate(old_common)}
        new_rank = {qid: i for i, qid in enumerate(new_common)}
        for qid in new_common:
            if old_rank[qid] == new_rank[qid]:
                continue
            bucket = new_index[qid][0]
            events.append(_event(
                telemetry.TEL_QUESTION_REORDERED,
                editor_id=editor_id,
                question_id=qid,
                question_type=TYPE_KEY_BY_BUCKET.get(bucket, bucket),
                previous_position=old_order.index(qid) + 1,
                new_position=new_order.index(qid) + 1,
                details={"source": "bulk_update"},
            ))

    return new, events
