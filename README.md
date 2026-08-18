# AI Assessment Service

An AI-powered, audit-ready assessment generation service built for the **Karmayogi government learning platform (iGot)**. Given one or more course IDs (or uploaded files), it produces pedagogically sound assessments — MCQs, fill-in-the-blanks, match-the-following, and more — aligned to the Karmayogi Competency Model (KCM) and Bloom's Taxonomy, in any of 10+ Indian languages.

Powered by **Google Gemini (Vertex AI)**, **FastAPI**, **Kafka**, and **PostgreSQL**.

---

## Table of Contents

1. [What is This?](#1-what-is-this)
2. [System at a Glance](#2-system-at-a-glance)
3. [Why API and Worker are Separate](#3-why-api-and-worker-are-separate)
4. [How a Request Flows — Step by Step](#4-how-a-request-flows--step-by-step)
5. [Assessment Types](#5-assessment-types)
6. [Question Types & Bloom's Taxonomy](#6-question-types--blooms-taxonomy)
7. [Project Structure & File Guide](#7-project-structure--file-guide)
8. [Configuration (Environment Variables)](#8-configuration-environment-variables)
9. [Running Locally](#9-running-locally)
10. [API Reference](#10-api-reference)
11. [Observability — Langfuse](#11-observability--langfuse)
12. [Database Schema](#12-database-schema)
13. [Export Formats](#13-export-formats)
14. [Developer Notes & Gotchas](#14-developer-notes--gotchas)

---

## 1. What is This?

The AI Assessment Service turns course content into complete assessments **automatically**. A user (teacher, content creator, or platform admin) selects a course on the Karmayogi platform, chooses how many questions they want and of what types, and this service handles the rest:

1. Fetches the course's VTT subtitles and PDF materials from the Karmayogi Learning API
2. Sends the content to Google Gemini (a large language model) with detailed pedagogic instructions
3. Gets back a fully formed assessment — questions, correct answers, wrong options, answer rationales, Bloom's level, competency alignment, and a Blueprint summary
4. Saves it to the database and notifies the calling system

The result is available in JSON, CSV (for import into the LMS), PDF, and DOCX formats.

---

## 2. System at a Glance

```
┌─────────────────────────────────────────────────────────────────┐
│                      Karmayogi Platform                         │
│              (Web UI / Chatbot / Admin Portal)                  │
└───────────────────────────┬─────────────────────────────────────┘
                            │  HTTP POST /generate  (JWT auth)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                     API  (FastAPI :8000)                      │
│  • Validates request & JWT                                    │
│  • Checks DB for an existing matching job (cache/clone)       │
│  • Creates a new job record in PostgreSQL                     │
│  • Publishes a message to Kafka  →  returns job_id instantly  │
└────────────────────────────┬─────────────────────────────────┘
                             │  Kafka message
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                   Worker  (Consumer)                          │
│  • Picks up the Kafka message                                 │
│  • Fetches VTT / PDF from Karmayogi Learning API              │
│  • Calls Google Gemini with the prompt + course content       │
│  • Parses the LLM response into structured JSON               │
│  • Saves result to PostgreSQL                                 │
│  • Publishes ASSESSMENT_COMPLETED event to Kafka              │
└──────────┬──────────────────────────────────────┬────────────┘
           │                                      │
           ▼                                      ▼
  ┌───────────────────────────┐         ┌──────────────────────┐
  │        PostgreSQL         │         │  Karmayogi Learning  │
  │ (interactive_assessments) │         │  API  (VTT / PDF)    │
  └───────────────────────────┘         └──────────────────────┘
```

**Three moving pieces:**
| Piece | What it does | When it runs |
|---|---|---|
| **API** | Accepts requests, manages jobs, serves results | Always running |
| **Worker** | Does the heavy lifting — LLM calls | Triggered by Kafka messages |
| **Streamlit UI** | Internal test/demo UI | Optional, development use |

---

## 3. Why API and Worker are Separate

This is the most important architectural decision in the service. Understanding it is essential for any developer working here.

### The problem with doing it all in one process

Generating an assessment with an LLM takes **30 seconds to 5 minutes** depending on question count, model, and content length. If the API itself called the LLM:

- The HTTP request would have to stay open for up to 5 minutes → clients time out
- One slow job would block other users' requests (Python async or not)
- If the API pod restarts (deploy, crash), the in-flight LLM call is lost forever — no retry, silent failure
- You cannot scale the LLM processing independently from the HTTP serving layer

### The solution: decouple with Kafka

```
Client  ──► API  ──► Kafka  ──► Worker  ──► DB
   ▲                                        │
   │                                        │
   └───────────  poll /status ◄─────────────┘
```

1. **API** receives the request, validates it, writes a `PENDING` job to the DB, drops a message on Kafka, and returns `200 OK` with a `job_id` and `status: PENDING` **in under 100ms**. (The response is always `200`; the `status` field — not the HTTP code — tells the client whether work was queued or served from cache.)
2. **Client** polls `/status/{job_id}` every few seconds.
3. **Worker** is a completely separate process. It reads from Kafka, does all the slow work (fetch → LLM → parse → save), and writes the final result back to the DB.
4. When the client's next poll hits, the status is `COMPLETED` and the data is there.

### What this gives you

| Benefit | How |
|---|---|
| **No client timeouts** | API returns instantly; client polls |
| **Resilient to crashes** | Kafka retains the message; Worker retries on restart |
| **Independent scaling** | Run 1 API pod and 10 Worker pods if needed |
| **Concurrency** | Multiple Worker pods process different jobs in parallel |
| **LLM isolation** | API never imports google-genai; Worker never serves HTTP |
| **Observability** | Each process has its own log file and Langfuse traces |

### In Kubernetes

The API and Worker are deployed as **separate Deployments** built from separate Dockerfiles (`Dockerfile` vs `DockerfileWorker`) by the Jenkins pipelines. Locally, `docker-compose` builds **both** services from the root `Dockerfile` and varies only the `command` — the two Dockerfiles currently differ only in `EXPOSE` and `CMD`. They share only:
- The same PostgreSQL database
- The same Kafka broker
- The same GCS bucket (for file storage in multi-pod setups)

---

## 4. How a Request Flows — Step by Step

### Happy path (new job)

```
 User                API                Kafka             Worker              DB
  │                   │                   │                  │                 │
  │── POST /generate ─►│                   │                  │                 │
  │                   │── validate JWT     │                  │                 │
  │                   │── check DB cache  ─────────────────────────────────────►│
  │                   │   (no hit)         │                  │                 │
  │                   │── INSERT job ──────────────────────────────────────────►│
  │                   │   status=PENDING   │                  │                 │
  │                   │── publish msg ────►│                  │                 │
  │◄── 200 job_id ────│                   │                  │                 │
  │    status=PENDING │                   │                  │                 │
  │                   │                   │── deliver msg ──►│                 │
  │                   │                   │                  │── fetch VTT/PDF  │
  │                   │                   │                  │── call Gemini    │
  │                   │                   │                  │── parse JSON     │
  │                   │                   │                  │── UPDATE job ───►│
  │                   │                   │                  │   status=DONE    │
  │── GET /status ───►│                   │                  │                 │
  │                   │── SELECT job ──────────────────────────────────────────►│
  │◄── COMPLETED ─────│                   │                  │                 │
```

### Cache / clone hit (same params requested before)

If a job with the same hash (course IDs + assessment type + difficulty + question config) already exists as `COMPLETED` — even from a different user — the API **clones** it instantly to the requesting user's workspace and returns `200 COMPLETED` without touching Kafka or the Worker at all.

---

## 5. Assessment Types

| Type | Course Content | KCM Required | Description |
|---|---|---|---|
| `practice` | Required | Optional | Reinforcement assessment for a single course |
| `final` | Required | Optional | Summative/certification assessment for a single course |
| `comprehensive` | Required (multiple) | Optional | Cross-course assessment; supports per-course weightage |
| `standalone` | Uploaded files (PDF/VTT) | Optional | No course ID needed; content comes from uploaded files |
| `competency` | Optional | **Required** | Pure KCM-aligned; works with or without course content |

---

## 6. Question Types & Bloom's Taxonomy

### Question Types

| Code | Full Name | Notes |
|---|---|---|
| `mcq` | Multiple Choice (single correct) | 4 options, one correct |
| `ftb` | Fill in the Blank | Exact answer phrase |
| `mtf` | Match the Following | Left-right pairs |
| `multichoice` | Multiple Selection | Multiple correct options |
| `truefalse` | True / False | Binary |

### Bloom's Taxonomy

The service distributes questions across cognitive levels. The user specifies a percentage per level (must sum to 100); the service maps them proportionally to question counts across all active question types.

| Level | What it tests |
|---|---|
| Remember | Recall of facts |
| Understand | Comprehension, paraphrasing |
| Apply | Using knowledge in a scenario |
| Analyze | Breaking down, finding relationships |
| Evaluate | Judging, critiquing |
| Create | Synthesizing, producing something new |

---

## 7. Project Structure & File Guide

```
ai-assessment-service/
│
├── src/
│   └── assessment/                  ← the Python package
│       ├── api.py                   ← FastAPI app (API process)
│       ├── worker_service.py        ← Kafka consumer (Worker process)
│       ├── generator.py             ← LLM prompt engineering & parsing
│       ├── fetcher.py               ← Karmayogi Learning API client
│       ├── db.py                    ← PostgreSQL async operations
│       ├── auth.py                  ← JWT validation
│       ├── events.py                ← Kafka producer & consumer setup
│       ├── storage.py               ← Abstraction: local disk vs GCS
│       ├── tracing.py               ← Langfuse observability (opt-in)
│       ├── exporters.py             ← PDF & DOCX generation
│       ├── exporters_csv_v2.py      ← CSV export (7-option V2 + basic MCQ schema)
│       ├── cleanup.py               ← Scheduled deletion of old cached files
│       ├── config.py                ← All env var loading
│       └── resources/
│           ├── prompts.yaml         ← LLM system & user prompt templates
│           ├── schemas.json         ← JSON schema for LLM output validation
│           ├── competencies.json    ← KCM index: 2 areas / 35 themes / 112 sub-themes
│           ├── kcm_descriptions.json← Full KCM text, 109 entries (~60k tokens, Gemini-cached)
│           └── fonts/               ← Noto Sans fonts for Indian language PDFs
│
├── ui/
│   └── app.py                       ← Streamlit internal test UI
│
├── scripts/
│   └── verify_env.py                ← Pre-flight env check
│
├── Dockerfile                       ← API container image
├── DockerfileWorker                 ← Worker container image (separate!)
├── docker-compose.yml               ← Full local stack
├── pyproject.toml                   ← Python deps (managed with uv)
├── .env.example                     ← Env var template (never commit .env)
├── architecture.md                  ← Detailed technical architecture
└── DEPLOYMENT.md                    ← Production deployment guide
```

### File-by-file breakdown

#### `api.py` — The HTTP layer
The FastAPI application. Handles **everything HTTP** but **never calls the LLM**.

- Defines all REST endpoints under `/ai-assessments/v1/`
- Validates JWT tokens via `auth.py`
- Checks PostgreSQL for an existing matching job (cache/clone logic)
- Creates a new job record and publishes to Kafka
- Serves status, history, and download endpoints
- **Does not import google-genai** — no LLM code here

#### `worker_service.py` — The background processor
The Kafka consumer. Runs as a completely separate process.

- Reads messages from the Kafka `assessment.request` topic
- Orchestrates the full generation pipeline:
  1. Fetches course content via `fetcher.py`
  2. Calls `generator.py` for LLM generation
  3. Saves results to DB via `db.py`
  4. Publishes a completion event back to Kafka
- Wraps each job in a Langfuse trace (via `tracing.py`) for observability

#### `generator.py` — The LLM brain
The most complex file. Contains all prompt engineering logic.

- Builds the full LLM prompt: system instructions + KCM context + course content + user config
- Handles Bloom's taxonomy distribution across question types (proportional round-robin)
- Sends the prompt to Google Gemini via `client.aio.models.generate_content`
- Parses and validates the structured JSON response
- Handles Gemini context caching for the KCM descriptions (~60k tokens)
- Returns `(metadata, assessment_data, usage_stats)`

#### `fetcher.py` — Course content retrieval
Fetches all content for a given course from the Karmayogi platform.

- Calls the Learning AI API to get VTT transcript download URLs
- Calls the Karmayogi Search API to get course metadata (name, description)
- Downloads VTT subtitle files and PDF materials
- Stores fetched content locally (or in GCS if `DOCUMENT_STORAGE_TYPE=gcs`)
- Caches on disk — subsequent requests for the same course skip the network call

#### `db.py` — Database operations
All PostgreSQL interactions via `asyncpg` (async driver).

- `create_job()` — inserts a new PENDING job
- `get_assessment_status()` — reads job status + result
- `save_assessment_result()` — writes completed assessment data
- `update_job_status()` — updates status (IN_PROGRESS, FAILED, etc.)
- `create_completed_job()` — clones an existing result to a new user
- `get_user_assessments_history()` — all jobs for a user
- All operations use connection pooling via `asyncpg.create_pool`

#### `auth.py` — JWT validation
Validates the `x-authenticated-user-token` header on every API request.

- Fetches the JWKS (public keys) from the Sunbird SSO endpoint
- Verifies the JWT signature, expiry, and issuer
- Extracts and returns the `user_id` (used as the owner identity for all DB records)
- Raises `401 Unauthorized` for missing/invalid tokens
- Can be bypassed in development with `DISABLE_AUTH_VERIFICATION=true`

#### `events.py` — Kafka plumbing
Kafka producer and consumer configuration using `aiokafka`.

- `send_request_event()` — API calls this to publish a new job to the Worker
- `send_completion_event()` — Worker calls this when a job finishes
- `get_kafka_consumer()` — returns the configured consumer for the Worker
- Topic names and broker address come from env vars

#### `storage.py` — File storage abstraction
Abstracts away where files live — local disk or Google Cloud Storage.

- `LocalStorageService` — stores files in `./interactive_courses_data/` (default, single-node)
- `GCSStorageService` — stores files in a GCS bucket (Kubernetes / multi-pod deployments)
- `get_storage_service()` — factory that returns the right one based on `DOCUMENT_STORAGE_TYPE`
- Used by both the API (for uploaded files) and the Worker (for fetched course content)

#### `tracing.py` — LLM observability
Langfuse integration for tracking every LLM call. **Zero overhead when disabled.**

- Monkeypatches `google.genai.models.AsyncModels.generate_content` at Worker startup — every LLM call is auto-captured without any per-call-site code
- `init()` — connects to Langfuse and applies the monkeypatch (no-op when `LANGFUSE_ENABLED=false`)
- `set_identity()` — attaches `user_id` and `session_id` to all LLM spans in the current task
- `trace()` — context manager grouping all LLM calls for one job under a single root trace
- `record_gemini_usage()` — extracts token counts (input / output / thinking / cached / total) from each response
- See [Observability section](#11-observability--langfuse) for details

#### `exporters.py` — PDF and DOCX generation
Converts the assessment JSON into downloadable documents.

- PDF via WeasyPrint (supports all Indian scripts via bundled Noto Sans fonts)
- DOCX via python-docx
- Handles all question types and their special formatting (e.g. MTF pairs)

#### `exporters_csv_v2.py` — CSV export
Generates CSV files in the iGot platform's 7-option import schema.

- `generate_csv_v2()` — full 7-option schema with `QuestionType` / `QuestionTagging` columns, all question types, `Yes`/`No` correctness values
- `generate_csv_basic()` — 6-option schema, **MCQ only** (single- and multi-answer); FTB, MTF and True/False are skipped entirely. Columns are `SR`, `Question`, `Option1`–`Option6`, `IsOption1Correct`–`IsOption6Correct`, with `TRUE`/`FALSE` correctness values

#### `cleanup.py` — Scheduled maintenance
APScheduler job that runs daily and **deletes files from disk — it does not touch the database**. It scans the top level of `INTERACTIVE_COURSES_PATH` and removes any entry whose mtime is older than `CLEANUP_RETENTION_DAYS` (default: 7 days), reclaiming cached course content. Job rows in PostgreSQL are never deleted. Schedule configured via `CLEANUP_SCHEDULE_HOUR` / `CLEANUP_SCHEDULE_MINUTE` (default: 03:00).

#### `config.py` — Environment variable registry
Single source of truth for all configuration. Loads `.env` at import time. If you add a new env var anywhere, add it here first.

#### `resources/prompts.yaml` — LLM prompts
All system and user prompt templates. Versioned (`version` key). Changing prompts here changes LLM output behaviour for all new jobs — do not edit without testing against all assessment types.

#### `resources/schemas.json` — LLM output schema
JSON schema that constrains the LLM's output format. Passed to Gemini as the `response_schema` to enforce structured JSON. Changing this requires matching changes in `generator.py`'s parsing logic.

#### `resources/kcm_descriptions.json` — KCM competency data
Full text of the Karmayogi Competency Model — 109 entries (~60k tokens), each with `Label`, `Area`, `Description` and `Levels`. This file is loaded once and cached in Gemini's context cache to avoid re-sending it with every request. (The separate `competencies.json` is the smaller structured index: 2 areas → 35 themes → 112 sub-themes, injected into the prompt directly.)

---

## 8. Configuration (Environment Variables)

Copy `.env.example` → `.env` and fill in values. **Never commit `.env`.**

| Variable | Used By | Description |
|---|---|---|
| `DATABASE_URL` | Both | `postgresql://user:pass@host:port/db` |
| `KARMAYOGI_API_KEY` | Both | `Bearer <jwt>` for Karmayogi platform API |
| `KARMAYOGI_BASE_URL` | Both | Platform base URL |
| `LEARNING_AI_BASE_URL` | Worker | Learning AI API for VTT/PDF fetch |
| `SUNBIRD_SSO_URL` | API | SSO base URL for JWKS endpoint |
| `SUNBIRD_SSO_REALM` | API | Keycloak realm name |
| `REQUIRED_ROLE` | API | JWT role required to use the service |
| `DISABLE_AUTH_VERIFICATION` | API | `true` to bypass JWT check (dev only) |
| `KAFKA_BOOTSTRAP_SERVERS` | Both | `localhost:29092` (local) or `kafka:9092` (Docker) |
| `KAFKA_REQUEST_TOPIC` | Both | Topic for generation requests |
| `KAFKA_TOPIC` | Both | Topic for completion events |
| `KAFKA_GROUP_ID` | Worker | Consumer group ID (default `assessment_worker_group`) |
| `GOOGLE_PROJECT_ID` | Worker | GCP project ID |
| `GOOGLE_LOCATION` | Worker | Vertex AI region (e.g. `us-central1`) |
| `GENAI_MODEL_NAME` | Worker | Gemini model (e.g. `gemini-2.5-pro`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Worker | Path to Vertex AI service account JSON |
| `DOCUMENT_STORAGE_TYPE` | Both | `local` (default) or `gcs` |
| `GCS_CREDENTIALS` | Both (GCS only) | Path to GCS service account JSON |
| `GCS_BUCKET_NAME` | Both (GCS only) | GCS bucket name |
| `GCS_UPLOAD_PREFIX` | Both (GCS only) | Prefix for user-uploaded files (default `ai-assessments/uploads`) |
| `GCS_COURSE_CONTENT_PREFIX` | Worker (GCS only) | Prefix for fetched course VTT/PDF/metadata (default `ai-assessments/course-content`) |
| `GCS_OUTPUT_PREFIX` | — | Loaded by `config.py` but currently unused |
| `INTERACTIVE_COURSES_PATH` | Both | Course content cache **and** the local storage root — the API writes uploads and runs cleanup here too |
| `LANGFUSE_ENABLED` | Worker | `true` to enable LLM tracing |
| `LANGFUSE_PUBLIC_KEY` | Worker | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | Worker | Langfuse project secret key |
| `LANGFUSE_HOST` | Worker | Langfuse host URL |
| `LANGFUSE_SAMPLE_RATE` | Worker | Fraction of traces to send (0.0–1.0) |
| `CLEANUP_RETENTION_DAYS` | API | Days before old cached **files** are deleted from disk (default `7`) |
| `CLEANUP_SCHEDULE_HOUR` | API | Hour the daily cleanup runs (default `3`) |
| `CLEANUP_SCHEDULE_MINUTE` | API | Minute the daily cleanup runs (default `0`) |

---

## 9. Running Locally

### Prerequisites
- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose
- GCP service account JSON with Vertex AI access
- Karmayogi platform API key (JWT)

### Setup
```bash
# 1. Clone the repo
git clone <repo-url> && cd ai-assessment-service

# 2. Create your .env
cp .env.example .env
# Fill in all required values in .env

# 3. Place credentials
cp /path/to/your/vertex-ai-key.json ./credentials.json

# 4. Install Python deps
uv sync
```

### Option A — Full Docker stack (simplest)
```bash
docker-compose up --build
```
Everything starts: PostgreSQL, Kafka, Zookeeper, API, Worker, Streamlit UI.

| Service | URL |
|---|---|
| API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| Streamlit UI | http://localhost:8501 |

### Option B — Local code + Docker infra (for development)
Run infra in Docker, code locally with hot-reload:

```bash
# 1. Start infra only
docker-compose up -d db kafka zookeeper

# 2. Start API (separate terminal)
export PYTHONPATH=$PYTHONPATH:$(pwd)/src
uv run uvicorn assessment.api:app --reload --port 8000

# 3. Start Worker (separate terminal)
export PYTHONPATH=$PYTHONPATH:$(pwd)/src
uv run python -m assessment.worker_service

# 4. (Optional) Start UI (separate terminal)
uv run streamlit run ui/app.py
```

> **Important**: The API and Worker are separate processes. If you change code in `generator.py` or `worker_service.py`, you must restart the Worker process manually — it does not hot-reload.

---

## 10. API Reference

**Base URL**: `/ai-assessments/v1`  
**Auth**: All endpoints require `x-authenticated-user-token: <JWT>` header.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/generate` | Start an assessment generation job |
| `GET` | `/status/{job_id}` | Poll job status and get result |
| `PUT` | `/update/{job_id}` | Edit a completed assessment |
| `GET` | `/history` | All jobs by the authenticated user |
| `GET` | `/download/{job_id}?format=<fmt>` | Download result (csv/json/pdf/docx) |

### POST /generate

Key form fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `assessment_type` | enum | ✅ | `practice`, `final`, `comprehensive`, `standalone`, `competency` |
| `difficulty` | enum | ✅ | `beginner`, `intermediate`, `advanced` |
| `total_questions` | int | ✅ | Total question count |
| `question_type_counts` | JSON | ✅ | `{"mcq": 5, "ftb": 5, "mtf": 0, "multichoice": 0, "truefalse": 0}` |
| `course_ids` | string | ✅* | Comma-separated course IDs (\*not needed for `standalone`/`competency`) |
| `course_names` | string | ❌ | Names matching `course_ids` order — prevents N/A in history |
| `language` | enum | ❌ | Default: `english`. Options: `hindi`, `tamil`, `telugu`, etc. |
| `enable_blooms` | bool | ❌ | Default: `true`. Enable Bloom's distribution |
| `blooms_config` | JSON | ❌ | `{"remember": 20, "understand": 30, ...}` — must sum to 100 |
| `force` | bool | ❌ | `true` bypasses cache and forces new generation |
| `files` | file | ✅* | PDF/VTT files (\*required for `standalone`) |

**All `/generate` responses are `200 OK`** — branch on the `status` field, not the HTTP status code.

**Response — new job queued:**
```json
{ "message": "Generation started (Queued)", "status": "PENDING", "job_id": "abc123" }
```

**Response — cache hit (this user already has it):**
```json
{ "message": "Assessment retrieved from cache", "status": "COMPLETED", "job_id": "abc123", "result": { ... } }
```

**Response — clone (another user had the same params):**
```json
{ "message": "Assessment cloned from cache", "status": "COMPLETED", "job_id": "abc123", "result": { ... } }
```

**Response — already running:**
```json
{ "message": "Assessment generation in progress", "status": "IN_PROGRESS", "job_id": "abc123" }
```

> **Note**: the completed assessment is returned under `result` here, but under `assessment_data` by `GET /status/{job_id}`. The two keys carry the same object.

### GET /status/{job_id}

**Possible statuses**: `PENDING` → `IN_PROGRESS` → `COMPLETED` / `FAILED`

Poll every 5–10 seconds. When `COMPLETED`, response includes:
- `assessment_data` — full assessment object (blueprint + questions)
- `metadata.config` — all generation parameters
- `metadata.content_availability` — which VTT/PDF files were found per course

### GET /download/{job_id}?format=

Supported formats: `csv`, `csv_basic`, `json`, `pdf`, `docx`

**Important**: Do not use `<a href>` with the token in the URL — it leaks JWTs in server logs. Use the Fetch + Blob pattern:
```js
const res = await fetch(`/ai-assessments/v1/download/${jobId}?format=csv`, {
  headers: { 'x-authenticated-user-token': token }
});
const blob = await res.blob();
const url = URL.createObjectURL(blob);
// ... trigger download
```

### Example cURL

```bash
# Generate
curl -X POST http://localhost:8000/ai-assessments/v1/generate \
  -H 'x-authenticated-user-token: YOUR_JWT' \
  -F 'course_ids=do_1144540583527301121908' \
  -F 'course_names=Foundations of Public Policy' \
  -F 'assessment_type=practice' \
  -F 'difficulty=intermediate' \
  -F 'total_questions=10' \
  -F 'question_type_counts={"mcq": 5, "ftb": 5}' \
  -F 'enable_blooms=true' \
  -F 'blooms_config={"remember": 20, "understand": 30, "apply": 30, "analyze": 20}'

# Poll status
curl http://localhost:8000/ai-assessments/v1/status/YOUR_JOB_ID \
  -H 'x-authenticated-user-token: YOUR_JWT'
```

---

## 11. Observability — Langfuse

The Worker integrates with **Langfuse** for LLM observability. When enabled, every Gemini call is automatically traced with full input/output and token counts — no per-call-site code.

### What gets traced

Every `generate_content` call is auto-captured via a monkeypatch applied at Worker startup (`tracing.init()`). For each call:

| Field | Captured |
|---|---|
| Model | `gemini-2.5-pro` (from env) |
| Input | Full prompt (VTT/course text included, no truncation) |
| Output | Full JSON assessment response |
| Tokens | Input / Output / Thinking / Cached / Total |
| Latency | Per-call duration |
| User | `user_id` from the job payload |
| Session | `{user_id}:{job_id}` — groups all calls for one job |

### What you see in the Langfuse dashboard

- **Traces** — one per assessment job, with nested generation spans for each LLM call
- **Users** — click any `user_id` to see all their assessments
- **Sessions** — all LLM calls for one job grouped together
- **Overview** — total traces, latency percentiles, token volume, error rate over time

### Enabling it

Add to `.env` (never commit real keys):
```env
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_SAMPLE_RATE=1.0
```

Restart the Worker. Traces appear immediately on next generation job.

> **Note**: `LANGFUSE_HOST` is the variable name (not `LANGFUSE_BASE_URL`). The API does **not** need Langfuse configured — tracing is Worker-only, since all LLM calls happen there.

---

## 12. Database Schema

Single table: `interactive_assessments`, created by `db.py` at startup (`CREATE TABLE IF NOT EXISTS`).

| Column | Type | Description |
|---|---|---|
| `course_id` | TEXT PRIMARY KEY | **The job ID**, despite the column name — format `{base_id}_{param_hash}_{user_id}` |
| `user_id` | TEXT | Owner (from JWT). Nullable, for v1 compatibility |
| `status` | TEXT | `PENDING` / `IN_PROGRESS` / `COMPLETED` / `FAILED` |
| `created_at` | TIMESTAMP | Job creation time (timezone-naive) |
| `updated_at` | TIMESTAMP | Last status change (timezone-naive) |
| `metadata` | JSONB | Config, course IDs/names, content_availability |
| `assessment_data` | JSONB | Full LLM output (blueprint + questions) |
| `token_usage` | JSONB | Token counts from Gemini (prompt / candidates / thoughts / total) |
| `error_message` | TEXT | Set on FAILED jobs |

> **Watch the column names.** The primary key is `course_id` but it stores the full composite job ID, not a bare Karmayogi course ID — legacy naming from v1. `GET /history` aliases it back with `SELECT course_id AS job_id`, which is why the API speaks `job_id` while the table stores `course_id`. Likewise the token column is `token_usage`, not `usage`. `GET /status/{job_id}` returns the raw row, so its response carries `course_id` and `token_usage` too.

### Useful queries

```sql
-- All jobs by status
SELECT status, COUNT(*) FROM interactive_assessments GROUP BY status;

-- Recent completed jobs with course and config info
SELECT
  course_id AS job_id,
  user_id,
  created_at,
  metadata->>'course_names'              AS course_names,
  metadata->'config'->>'assessment_type' AS type,
  metadata->'config'->>'difficulty'      AS difficulty,
  metadata->'config'->>'total_questions' AS questions,
  token_usage->>'total_token_count'      AS total_tokens
FROM interactive_assessments
WHERE status = 'COMPLETED'
ORDER BY created_at DESC
LIMIT 20;

-- Failed jobs with error
SELECT course_id AS job_id, user_id, created_at, error_message
FROM interactive_assessments
WHERE status = 'FAILED'
ORDER BY created_at DESC;
```

---

## 13. Export Formats

| Format | Endpoint param | Contents |
|---|---|---|
| `json` | `format=json` | Raw LLM output — blueprint + all question types |
| `csv` | `format=csv` | iGot 7-option import schema with QuestionTagging (all types), `Yes`/`No` correctness |
| `csv_basic` | `format=csv_basic` | 6-option schema, **MCQ only** (single + multi answer). FTB / MTF / True-False rows are omitted. `TRUE`/`FALSE` correctness, no QuestionType or QuestionTagging columns |
| `pdf` | `format=pdf` | Formatted PDF with Indian language font support |
| `docx` | `format=docx` | Word document |

---

## 14. Developer Notes & Gotchas

### Worker must be restarted after code changes
The Worker is a long-running process. Unlike the API (which can hot-reload), the Worker does not detect file changes. After editing `generator.py`, `fetcher.py`, or `worker_service.py`, restart the Worker manually.

### Bloom's config — lowercase keys
The API accepts Bloom's keys in any case (`remember`, `Remember`, `REMEMBER`) and normalises them internally to title-case for computation. The status endpoint returns lowercase for consistency. Do not hard-code title-case in client code.

### Course names — always pass them
If a course's metadata is not found via the Karmayogi API (404), the course name in the history response falls back to `N/A`. Pass `course_names` matching the order of `course_ids` in every `/generate` request to avoid this.

### gemini-3.5-flash requires higher token limit
The gemini-3.5-flash model is a thinking model. Set `max_output_tokens=8192` or higher, otherwise the model exhausts tokens during its reasoning phase and returns `None`.

### `DOCUMENT_STORAGE_TYPE=local` vs `gcs`
- `local` — files stored on the pod's filesystem. Only works in single-pod / Docker Compose setups. If API and Worker are in different pods, they cannot share local files.
- `gcs` — files stored in GCS. Required for Kubernetes where API and Worker pods may run on different nodes.

### Kafka topic names
Two topics are used:
- `assessment.request` — API → Worker (job requests)
- `assessment.lifecycle.events` — Worker → Platform (completion notifications)

Both are configured via env vars; defaults are in `.env.example`.

### JWT leaking
Never include the JWT token in a URL query string or `<a href>`. Use the `x-authenticated-user-token` header and the Fetch API for downloads. URLs are logged by every proxy, CDN, and browser in between.
