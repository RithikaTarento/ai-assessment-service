"""
Telemetry event emission for the editing workspace.

Emitting every event and building the dashboard/reporting layer is out of
scope for this release. What *is* in scope is persisting an audit record that
is explicitly fed by the Question Edit Saved, Question Added, Question
Deleted, Question Reordered, Correct Answer Changed and Mapping Updated
events. So the events Groups A-C produce are modelled here once and land in
two places:

  1. `interactive_assessment_audit` — for the six audited events, written by
     `db.py` inside the same transaction as the assessment update, so an audit
     row exists if and only if the change was actually persisted.
  2. Any sink registered with `register_sink()` — called after the transaction
     commits. No sinks ship by default beyond the structured log, which keeps
     this change free of new infrastructure. When the dashboard/reporting
     layer is picked up, a Kafka or analytics sink registers here and every
     existing call site starts feeding it with no further edits.

The full event registry (22 events in total) is declared below so the codes
are documented in one place. Groups A-C are emitted from the editing
workspace. Of Groups D and E, call sites were added for Question Distribution
Generated and Configuration Mismatch, which fire at *generation* time rather
than edit time — see `emit_generation_event`. Course Selected, Course
Removed, Question Generation Requested and Generation Limit Validation still
have no call sites.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("assessment-telemetry")

# --------------------------------------------------------------------------
# Event codes — names quoted from the source specification
# --------------------------------------------------------------------------

# Group A — question editing
TEL_ASSESSMENT_EDIT_OPENED = "TEL-01"
TEL_QUESTION_EDIT_STARTED = "TEL-02"
TEL_QUESTION_EDIT_SAVED = "TEL-03"       # carries the changed fields
TEL_QUESTION_EDIT_CANCELLED = "TEL-04"
TEL_OPTION_ADDED = "TEL-08"
TEL_OPTION_DELETED = "TEL-09"
TEL_CORRECT_ANSWER_CHANGED = "TEL-10"
TEL_MAPPING_UPDATED = "TEL-11"
TEL_VALIDATION_FAILED = "TEL-12"

# Group B — assessment composition
TEL_QUESTION_ADDED = "TEL-05"
TEL_QUESTION_DELETED = "TEL-06"
TEL_QUESTION_REORDERED = "TEL-07"

# Group C — persistence, downloads
TEL_SAVE_FAILED = "TEL-13"
TEL_SAVE_SUCCESSFUL = "TEL-14"
TEL_ASSESSMENT_DOWNLOADED = "TEL-15"
TEL_ASSESSMENT_REOPENED = "TEL-16"

# Groups D and E — Question Distribution Generated and Configuration Mismatch
# are emitted; the rest are declared for completeness and have no call sites.
TEL_COURSE_SELECTED = "TEL-17"
TEL_COURSE_REMOVED = "TEL-18"
TEL_QUESTION_DISTRIBUTION_GENERATED = "TEL-19"  # emitted at generation time
TEL_QUESTION_GENERATION_REQUESTED = "TEL-20"
TEL_GENERATION_LIMIT_VALIDATION = "TEL-21"
TEL_CONFIGURATION_MISMATCH = "TEL-22"           # emitted at generation time

EVENT_NAMES: Dict[str, str] = {
    TEL_ASSESSMENT_EDIT_OPENED: "Assessment Edit Opened",
    TEL_QUESTION_EDIT_STARTED: "Question Edit Started",
    TEL_QUESTION_EDIT_SAVED: "Question Edit Saved",
    TEL_QUESTION_EDIT_CANCELLED: "Question Edit Cancelled",
    TEL_QUESTION_ADDED: "Question Added",
    TEL_QUESTION_DELETED: "Question Deleted",
    TEL_QUESTION_REORDERED: "Question Reordered",
    TEL_OPTION_ADDED: "Option Added",
    TEL_OPTION_DELETED: "Option Deleted",
    TEL_CORRECT_ANSWER_CHANGED: "Correct Answer Changed",
    TEL_MAPPING_UPDATED: "Mapping Updated",
    TEL_VALIDATION_FAILED: "Validation Failed",
    TEL_SAVE_FAILED: "Save Failed",
    TEL_SAVE_SUCCESSFUL: "Save Successful",
    TEL_ASSESSMENT_DOWNLOADED: "Assessment Downloaded",
    TEL_ASSESSMENT_REOPENED: "Assessment Reopened",
    TEL_COURSE_SELECTED: "Course Selected",
    TEL_COURSE_REMOVED: "Course Removed",
    TEL_QUESTION_DISTRIBUTION_GENERATED: "Question Distribution Generated",
    TEL_QUESTION_GENERATION_REQUESTED: "Question Generation Requested",
    TEL_GENERATION_LIMIT_VALIDATION: "Generation Limit Validation",
    TEL_CONFIGURATION_MISMATCH: "Configuration Mismatch",
}

# Exactly the events that feed the audit trail. Everything else is
# observability-only and is logged but not persisted.
AUDITED_EVENTS = {
    TEL_QUESTION_EDIT_SAVED,
    TEL_QUESTION_ADDED,
    TEL_QUESTION_DELETED,
    TEL_QUESTION_REORDERED,
    TEL_CORRECT_ANSWER_CHANGED,
    TEL_MAPPING_UPDATED,
}

# Events the backend cannot observe on its own — they describe what the user did
# in the editor, not what was written. The client reports them through
# `POST /telemetry/{job_id}`.
UI_REPORTED_EVENTS = {
    TEL_ASSESSMENT_EDIT_OPENED,    # the workspace was opened
    TEL_QUESTION_EDIT_STARTED,     # a question was opened for editing
    TEL_QUESTION_EDIT_CANCELLED,   # the edit was abandoned, nothing saved
    TEL_ASSESSMENT_REOPENED,       # a Past Assessment was reopened
}

# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

Sink = Callable[[Dict[str, Any]], None]
_SINKS: List[Sink] = []


def register_sink(sink: Sink) -> None:
    """
    Register an additional destination for telemetry events — the hook the
    future dashboard/reporting layer will plug into.

    A sink must never raise into the caller — `emit` guards against it — and
    must not block, since it runs on the request path.
    """
    _SINKS.append(sink)


def clear_sinks() -> None:
    _SINKS.clear()


def build_event(
    event_code: str,
    *,
    job_id: str,
    editor_id: str,
    assessment_version: int,
    **fields: Any,
) -> Dict[str, Any]:
    """
    Construct one telemetry event. Every event carries the assessment ID, the
    acting editor, the resulting assessment version and a timestamp, which is
    the attributability the audit trail requires.
    """
    event: Dict[str, Any] = {
        "event_code": event_code,
        "event_name": EVENT_NAMES.get(event_code, event_code),
        "assessment_id": job_id,
        "editor_id": editor_id,
        "assessment_version": assessment_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    return event


def build_generation_event(
    event_code: str,
    *,
    job_id: str,
    **fields: Any,
) -> Dict[str, Any]:
    """
    Construct a generation-time event (Question Distribution Generated,
    Configuration Mismatch).

    Deliberately not `build_event`: those events describe an *edit*, so they
    carry an `editor_id` and the resulting `assessment_version`. At generation
    time neither exists — nobody has edited anything and no version has been
    written — so requiring them would mean inventing values. The assessment ID
    and timestamp the spec asks for are still here.
    """
    event: Dict[str, Any] = {
        "event_code": event_code,
        "event_name": EVENT_NAMES.get(event_code, event_code),
        "assessment_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    return event


def emit(event: Dict[str, Any]) -> None:
    """Log the event and fan it out to every registered sink."""
    try:
        logger.info("TELEMETRY %s", json.dumps(event, default=str, ensure_ascii=False))
    except Exception:  # pragma: no cover — logging must never break a save
        logger.info("TELEMETRY %s", event)

    for sink in _SINKS:
        try:
            sink(event)
        except Exception:
            logger.exception("Telemetry sink %r failed for event %s",
                             sink, event.get("event_code"))


def emit_all(events: List[Dict[str, Any]]) -> None:
    for event in events:
        emit(event)


def emit_generation_event(
    event_code: str,
    *,
    job_id: str,
    **fields: Any,
) -> Dict[str, Any]:
    """
    Emit a generation-time event and return it (Question Distribution
    Generated, Configuration Mismatch).

    Question Distribution Generated carries the course-wise allocation and
    the total; Configuration Mismatch carries the configured count against
    the actual count. Both are observability-only —
    neither is in `AUDITED_EVENTS`, because no assessment row exists yet for an
    audit entry to hang off.
    """
    event = build_generation_event(event_code, job_id=job_id, **fields)
    emit(event)
    return event


def emit_distribution_generated(
    *,
    job_id: str,
    allocation: Dict[str, int],
    total_questions: int,
    **fields: Any,
) -> Dict[str, Any]:
    """Question Distribution Generated."""
    return emit_generation_event(
        TEL_QUESTION_DISTRIBUTION_GENERATED,
        job_id=job_id,
        course_allocation=allocation,
        total_questions=total_questions,
        **fields,
    )


def emit_configuration_mismatch(
    *,
    job_id: str,
    configured_count: int,
    actual_count: int,
    **fields: Any,
) -> Dict[str, Any]:
    """Configuration Mismatch."""
    return emit_generation_event(
        TEL_CONFIGURATION_MISMATCH,
        job_id=job_id,
        configured_count=configured_count,
        actual_count=actual_count,
        **fields,
    )


# Bulky payloads belong in the audit table, not in a telemetry event.
_AUDIT_ONLY_KEYS = {"original_question", "question_snapshot", "event_name"}


def emit_change_events(
    rows: List[Dict[str, Any]],
    *,
    job_id: str,
    editor_id: str,
    assessment_version: int,
) -> None:
    """
    Emit one telemetry event per recorded change. Call this only after the
    transaction commits, so telemetry never reports a change that was rolled
    back.

    `changed_fields` is reduced to the list of field names — enough for a
    future "most frequently edited fields" metric without duplicating the
    full before/after values already held in the audit table.
    """
    events: List[Dict[str, Any]] = []
    for row in rows:
        fields = {k: v for k, v in row.items()
                  if k not in _AUDIT_ONLY_KEYS and k not in ("event_code", "editor_id")}
        changed = fields.pop("changed_fields", None)
        if changed:
            fields["changed_field_names"] = [c.get("field") for c in changed]
        events.append(build_event(
            row["event_code"],
            job_id=job_id,
            editor_id=row.get("editor_id") or editor_id,
            assessment_version=assessment_version,
            **fields,
        ))
    emit_all(events)


def emit_ui_event(
    event_code: str,
    *,
    job_id: str,
    editor_id: str,
    assessment_version: int,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """
    Emit an editor-lifecycle event reported by the client (Assessment Edit
    Opened, Question Edit Started, Question Edit Cancelled, Assessment
    Reopened). Returns the event, or None if the code is not one a client
    is allowed to report — the backend owns every event that describes a write.
    """
    if event_code not in UI_REPORTED_EVENTS:
        return None
    event = build_event(event_code, job_id=job_id, editor_id=editor_id,
                        assessment_version=assessment_version, **fields)
    emit(event)
    return event
