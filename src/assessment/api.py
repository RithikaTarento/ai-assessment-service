import os
import shutil
import tempfile
import logging
import json
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException, APIRouter, Header, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.openapi.utils import get_openapi
from contextlib import asynccontextmanager

from .db import (
    init_db, close_db, create_job, get_assessment_status, find_job_by_prefix,
    create_completed_job, get_user_assessments_history,
    save_edited_assessment, get_audit_trail,
)
from .config import INTERACTIVE_COURSES_PATH
from .storage import get_storage_service
from .exporters import generate_pdf, generate_docx
from .cleanup import start_cleanup_scheduler, stop_cleanup_scheduler
from .events import stop_kafka_producer, send_request_event
from .exporters_csv_v2 import generate_csv_v2, generate_csv_basic
from . import telemetry
from .questions import normalize_assessment, ordered_questions, question_count
from .validation import ValidationError, validate_assessment
from .editing import (
    EditResult, apply_question_add, apply_question_delete, apply_question_edit,
    apply_question_reorder, diff_assessments, preview_question_delete,
    preview_question_edit,
)

# Configure Logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(), # Good for Docker/K8s
        logging.FileHandler(log_dir / "api.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("assessment-api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Assessment API...")

    from .config import SSO_URL, SSO_REALM, JWKS_URL
    missing = [name for name, val in [("SUNBIRD_SSO_URL", SSO_URL), ("SUNBIRD_SSO_REALM", SSO_REALM)] if not val]
    if missing:
        raise RuntimeError(f"Missing mandatory env vars: {', '.join(missing)}")
    logger.info(f"SSO configured: {JWKS_URL}")

    try:
        await init_db()
    except Exception as e:
        logger.error(f"Database connection failed: {e}")

    # Start Background Scheduler
    start_cleanup_scheduler()

    yield

    stop_cleanup_scheduler()
    await stop_kafka_producer()
    await close_db()
    logger.info("Shutting down Assessment API...")

app = FastAPI(
    title="Course Assessment Generator API (v1.0)",
    description="Audit-ready event-driven assessment generation using Gemini 2.5 Pro and Kafka",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json"
)

@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "assessment-generator"}

from enum import Enum
from typing import Any, List, Optional, Dict

class AssessmentType(str, Enum):
    PRACTICE = "practice"
    FINAL = "final"
    COMPREHENSIVE = "comprehensive"
    STANDALONE = "standalone"
    COMPETENCY = "competency"

