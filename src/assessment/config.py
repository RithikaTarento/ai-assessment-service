import os
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv

# Robustly find the .env file relative to this file's location
# Found in root
ROOT_DIR = Path(__file__).parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

# API Configuration
KARMAYOGI_API_KEY = os.getenv("KARMAYOGI_API_KEY")  # Expected format: "Bearer <token>"
KARMAYOGI_BASE_URL = os.getenv("KARMAYOGI_BASE_URL", "https://igotkarmayogi.gov.in")
LEARNING_AI_BASE_URL = os.getenv("LEARNING_AI_BASE_URL", "https://learning-ai.prod.karmayogibharat.net")

# Derived API endpoints
SEARCH_API_URL = f"{KARMAYOGI_BASE_URL}/api/content/v1/search"
TRANSCODER_STATS_URL = f"{LEARNING_AI_BASE_URL}/api/kb-pipeline/v3/transcoder/stats"

# SSO / Auth Configuration
SSO_URL = os.getenv("SUNBIRD_SSO_URL")
SSO_REALM = os.getenv("SUNBIRD_SSO_REALM")
REQUIRED_ROLE = os.getenv("REQUIRED_ROLE", "AI_ASSESSMENT_CREATOR")

# Derived: JWKS endpoint built from SSO_URL + realm
JWKS_URL = f"{SSO_URL}realms/{SSO_REALM}/protocol/openid-connect/certs" if SSO_URL and SSO_REALM else None

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://myuser:mypassword@localhost:5432/karmayogi_db")

# Paths
# Store data in the root directory's interactive_courses_data folder (or custom path)
default_courses_path = os.path.join(ROOT_DIR, "interactive_courses_data")
INTERACTIVE_COURSES_PATH = os.getenv("INTERACTIVE_COURSES_PATH", default_courses_path)

# Google GenAI
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
GOOGLE_LOCATION = os.getenv("GOOGLE_LOCATION", "us-central1")
GENAI_MODEL_NAME = os.getenv("GENAI_MODEL_NAME", "gemini-2.5-pro")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# ---------------------------------------------------------------------------
# Batched question generation
# ---------------------------------------------------------------------------
# A single LLM call degrades once it is asked for a large number of questions at
# once, so an assessment bigger than QUESTION_BATCH_SIZE is generated as several
# parallel calls and merged. Requests at or under the batch size keep taking the
# original single-call path unchanged.
#
# Deliberately NO cap on the total question count anywhere — the frontend owns
# that decision, as it did before batching existed.
QUESTION_BATCH_SIZE = int(os.getenv("QUESTION_BATCH_SIZE", "25"))

# In-flight LLM calls allowed process-wide. Batches run in parallel, so without
# a bound, concurrent batches across concurrent jobs exhaust the Vertex quota.
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "4"))

# Attempts per batch before the job is failed. One job is now several calls, so
# a single bad response must not lose the whole assessment.
BATCH_MAX_ATTEMPTS = int(os.getenv("BATCH_MAX_ATTEMPTS", "2"))

# Escape hatch: forces every request down the original single-call path.
ENABLE_QUESTION_BATCHING = os.getenv("ENABLE_QUESTION_BATCHING", "true").lower() == "true"

# Sampling temperature for batch calls. Defaults to the same 0.1 the single-call
# path uses, so behaviour is unchanged unless deliberately raised. Raising it is
# the available knob for question diversity across parallel batches, which all
# see identical content.
BATCH_TEMPERATURE = float(os.getenv("BATCH_TEMPERATURE", "0.1"))

# Option indexes on MCQ / Multi-Choice questions are zero-based — the convention
# resources/prompts.yaml states, resources/schemas.json documents and the UI
# assumes. The model still occasionally numbers its options 1..n, so
# `questions._rebase_option_indexes` shifts such a question back onto the
# convention at the single ingest point in worker_service.py, before the
# generated assessment is stored for the first time.
#
# The rebase is identity-preserving: it moves the option `index` values and
# `correct_option_index` together, so the same option stays correct. It is
# applied only when the question is provably one-based and self-consistent —
# never to a question whose answer key would move — and never to an assessment
# already in the database, so a stored assessment's base cannot change under a
# client that has already read it. Set to "false" to store fresh LLM output with
# whatever base it was generated on.
NORMALIZE_OPTION_INDEX_BASE = os.getenv("NORMALIZE_OPTION_INDEX_BASE", "true").lower() == "true"

# Per-question-type provisions. `None` means no limit; nothing enforces these
# yet. BATCH_SIZE_BY_TYPE overrides QUESTION_BATCH_SIZE for one type — useful
# because output volume per question differs sharply by type (an MTF with five
# pairs plus full reasoning is several times a True/False).
BATCH_SIZE_BY_TYPE: Dict[str, Optional[int]] = {
    "mcq": None,
    "ftb": None,
    "mtf": None,
    "multichoice": None,
    "truefalse": None,
}
MAX_QUESTIONS_PER_TYPE: Dict[str, Optional[int]] = {
    "mcq": None,
    "ftb": None,
    "mtf": None,
    "multichoice": None,
    "truefalse": None,
}

# Langfuse Observability (opt-in — set LANGFUSE_ENABLED=true to activate)
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
LANGFUSE_SAMPLE_RATE = float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))

# Storage backend for standalone upload file sharing between API and Worker pods
# DOCUMENT_STORAGE_TYPE: "local" (default, single-node) | "gcs" (multi-pod / Kubernetes)
DOCUMENT_STORAGE_TYPE = os.getenv("DOCUMENT_STORAGE_TYPE", "local")
GCS_CREDENTIALS = os.getenv("GCS_CREDENTIALS")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")
GCS_UPLOAD_PREFIX = os.getenv("GCS_UPLOAD_PREFIX", "ai-assessments/uploads")
GCS_COURSE_CONTENT_PREFIX = os.getenv("GCS_COURSE_CONTENT_PREFIX", "ai-assessments/course-content")
GCS_OUTPUT_PREFIX = os.getenv("GCS_OUTPUT_PREFIX", "ai-assessments/outputs")

# Headers for Karmayogi API
if not KARMAYOGI_API_KEY:
    raise RuntimeError("Missing mandatory env var: KARMAYOGI_API_KEY")

API_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'authorization': KARMAYOGI_API_KEY,
    'org': 'dopt',
    'rootorg': 'igot',
    'locale': 'en',
}

# Load Prompt Version
import yaml
PROMPTS_PATH = Path(__file__).parent / "resources" / "prompts.yaml"
try:
    # Explicit encoding: prompts.yaml contains typographic quotes, so on a
    # platform whose default is not UTF-8 (cp1252 on Windows) the read raises and
    # PROMPT_VERSION silently falls back to "Unknown" — which then lands in the
    # blueprint and every export. generator.load_yaml already reads it as UTF-8.
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        _prompts = yaml.safe_load(f)
        PROMPT_VERSION = _prompts.get("version", "3.0")
except Exception as e:
    print(f"Warning: Could not load prompt version: {e}")
    PROMPT_VERSION = "Unknown"
