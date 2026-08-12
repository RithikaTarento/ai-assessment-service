import os
import shutil
import tempfile
import logging
import json
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException, APIRouter
from fastapi.responses import FileResponse, JSONResponse
from fastapi.openapi.utils import get_openapi
from contextlib import asynccontextmanager

from .db import init_db, close_db, create_job, get_assessment_status, find_job_by_prefix, create_completed_job, update_job_result, get_user_assessments_history
from .config import INTERACTIVE_COURSES_PATH
from .storage import get_storage_service
from .exporters import generate_pdf, generate_docx
from .cleanup import start_cleanup_scheduler, stop_cleanup_scheduler
from .events import stop_kafka_producer, send_request_event
from .exporters_csv_v2 import generate_csv_v2, generate_csv_basic

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
from typing import List, Optional, Dict

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
            t_data = template['assessment_data']
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

    return status

from pydantic import BaseModel
class AssessmentUpdate(BaseModel):
    assessment_data: Dict

@api_v1_router.put("/update/{job_id}")
async def update_assessment_v1(
    job_id: str, 
    payload: AssessmentUpdate, 
    user_id: str = Depends(get_current_user)
):
    """
    Updates the assessment result.
    Enforces that the user owns the assessment.
    """
    logger.info(f"[{job_id}] Update request | user={user_id}")
    success = await update_job_result(job_id, user_id, payload.assessment_data)
    if not success:
        logger.warning(f"[{job_id}] Update failed — not found or access denied | user={user_id}")
        raise HTTPException(status_code=404, detail="Assessment not found or you do not have permission to edit it")
    logger.info(f"[{job_id}] Update successful | user={user_id}")
    return {"message": "Assessment updated successfully", "status": "COMPLETED", "job_id": job_id}

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

    assessment_json = data['assessment_data']
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"assessment_{job_id}_"))
    background_tasks.add_task(shutil.rmtree, str(tmp_dir), True)
    logger.info(f"[{job_id}] Generating {format} export | user={user_id}")

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
            "error_message": item.get("error_message")
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