class Difficulty(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"

class Language(str, Enum):
    ENGLISH = "english"
    HINDI = "hindi"
    TAMIL = "tamil"
    TELUGU = "telugu"
    KANNADA = "kannada"
    MALAYALAM = "malayalam"
    MARATHI = "marathi"
    BENGALI = "bengali"
    GUJARATI = "gujarati"
    PUNJABI = "punjabi"
    ODIA = "odia"
    ASSAMESE = "assamese"

class QuestionType(str, Enum):
    MCQ = "mcq"
    FTB = "ftb"
    MTF = "mtf"
    MULTICHOICE = "multichoice"
    TRUE_FALSE = "truefalse"

# ==========================================
# API V1 Router
# ==========================================
from fastapi import Depends
from .auth import get_current_user

api_v1_router = APIRouter(prefix="/ai-assessments/v1", tags=["AI Assessments"])

@api_v1_router.post("/generate")
async def generate_v1(
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user), # AUTH REQUIREMENT
    course_ids: Optional[List[str]] = Form(None, description="List of Course IDs"),
    force: bool = Form(False),
    assessment_type: AssessmentType = Form(...),
    difficulty: Difficulty = Form(...),
    total_questions: int = Form(5),
    question_type_counts: str = Form(
        '{"mcq": 5, "ftb": 5, "mtf": 5, "multichoice": 5, "truefalse": 5}',
        description='JSON: mcq, ftb, mtf, multichoice, truefalse counts. Example: mcq=5, ftb=5, mtf=5, multichoice=5, truefalse=5'
    ),
    time_limit: Optional[int] = Form(None),
    topic_names: Optional[str] = Form(""),
    language: Language = Form(Language.ENGLISH),
    blooms_config: Optional[str] = Form(
        '{"Remember": 20, "Understand": 30, "Apply": 30, "Analyze": 10, "Analyze": 10, "Evaluate": 10, "Create": 0}',
        description="JSON string of Bloom's percentage per level"
    ),
    enable_blooms: bool = Form(True, description="Enable or disable Bloom's taxonomy"),
    course_weightage: Optional[str] = Form(None, description="JSON mapping course IDs to weightage % (Comprehensive Phase only)"),
    course_names: Optional[List[str]] = Form(None, description="Course names matching the order of course_ids. Pass as repeated fields or a single comma-separated value."),
    competency_area: Optional[str] = Form(None, description="Competency area (required for competency assessment type). e.g. 'Behavioural'"),
    competency_themes: Optional[List[str]] = Form(None, description="Competency themes (required for competency type). Pass as repeated fields or a single comma-separated value."),
    competency_sub_themes: Optional[List[str]] = Form(None, description="Competency sub-themes (required for competency type). Pass as repeated fields or a single comma-separated value."),
    additional_instructions: Optional[str] = Form(""),
    files: Optional[List[UploadFile]] = File(None)
):
    """
    V2 Generation Endpoint:
    1. Authenticated: Requires valid `x-authenticated-user-token` (User ID extraction).
    2. Private Instances: Every request gets a unique Job ID (Hash + UserID).
    3. Clone-on-Request: If a matching assessment exists (even from another user), it is instantly CLONED to this user's workspace.
    4. Async/Sync Hybrid: 
       - Returns 200 OK + JSON if cache hit/cloned.
       - Returns 202 Accepted if new generation started.
    """
    
    # --- 1. Validation & Logic Reuse (Same as V1) ---
    valid_files = []
    if files:
        for f in files:
            if not isinstance(f, str):
                valid_files.append(f)
    files = valid_files

    if topic_names in ["string", ""]: topic_names = None
    if blooms_config in ["string", ""]: blooms_config = None
    if additional_instructions in ["string", ""]: additional_instructions = None
    
    c_ids = []
    if course_ids:
        for item in course_ids:
            c_ids.extend([c.strip() for c in item.split(",") if c.strip()])
    
    parsed_competency_themes = []
    if competency_themes:
        for item in competency_themes:
            parsed_competency_themes.extend([t.strip() for t in item.split(",") if t.strip()])

    parsed_competency_sub_themes = []
    if competency_sub_themes:
        for item in competency_sub_themes:
            parsed_competency_sub_themes.extend([s.strip() for s in item.split(",") if s.strip()])

    if assessment_type == AssessmentType.COMPETENCY:
        if not competency_area or not parsed_competency_themes or not parsed_competency_sub_themes:
            raise HTTPException(status_code=400, detail="competency_area, competency_themes, and competency_sub_themes are required for competency assessment type.")
        # competency type can work purely from KCM descriptions — no course_ids or files required
    elif not c_ids and not valid_files:
        raise HTTPException(status_code=400, detail="Must provide either Course ID(s) or Uploaded Files.")

    valid_types = {t.value for t in QuestionType}

    q_counts: Dict[str, int] = json.loads(question_type_counts)
    q_types = list(q_counts.keys())
    
    for qtype, count in q_counts.items():
        if qtype not in valid_types:
            raise HTTPException(400, f"Unknown question type: {qtype}")

    t_names = [t.strip() for t in topic_names.split(",")] if topic_names else None
    
    b_dist = None
    if blooms_config:
        try:
            raw = json.loads(blooms_config)
            # Normalize keys to title-case for internal storage and computation
            b_dist = {k.capitalize(): v for k, v in raw.items()}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON for blooms_config")

    # --- 2. Hashing (Same logic) ---
    import hashlib
    param_list = [
        str(assessment_type), str(difficulty), str(total_questions),
        str(q_counts), str(sorted(q_types)), str(time_limit),
        str(topic_names), str(language), str(blooms_config), str(enable_blooms), str(course_weightage), str(additional_instructions)
    ]
    if files:
         param_list.extend([f.filename for f in valid_files])

    param_str = "_".join(param_list)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]

    if c_ids:
        sorted_ids = sorted(c_ids)
        base_id = f"comprehensive_{'_'.join(sorted_ids)}" if len(sorted_ids) > 1 else sorted_ids[0]
    else:
        base_id = "custom_upload"
        
    composite_id = f"{base_id}_{param_hash}" # This is the "Shared Signature"
    
    # V2 Logic: User-Specific IDs to allow private editing
    # Format: {Shared_Signature}_{User_ID}
    # But we fallback to Shared_Signature lookup for cache hits
    
    user_job_id = f"{composite_id}_{user_id}"

    logger.info(f"[{user_job_id}] Generate request | user={user_id} | type={assessment_type} | courses={c_ids} | questions={total_questions} | language={language} | difficulty={difficulty} | force={force}")

    # --- 3. Check Status (V2 Logic: Clone or Generate) ---

    # A. Check if THIS user already has this job
    existing_own = await get_assessment_status(user_job_id)
    if existing_own and not force:
        status = existing_own['status']
        if status == 'COMPLETED':
            logger.info(f"[{user_job_id}] Cache hit (own) — returning existing result")
            result = existing_own['assessment_data']
            return {
                "message": "Assessment retrieved from cache",
                "status": "COMPLETED",
                "job_id": user_job_id,
                "result": result
            }
        elif status == 'IN_PROGRESS':
            logger.info(f"[{user_job_id}] Job already IN_PROGRESS — returning status")
            return {"message": "Assessment generation in progress", "status": "IN_PROGRESS", "job_id": user_job_id}

    # B. If not found (or forced), check if a TEMPLATE exists (Shared Cache)
    if not force:
        template = await find_job_by_prefix(composite_id)
        if template:
            logger.info(f"[{user_job_id}] Cloning from template {template['course_id']} for user {user_id}")
            t_meta = template['metadata']
            # Clone the pristine AI copy, never the template owner's edits — the
            # recipient must receive AI-generated content with `ai_generated`
            # provenance and no inherited audit history. `ai_original_data`
            # is NULL only on rows generated before the column existed, where
            # `assessment_data` is the original by definition.
            t_data = template['ai_original_data'] or template['assessment_data']
            t_usage = template['token_usage']
            await create_completed_job(user_job_id, user_id, t_meta, t_data, t_usage)
            logger.info(f"[{user_job_id}] Clone complete")
            return {
                "message": "Assessment cloned from cache",
                "status": "COMPLETED",
                "job_id": user_job_id,
                "result": t_data
            }

    # --- 4. Start New Job ---
    parsed_course_names = []
    if course_names:
        for item in course_names:
            parsed_course_names.extend([n.strip() for n in item.split(",") if n.strip()])

    initial_metadata = {
        "course_ids": c_ids,
        "course_names": parsed_course_names,
        "config": {
            "assessment_type": assessment_type,
            "difficulty": difficulty,
            "total_questions": total_questions,
            "question_type_counts": q_counts,
            "language": language,
            "time_limit": time_limit,
            "course_weightage": course_weightage,
            "competency_area": competency_area,
            "competency_themes": parsed_competency_themes,
            "competency_sub_themes": parsed_competency_sub_themes,
            "topic_names": t_names,
            "blooms_config": b_dist,
            "enable_blooms": enable_blooms,
            "additional_instructions": additional_instructions,
        }
    }
    await create_job(user_job_id, user_id=user_id, metadata=initial_metadata)
    logger.info(f"[{user_job_id}] Job created in DB with status PENDING")

    saved_files = []
    if files:
        storage = get_storage_service()
        for file in files:
            stored_path, size = storage.save_file(file.file, file.filename, user_job_id)
            saved_files.append(stored_path)
            logger.info(f"[{user_job_id}] Uploaded file stored: {file.filename} ({size} bytes) → {stored_path}")

    # Construct Payload for Worker — full self-contained payload, no DB roundtrip needed
    worker_payload = {
        "job_id": user_job_id, # Use user_job_id for the worker to process
        "user_id": user_id, # V2: Pass real user_id
        "course_ids": c_ids,
        "course_names": parsed_course_names,
        "extra_files": [str(p) for p in saved_files],
        "assessment_type": assessment_type.value if hasattr(assessment_type, 'value') else assessment_type,
        "difficulty": difficulty.value if hasattr(difficulty, 'value') else difficulty,
        "total_questions": total_questions,
        "question_type_counts": q_counts,
        "additional_instructions": additional_instructions,
        "language": language.value if hasattr(language, 'value') else language,
        "topic_names": t_names,
        "blooms_distribution": b_dist,
        "enable_blooms": enable_blooms,
        "course_weightage": course_weightage,
        "question_types": q_types,
        "time_limit": time_limit,
        "competency_area": competency_area,
        "competency_themes": parsed_competency_themes,
        "competency_sub_themes": parsed_competency_sub_themes,
        # Pre-built config dict — worker reuses this verbatim as the DB-stored config
        "config": initial_metadata["config"],
    }

    await send_request_event(worker_payload)
    logger.info(f"[{user_job_id}] Job queued to Kafka | topic={os.getenv('KAFKA_REQUEST_TOPIC', 'assessment.request')}")
    return {"message": "Generation started (Queued)", "status": "PENDING", "job_id": user_job_id}

@api_v1_router.get("/status/{job_id}", summary="Get Assessment Status")
async def check_status_v1(job_id: str, user_id: str = Depends(get_current_user)):
    logger.info(f"[{job_id}] Status check | user={user_id}")
    status = await get_assessment_status(job_id)
    if not status:
        logger.warning(f"[{job_id}] Status check — job not found | user={user_id}")
        return JSONResponse(status_code=404, content={"status": "NOT_FOUND"})
    if status.get('user_id') and status.get('user_id') != user_id:
        logger.warning(f"[{job_id}] Status check — access denied | requester={user_id} | owner={status.get('user_id')}")
        raise HTTPException(status_code=403, detail="Access denied: you do not own this assessment")
    logger.info(f"[{job_id}] Status check — current status={status.get('status')}")

    # Lowercase blooms_config keys in the response to match generate endpoint format
    meta = status.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    config = meta.get("config") or {}
    if "blooms_config" in config and isinstance(config["blooms_config"], dict):
        config["blooms_config"] = {k.lower(): v for k, v in config["blooms_config"].items()}
    status["metadata"] = meta

    # Normalize the stored payload before returning it so every client sees a
    # `question_id` on each question and the authoritative `question_order`,
    # including for assessments generated before the editing workspace existed.
    # This is a read-time projection — nothing is written back here; the
    # normalized form is persisted on the first edit.
    if status.get("assessment_data"):
        status["assessment_data"] = normalize_assessment(status["assessment_data"])

    return status


# ==========================================================================
# Editing workspace — Groups A, B, C
# ==========================================================================

from pydantic import BaseModel, ConfigDict, Field


class AssessmentUpdate(BaseModel):
    assessment_data: Dict
    version: Optional[int] = Field(
        None,
        description="Assessment version this edit is based on, from GET /status. "
                    "When supplied, the save is rejected with 409 if another "
                    "writer has changed the assessment since.",
    )


# ==========================================================================
# Sunbird request envelope
#
# The editing endpoints are routed by a Kong 0.10–0.14 `API` entity, which can
# only prefix-match: it strips the matched prefix and appends the rest of the
# path verbatim upstream. It cannot reorder segments, and it cannot express a
# path parameter with further segments after it. So the routable shape is a
# static verb prefix followed by `job_id` as the single trailing segment, and
# every other identifier travels in the body instead:
#
#     POST /questions/update/{job_id}   body: {"request": {"questionId": ...}}
#
# Bodies are wrapped in the Sunbird envelope, `{"request": {...}}`. Responses
# are left bare, matching every other handler in this service.
# ==========================================================================


class _EnvelopeBody(BaseModel):
    """
    Base for the inner object of a `{"request": {...}}` body.

    `populate_by_name` lets each field be sent either as the camelCase name the
    gateway contract documents (`questionId`) or as the snake_case name this
    service uses internally (`question_id`).
    """
    model_config = ConfigDict(populate_by_name=True)


class QuestionEditBody(_EnvelopeBody):
    question_id: Optional[Any] = Field(
        None, alias="questionId",
        description="Identifier of the question to edit. Required — it was a "
                    "path parameter before the gateway reshape.",
    )
    updates: Dict[str, object] = Field(
        ...,
        description="Field updates keyed by dotted path, e.g. "
                    '{"question_text": "...", "correct_option_index": 2, '
                    '"reasoning.competency_alignment.kcm.competency_theme": "Integrity"}',
    )
    version: Optional[int] = Field(None, description="Version this edit is based on.")


class QuestionEditRequest(BaseModel):
    request: Optional[QuestionEditBody] = Field(
        None, description="Sunbird request envelope."
    )


class QuestionAddBody(_EnvelopeBody):
    question_type: str = Field(
        ..., alias="questionType",
        description="mcq | ftb | mtf | multichoice | truefalse",
    )
    question: Dict[str, object] = Field(
        ..., description="The authored question — text, options, correct answer, "
                         "rationale and mapping. The identifier and the "
                         "human-authored provenance are assigned by the server."
    )
    position: Optional[int] = Field(
        None, description="1-based position in the assessment sequence. "
                         "Omit to append at the end."
    )
    version: Optional[int] = None


class QuestionAddRequest(BaseModel):
    request: Optional[QuestionAddBody] = Field(
        None, description="Sunbird request envelope."
    )


class QuestionDeleteBody(_EnvelopeBody):
    question_id: Optional[Any] = Field(
        None, alias="questionId",
        description="Identifier of the question to delete. Required — it was a "
                    "path parameter before the gateway reshape.",
    )
    confirm: Optional[Any] = Field(
        None,
        description="Must be `true`. Deletion requires explicit confirmation. "
                    "Was a query parameter before the reshape.",
    )
    version: Optional[int] = Field(
        None, description="Version this delete is based on."
    )


class QuestionDeleteRequest(BaseModel):
    request: Optional[QuestionDeleteBody] = Field(
        None, description="Sunbird request envelope."
    )


class QuestionReorderBody(_EnvelopeBody):
    question_order: Optional[List[str]] = Field(
        None, alias="questionOrder",
        description="The complete new sequence. Must list every question "
                    "in the assessment exactly once.",
    )
    question_id: Optional[str] = Field(
        None, alias="questionId",
        description="Single-question move — used with `position`. This is "
                    "the form a keyboard reorder produces.",
    )
    position: Optional[int] = Field(None, description="1-based target position.")
    version: Optional[int] = None


class QuestionReorderRequest(BaseModel):
    request: Optional[QuestionReorderBody] = Field(
        None, description="Sunbird request envelope."
    )


def _missing_request_response() -> JSONResponse:
    """
    A body that is not the Sunbird envelope. 400 in the same shape as every
    other validation failure here, rather than Pydantic's 422.
    """
    return _validation_response([{
        "code": "request_required",
        "field": "request",
        "message": 'Request body must be the Sunbird envelope: {"request": {...}}.',
    }])


def _question_id_errors(value: object) -> List[Dict]:
    """
    Validate the `questionId` body field — missing, non-string or blank.

    Deliberately does NOT check that the id names a real question. A
    well-formed id that matches nothing stays a 404 raised by the editing
    layer's `question_not_found`, exactly as it was when the id arrived in the
    path. Only malformed input is a 400.
    """
    if value is None:
        return [{"code": "question_id_required", "field": "questionId",
                 "message": "questionId is required in the request body."}]
    if not isinstance(value, str):
        return [{"code": "question_id_invalid", "field": "questionId",
                 "message": "questionId must be a string."}]
    if not value.strip():
        return [{"code": "question_id_invalid", "field": "questionId",
                 "message": "questionId must not be blank."}]
    return []


def _validation_response(errors: List[Dict], status_code: int = 400) -> JSONResponse:
    """
    A blocked save. `detail` stays a plain string so existing error
    handling keeps working; `errors` carries the per-field detail.
    """
    first = errors[0]["message"] if errors else "Validation failed"
    detail = first if len(errors) == 1 else f"{first} ({len(errors)} validation errors)"
    return JSONResponse(status_code=status_code, content={"detail": detail, "errors": errors})


def _conflict_response(job_id: str, current_version: Optional[int]) -> JSONResponse:
    """Concurrent update detected. The save is blocked, not merged."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": "This assessment was changed by another update since you loaded it. "
                      "Reload the assessment and re-apply your change.",
            "job_id": job_id,
            "current_version": current_version,
        },
    )


def _owns(row: Dict, user_id: str) -> bool:
    """
    Ownership test that also resolves rows predating the `user_id` column.

    `user_id` is nullable for v1 compatibility, so the earliest assessments have
    no owner recorded. Their owner is still recoverable without touching stored
    data: `course_id` has always been built as f"{composite_id}_{user_id}" (see
    `generate_assessment`), so a legacy row belongs to whoever's id it ends with.

    This grants no access that did not already exist — a caller can only match a
    row whose id ends with their own authenticated user id, which is precisely
    the row they created. A suffix test is used rather than parsing the id
    because the middle segment varies (`comprehensive_<ids>_<hash>`,
    `<course_id>_<hash>`, `custom_upload_<hash>`) and user ids themselves contain
    underscores.
    """
    recorded = row.get("user_id")
    if recorded:
        return recorded == user_id
    return str(row.get("course_id") or "").endswith(f"_{user_id}")


async def _load_for_edit(job_id: str, user_id: str) -> Dict:
    """
    Fetch a completed, user-owned assessment ready for editing.
    Raises HTTPException for the non-editable cases.
    """
    row = await get_assessment_status(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if not _owns(row, user_id):
        logger.warning(f"[{job_id}] Edit denied — requester={user_id} | owner={row.get('user_id')}")
        raise HTTPException(
            status_code=403, detail="Access denied: you do not own this assessment"
        )
    if row.get("status") != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail=f"Assessment is {row.get('status')} and cannot be edited until "
                   f"generation completes.",
        )
    return row


def _blooms_enabled(row: Dict) -> bool:
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    config = meta.get("config") or {}
    return bool(config.get("enable_blooms", True))


def _expected_version(row: Dict, body_version: Optional[int],
                      if_match: Optional[str]) -> int:
    """
    Resolve the version this write compares against.

    A client-supplied version (body field or `If-Match` header) is honoured, so
    a stale client is rejected even if it re-read the row a moment ago. Without
    one, the version just read is used, which still closes the read-modify-write
    window inside this request.
    """
    stored = int(row.get("version") or 1)
    claimed = body_version
    if claimed is None and if_match:
        try:
            claimed = int(if_match.strip().strip('"'))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="If-Match must be the integer assessment version, e.g. 'If-Match: 3'.",
            )
    if claimed is not None and int(claimed) != stored:
        raise _Conflict(stored)
    return stored


class _Conflict(Exception):
    def __init__(self, current_version: int):
        self.current_version = current_version


async def _commit(
    job_id: str,
    user_id: str,
    expected_version: int,
    result,
    *,
    operation: str,
    question_id: Optional[str] = None,
) -> int:
    """
    Persist an editing result and its audit trail, then emit
    telemetry (Save Failed / Save Successful plus the per-change events).

    Success is only reported after the database confirms the write, which is
    what lets the client show "Saved successfully" truthfully.

    This is also what prevents duplicate saves. A double submission is
    two requests carrying the same expected version: the first commits and moves
    the version on, the second matches zero rows and returns 409 having written
    nothing. Even with no version supplied, the second request diffs to no
    changes and is a no-op. Neither path can apply the same edit twice.
    """
    audit_rows = result.audit_rows
    new_version = await save_edited_assessment(
        job_id, user_id, expected_version, result.assessment_data, audit_rows
    )

    if new_version is None:
        current = await get_assessment_status(job_id)
        current_version = int((current or {}).get("version") or 0)
        logger.warning(
            f"[{job_id}] {operation} rejected — version conflict | "
            f"expected={expected_version} | current={current_version} | user={user_id}"
        )
        telemetry.emit(telemetry.build_event(
            telemetry.TEL_SAVE_FAILED, job_id=job_id, editor_id=user_id,
            assessment_version=expected_version, operation=operation,
            question_id=question_id, reason="version_conflict", api_status=409,
        ))
        raise _Conflict(current_version)

    logger.info(
        f"[{job_id}] {operation} saved | version={new_version} | "
        f"events={len(result.events)} | audit_rows={len(audit_rows)} | user={user_id}"
    )
    # Emit every event the operation produced. The audit table holds only the
    # six audit feeds; telemetry sees them all, Option Added / Option Deleted included.
    telemetry.emit_change_events(
        result.events, job_id=job_id, editor_id=user_id,
        assessment_version=new_version,
    )
    telemetry.emit(telemetry.build_event(
        telemetry.TEL_SAVE_SUCCESSFUL, job_id=job_id, editor_id=user_id,
        assessment_version=new_version, operation=operation,
        question_id=question_id,
        question_count=question_count(result.assessment_data),
    ))
    return new_version


def _emit_validation_failure(job_id: str, user_id: str, row: Dict,
                             operation: str, errors: List[Dict],
                             question_id: Optional[str] = None) -> None:
    """
    Validation Failed — a blocked save is observable, which is
    what the validation-failure-rate metric is built on.

    Also emits Save Failed, so every attempted save that did not
    persist — whether blocked by validation or lost to a version conflict —
    lands in the same save-failure-rate bucket. Validation Failed keeps firing
    alongside it so the validation-specific breakdown is not lost.
    """
    resolved_question_id = question_id or (errors[0].get("question_id") if errors else None)
    assessment_version = int(row.get("version") or 1)
    telemetry.emit(telemetry.build_event(
        telemetry.TEL_VALIDATION_FAILED, job_id=job_id, editor_id=user_id,
        assessment_version=assessment_version, operation=operation,
        question_id=resolved_question_id,
        validation_errors=[{"code": e.get("code"), "field": e.get("field")}
                           for e in errors],
    ))
    telemetry.emit(telemetry.build_event(
        telemetry.TEL_SAVE_FAILED, job_id=job_id, editor_id=user_id,
        assessment_version=assessment_version, operation=operation,
        question_id=resolved_question_id, reason="validation_failed", api_status=400,
    ))


def _saved_payload(job_id: str, version: int, data: Dict, result) -> Dict:
    return {
        "message": "Saved successfully",
        "status": "COMPLETED",
        "job_id": job_id,
        "version": version,
        "question_order": data.get("question_order", []),
        "total_questions": question_count(data),
        "alerts": result.alerts,
        "announcement": result.announcement,
    }


@api_v1_router.get(
    "/questions/list/{job_id}",
    summary="List questions in assessment order",
)
async def list_questions_v1(job_id: str, user_id: str = Depends(get_current_user)):
    """
    The authoritative question sequence, flattened and position-annotated —
    what the editing workspace renders.

    Each item carries `position` (1-based), `question_bucket`,
    `question_type_key` and `provenance` alongside the question's own fields.
    """
    row = await _load_for_edit(job_id, user_id)
    data = normalize_assessment(row.get("assessment_data"))
    return {
        "job_id": job_id,
        "version": int(row.get("version") or 1),
        "total_questions": question_count(data),
        "question_order": data.get("question_order", []),
        "questions": ordered_questions(data),
    }


@api_v1_router.post(
    "/questions/update/{job_id}",
    summary="Edit a question in place",
)
async def edit_question_v1(
    job_id: str,
    payload: QuestionEditRequest,
    user_id: str = Depends(get_current_user),
    dry_run: bool = Query(
        False,
        description="Validate the edit and return the pre-update alerts without "
                    "saving anything.",
    ),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Apply field-level updates to one question. Editable paths cover question
    text, options, correct answer, rationale, Bloom's level, relevance,
    learning outcome, competency and course mapping.

    `questionId` names the question and travels in the body — the gateway
    cannot route a path parameter followed by further segments.

    An edited AI-generated question is recorded as **AI-assisted**; a
    human-authored question stays human-authored.
    """
    if payload.request is None:
        return _missing_request_response()
    body = payload.request

    id_errors = _question_id_errors(body.question_id)
    if id_errors:
        return _validation_response(id_errors)
    question_id: str = body.question_id

    row = await _load_for_edit(job_id, user_id)
    data = row.get("assessment_data") or {}
    enable_blooms = _blooms_enabled(row)

    if dry_run:
        preview = preview_question_edit(
            data, question_id, body.updates,
            editor_id=user_id, enable_blooms=enable_blooms,
        )
        preview["job_id"] = job_id
        preview["version"] = int(row.get("version") or 1)
        preview["dry_run"] = True
        return preview

    try:
        expected = _expected_version(row, body.version, if_match)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    try:
        result = apply_question_edit(
            data, question_id, body.updates,
            editor_id=user_id,
            enable_blooms=enable_blooms,
            ai_original_data=row.get("ai_original_data"),
        )
    except ValidationError as exc:
        if any(e["code"] == "question_not_found" for e in exc.errors):
            return _validation_response(exc.errors, status_code=404)
        _emit_validation_failure(job_id, user_id, row, "edit_question", exc.errors,
                                 question_id=question_id)
        logger.info(f"[{job_id}] Edit blocked by validation | question={question_id} | "
                    f"errors={[e['code'] for e in exc.errors]}")
        return _validation_response(exc.errors)

    if not result.changed:
        return {
            "message": "No changes to save",
            "status": "COMPLETED",
            "job_id": job_id,
            "version": int(row.get("version") or 1),
            "question": result.question,
            "alerts": [],
        }

    try:
        version = await _commit(job_id, user_id, expected, result,
                                operation="edit_question", question_id=question_id)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    response = _saved_payload(job_id, version, result.assessment_data, result)
    response["question"] = result.question
    return response


@api_v1_router.post(
    "/questions/create/{job_id}",
    summary="Add a question manually",
)
async def add_question_v1(
    job_id: str,
    payload: QuestionAddRequest,
    user_id: str = Depends(get_current_user),
    dry_run: bool = Query(False, description="Validate without saving."),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Author a new question. The server assigns a unique identifier and marks the
    question **human-authored** — neither can be set by the
    caller. No AI generation is involved.
    """
    if payload.request is None:
        return _missing_request_response()
    body = payload.request

    row = await _load_for_edit(job_id, user_id)
    data = row.get("assessment_data") or {}
    enable_blooms = _blooms_enabled(row)

    try:
        expected = _expected_version(row, body.version, if_match)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    try:
        result = apply_question_add(
            data, body.question_type, body.question,
            editor_id=user_id, position=body.position, enable_blooms=enable_blooms,
        )
    except ValidationError as exc:
        _emit_validation_failure(job_id, user_id, row, "add_question", exc.errors)
        logger.info(f"[{job_id}] Add blocked by validation | "
                    f"errors={[e['code'] for e in exc.errors]}")
        return _validation_response(exc.errors)

    if dry_run:
        return {
            "valid": True, "errors": [], "dry_run": True, "job_id": job_id,
            "version": int(row.get("version") or 1),
            "alerts": result.alerts, "question": result.question,
        }

    try:
        version = await _commit(
            job_id, user_id, expected, result, operation="add_question",
            question_id=(result.question or {}).get("question_id"))
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    response = _saved_payload(job_id, version, result.assessment_data, result)
    response["question"] = result.question
    response["question_id"] = (result.question or {}).get("question_id")
    return JSONResponse(status_code=201, content=response)


@api_v1_router.post(
    "/questions/delete/{job_id}",
    summary="Delete a question, with confirmation",
)
async def delete_question_v1(
    job_id: str,
    payload: QuestionDeleteRequest,
    user_id: str = Depends(get_current_user),
    dry_run: bool = Query(
        False,
        description="Return the confirmation content for this deletion without "
                    "applying it.",
    ),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Remove a question. Requires `confirm: true` in the body, and the last
    remaining question cannot be deleted.

    This is a POST rather than a DELETE because `questionId` and the
    confirmation flag now travel in the body, and a DELETE must not carry
    one — intermediate proxies are free to drop it.
    """
    if payload.request is None:
        return _missing_request_response()
    body = payload.request

    id_errors = _question_id_errors(body.question_id)
    if id_errors:
        return _validation_response(id_errors)
    question_id: str = body.question_id

    if body.confirm is not None and not isinstance(body.confirm, bool):
        return _validation_response([{
            "code": "confirm_invalid", "field": "confirm",
            "message": "confirm must be a boolean.",
        }])
    confirm = bool(body.confirm)
    version = body.version

    row = await _load_for_edit(job_id, user_id)
    data = row.get("assessment_data") or {}

    if dry_run:
        preview = preview_question_delete(data, question_id)
        preview["job_id"] = job_id
        preview["version"] = int(row.get("version") or 1)
        preview["dry_run"] = True
        return preview

    try:
        expected = _expected_version(row, version, if_match)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    try:
        result = apply_question_delete(
            data, question_id, editor_id=user_id, confirmed=confirm
        )
    except ValidationError as exc:
        codes = {e["code"] for e in exc.errors}
        if "question_not_found" in codes:
            return _validation_response(exc.errors, status_code=404)
        _emit_validation_failure(job_id, user_id, row, "delete_question", exc.errors,
                                 question_id=question_id)
        logger.info(f"[{job_id}] Delete blocked | question={question_id} | codes={codes}")
        return _validation_response(exc.errors)

    try:
        new_version = await _commit(job_id, user_id, expected, result,
                                    operation="delete_question",
                                    question_id=question_id)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    response = _saved_payload(job_id, new_version, result.assessment_data, result)
    response["deleted_question_id"] = question_id
    return response


@api_v1_router.post(
    "/questions/order/{job_id}",
    summary="Reorder questions",
)
async def reorder_questions_v1(
    job_id: str,
    payload: QuestionReorderRequest,
    user_id: str = Depends(get_current_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Re-sequence the assessment, either by sending the complete new
    `questionOrder`, or by moving one question with `questionId` + `position`
    — the form a keyboard reorder produces.

    `announcement` in the response is ready to place in an ARIA live region so
    screen-reader users hear the result of the move.
    """
    if payload.request is None:
        return _missing_request_response()
    body = payload.request

    row = await _load_for_edit(job_id, user_id)
    data = row.get("assessment_data") or {}

    try:
        expected = _expected_version(row, body.version, if_match)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    try:
        result = apply_question_reorder(
            data, editor_id=user_id,
            question_order=body.question_order,
            question_id=body.question_id,
            position=body.position,
        )
    except ValidationError as exc:
        _emit_validation_failure(job_id, user_id, row, "reorder_questions", exc.errors)
        logger.info(f"[{job_id}] Reorder blocked | "
                    f"errors={[e['code'] for e in exc.errors]}")
        return _validation_response(exc.errors)

    if not result.changed:
        return {
            "message": "Question order unchanged",
            "status": "COMPLETED",
            "job_id": job_id,
            "version": int(row.get("version") or 1),
            "question_order": result.assessment_data.get("question_order", []),
            "announcement": result.announcement,
            "alerts": [],
        }

    try:
        version = await _commit(job_id, user_id, expected, result,
                                operation="reorder_questions")
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    return _saved_payload(job_id, version, result.assessment_data, result)


class TelemetryEventBody(_EnvelopeBody):
    event_code: str = Field(
        ..., alias="eventCode",
        description="Assessment Edit Opened · Question Edit Started · "
                    "Question Edit Cancelled · Assessment Reopened",
    )
    question_id: Optional[str] = Field(
        None, alias="questionId", description="Required for Question Edit Started and Question Edit Cancelled."
    )
    question_type: Optional[str] = Field(None, alias="questionType")
    question_position: Optional[int] = Field(None, alias="questionPosition")
    entry_point: Optional[str] = Field(
        None, alias="entryPoint",
        description="Assessment Edit Opened — where the user entered the editor from.",
    )
    source: Optional[str] = Field(
        None, description="Assessment Reopened — e.g. 'Past Assessment'."
    )


class TelemetryEventRequest(BaseModel):
    request: Optional[TelemetryEventBody] = Field(
        None, description="Sunbird request envelope."
    )


@api_v1_router.post(
    "/telemetry/{job_id}",
    summary="Report an editor lifecycle event (Assessment Edit Opened, Question Edit Started, Question Edit Cancelled, Assessment Reopened)",
)
async def report_telemetry_v1(
    job_id: str,
    payload: TelemetryEventRequest,
    user_id: str = Depends(get_current_user),
):
    """
    Report an event the backend cannot observe for itself.

    Opening the editing workspace, opening a question for editing, cancelling an
    edit and reopening a Past Assessment all happen entirely in the client — no
    write reaches the server, so no endpoint sees them. Reporting them is
    required, and metrics ("% Past Assessments reopened and edited", "average
    time from generation to final save") are built on them, so the client reports
    them here.

    Only the four editor-lifecycle codes are accepted. Every event that describes
    a write is emitted by the server itself and cannot be injected by a client.

    Cancelling an edit needs no other call: discarding the client's local state
    is what makes the change not persist, and this reports Question Edit Cancelled.
    """
    if payload.request is None:
        return _missing_request_response()
    body = payload.request

    row = await get_assessment_status(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if not _owns(row, user_id):
        raise HTTPException(
            status_code=403, detail="Access denied: you do not own this assessment"
        )

    code = body.event_code.strip().upper()
    event = telemetry.emit_ui_event(
        code,
        job_id=job_id,
        editor_id=user_id,
        assessment_version=int(row.get("version") or 1),
        question_id=body.question_id,
        question_type=body.question_type,
        question_position=body.question_position,
        entry_point=body.entry_point,
        source=body.source,
        assessment_type=((row.get("metadata") or {}).get("config") or {}).get(
            "assessment_type"
        ) if isinstance(row.get("metadata"), dict) else None,
    )

    if event is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{body.event_code}' is not a client-reportable event. "
                   f"Accepted: {', '.join(sorted(telemetry.UI_REPORTED_EVENTS))}. "
                   f"Events describing a write are emitted by the server.",
        )

    return {"recorded": True, "event_code": code,
            "event_name": event["event_name"], "job_id": job_id}


@api_v1_router.get(
    "/audit/{job_id}",
    summary="Audit trail of human changes",
)
async def get_audit_trail_v1(
    job_id: str,
    user_id: str = Depends(get_current_user),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    Every human change made to this assessment, oldest first — editor,
    timestamp, changed fields with previous and new values, added and deleted
    questions, sequence changes, and the assessment version each change
    produced.

    `ai_original` is the pristine AI-generated assessment, retained for audit.
    """
    row = await get_assessment_status(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if not _owns(row, user_id):
        raise HTTPException(
            status_code=403, detail="Access denied: you do not own this assessment"
        )

    entries = await get_audit_trail(job_id, limit=limit, offset=offset)
    for entry in entries:
        created = entry.get("created_at")
        entry["created_at"] = created.isoformat() if created else None

    return {
        "job_id": job_id,
        "version": int(row.get("version") or 1),
        "edited_at": row["edited_at"].isoformat() if row.get("edited_at") else None,
        "count": len(entries),
        "audit_trail": entries,
        "ai_original": normalize_assessment(row.get("ai_original_data"))
                       if row.get("ai_original_data") else None,
    }


@api_v1_router.put("/update/{job_id}")
async def update_assessment_v1(
    job_id: str,
    payload: AssessmentUpdate,
    user_id: str = Depends(get_current_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """
    Replace the whole assessment payload.

    Retained for backward compatibility, and now subject to the same rules as
    the granular endpoints: the payload is validated, the change is
    versioned and the difference against the stored copy is recorded in
    the audit trail.

    Prefer the granular endpoints — `POST /questions/update/{job_id}`,
    `POST /questions/create/{job_id}`, `POST /questions/delete/{job_id}` and
    `POST /questions/order/{job_id}` — which record the reviewer's actual intent
    instead of inferring it from a diff.
    """
    logger.info(f"[{job_id}] Whole-blob update request | user={user_id}")
    row = await _load_for_edit(job_id, user_id)

    try:
        expected = _expected_version(row, payload.version, if_match)
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    normalized, events = diff_assessments(
        row.get("assessment_data") or {}, payload.assessment_data, editor_id=user_id
    )

    # Validate the questions this save adds or changes. Untouched questions are
    # left alone so a pre-existing gap elsewhere in an older assessment cannot
    # block an unrelated edit — see `validate_assessment`.
    touched = {
        e["question_id"] for e in events
        if e.get("question_id")
        and e["event_code"] in (telemetry.TEL_QUESTION_EDIT_SAVED,
                                telemetry.TEL_QUESTION_ADDED)
    }
    # Which paths each question changed, so mapping vocabulary validation only
    # runs where the reviewer actually touched the mapping.
    edited_paths_by_question: Dict[str, set] = {}
    for e in events:
        if e.get("question_id") and e.get("changed_fields"):
            edited_paths_by_question.setdefault(e["question_id"], set()).update(
                c["field"] for c in e["changed_fields"]
            )
    errors = validate_assessment(
        normalized, enable_blooms=_blooms_enabled(row), only_question_ids=touched,
        edited_paths_by_question=edited_paths_by_question,
    )
    if errors:
        _emit_validation_failure(job_id, user_id, row, "bulk_update", errors)
        logger.info(f"[{job_id}] Whole-blob update blocked by validation | "
                    f"errors={[e['code'] for e in errors][:10]}")
        return _validation_response(errors)

    result = EditResult(assessment_data=normalized, events=events)

    try:
        version = await _commit(job_id, user_id, expected, result,
                                operation="bulk_update")
    except _Conflict as conflict:
        return _conflict_response(job_id, conflict.current_version)

    return {
        "message": "Assessment updated successfully",
        "status": "COMPLETED",
        "job_id": job_id,
        "version": version,
        "question_order": normalized.get("question_order", []),
        "total_questions": question_count(normalized),
        "changes_recorded": len(result.audit_rows),
    }

SUPPORTED_FORMATS = {"csv", "csv_basic", "json", "pdf", "docx"}

@api_v1_router.get(
    "/download/{job_id}",
    summary="Download Assessment",
    description=(
        "Download a completed assessment in the specified format.\n\n"
        "**Supported formats:** `csv`, `json`, `pdf`, `docx`\n\n"
        "**Authentication:** Pass JWT via `x-authenticated-user-token` header.\n\n"
        "**Ownership:** Only the user who generated the assessment can download it.\n\n"
        "**Examples:**\n"
        "- `GET /ai-assessments/v1/download/{job_id}?format=csv`\n"
        "- `GET /ai-assessments/v1/download/{job_id}?format=json`\n"
        "- `GET /ai-assessments/v1/download/{job_id}?format=pdf`\n"
        "- `GET /ai-assessments/v1/download/{job_id}?format=docx`"
    )
)
async def download_assessment_v1(
    job_id: str,
    format: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user)
):
    logger.info(f"[{job_id}] Download request | format={format} | user={user_id}")
    if format not in SUPPORTED_FORMATS:
        logger.warning(f"[{job_id}] Download rejected — invalid format={format} | user={user_id}")
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format '{format}'. Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )

    data = await get_assessment_status(job_id)
    if not data or data['status'] != 'COMPLETED':
        logger.warning(f"[{job_id}] Download rejected — job not ready or not found | status={data.get('status') if data else 'NOT_FOUND'} | user={user_id}")
        raise HTTPException(status_code=404, detail="Assessment not ready or found")

    if data.get('user_id') != user_id:
        logger.warning(f"[{job_id}] Download rejected — access denied | requester={user_id} | owner={data.get('user_id')}")
        raise HTTPException(status_code=403, detail="Access denied: you do not own this assessment")

    # Every format is built from the persisted final assessment state,
    # never from the original AI-generation payload. Normalizing here supplies
    # `question_order` for assessments saved before the editing workspace
    # existed, so all formats share one sequence.
    assessment_json = normalize_assessment(data['assessment_data'])
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"assessment_{job_id}_"))
    background_tasks.add_task(shutil.rmtree, str(tmp_dir), True)
    logger.info(
        f"[{job_id}] Generating {format} export | version={data.get('version')} | "
        f"questions={question_count(assessment_json)} | user={user_id}"
    )
    telemetry.emit(telemetry.build_event(
        telemetry.TEL_ASSESSMENT_DOWNLOADED,
        job_id=job_id, editor_id=user_id,
        assessment_version=int(data.get("version") or 1),
        format=format, question_count=question_count(assessment_json),
    ))

    if format == "csv":
        path = tmp_dir / f"{job_id}_assessment_v2.csv"
        generate_csv_v2(assessment_json, path)
        return FileResponse(path, filename=f"{job_id}_assessment.csv", media_type="text/csv")

    elif format == "csv_basic":
        path = tmp_dir / f"{job_id}_assessment_basic.csv"
        generate_csv_basic(assessment_json, path)
        return FileResponse(path, filename=f"{job_id}_assessment_basic.csv", media_type="text/csv")

    elif format == "json":
        path = tmp_dir / f"{job_id}_assessment_v2.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(assessment_json, f, indent=2, ensure_ascii=False)
        return FileResponse(path, filename=f"{job_id}_assessment.json", media_type="application/json")

    elif format == "pdf":
        path = tmp_dir / f"{job_id}_assessment_v2.pdf"
        generate_pdf(assessment_json, path)
        return FileResponse(path, filename=f"{job_id}_assessment.pdf", media_type="application/pdf")

    elif format == "docx":
        path = tmp_dir / f"{job_id}_assessment_v2.docx"
        generate_docx(assessment_json, path)
        return FileResponse(path, filename=f"{job_id}_assessment.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

@api_v1_router.get("/history")
async def get_history_v1(user_id: str = Depends(get_current_user)):
    """
    Returns a list of all assessments previously generated or cloned by the authenticated user.
    """
    history = await get_user_assessments_history(user_id)
    
    formatted_history = []
    for item in history:
        meta = item.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        formatted_history.append({
            "job_id": item.get("job_id"),
            "status": item.get("status"),
            "created_at": item.get("created_at").isoformat() if item.get("created_at") else None,
            "updated_at": item.get("updated_at").isoformat() if item.get("updated_at") else None,
            "course_ids": meta.get("course_ids", []),
            "course_names": meta.get("course_names", []),
            "config": meta.get("config", {}),
            "error_message": item.get("error_message"),
            # Lets a listing show which assessments have been
            # reviewed without fetching each one's audit trail.
            "version": int(item.get("version") or 1),
            "edited": bool(item.get("edited_at")),
            "edited_at": item["edited_at"].isoformat() if item.get("edited_at") else None,
        })
        
    return formatted_history

app.include_router(api_v1_router)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    
    # WORKAROUND: Force 'files' to be binary array in Docs
    # ----------------------------------------------------
    try:
        paths = openapi_schema.get("paths", {})
        for path, methods in paths.items():
            if path.endswith("/v1/generate"):
                post = methods.get("post", {})
                content = post.get("requestBody", {},).get("content", {})
                multipart = content.get("multipart/form-data", {})
                schema = multipart.get("schema", {})
                
                # Check if schema is a reference
                if "$ref" in schema:
                    ref_name = schema["$ref"].split("/")[-1]
                    schema = openapi_schema.get("components", {}).get("schemas", {}).get(ref_name, {})
                
                properties = schema.get("properties", {})
                
                # Force File Picker Override
                properties["files"] = {
                    "type": "array",
                    "items": {"type": "string", "format": "binary"},
                    "title": "Files",
                    "description": "Upload Files"
                }
                logger.info(f"Forced 'files' schema override for path: {path}")
    except Exception as e:
        logger.warning(f"Failed to patch OpenAPI schema: {e}")

    app.openapi_schema = openapi_schema
    return openapi_schema

app.openapi = custom_openapi
