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
│       ├── batching.py              ← Sequential batch planning/merging for large assessments
│       ├── questions.py             ← Editable question model: ids, ordering, provenance
│       ├── validation.py            ← Validation gate
│       ├── editing.py               ← Edit / add / delete / reorder operations + audit
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
- For assessments larger than `QUESTION_BATCH_SIZE`, branches into a batched path: plans and merges **sequential** calls via `batching.plan_batches`/`merge_batches`. Each call renders the same `system_prompt_template` a single call does, with its own counts, its slice of the Bloom's assignment and its share of the course counts, plus a list of the questions earlier batches produced so it can avoid repeating them
- Sends the prompt to Google Gemini via `client.aio.models.generate_content`
- Parses the structured JSON response (real field/answer/mapping validation lives in `validation.py`, applied on the editing endpoints, not here)
- Handles Gemini context caching for the KCM descriptions (~60k tokens)
- Returns `(metadata, assessment_data, usage_stats)`

#### `batching.py` — Sequential batch planning and merging
Splits an assessment bigger than `QUESTION_BATCH_SIZE` into several LLM calls made one after another (batched along the question-type axis, not by course) and merges the results back into one payload. Divides the three whole-assessment properties — type counts, Bloom's levels and course counts — once, before the first call, so the parts sum to the whole by construction. `summarize_for_dedup` produces the compact list of already-generated questions each subsequent batch is shown. Imported by `generator.py`.

#### `questions.py` — The editable question model
The LLM returns questions grouped into type buckets with no single sequence. This module
adds what the editing workspace needs without breaking that shape.

- `normalize_assessment()` — idempotent; guarantees a unique `question_id` and a valid `provenance` on every question, fills each option's `index`, and builds/repairs the top-level `question_order` array. It backfills assessments generated before these fields existed, which is why no data migration is needed
- `iter_questions_in_order()` — iterate the authoritative sequence; used by every exporter. There is deliberately no flat/position-annotated projection here: that is a presentation shape a client derives from `question_order` in a few lines
- `question_order` is the authoritative sequence; the type buckets remain the authoritative content store

#### `validation.py` — The validation gate
Nothing reaches the database until these rules pass, so a rejected save can never leave a
half-written assessment behind.

Covers the five limbs the specification names — question, answer, option, mapping and assessment-level.

- `validate_question()` — field-level rules. MCQ and Multi-Choice require **at least 2 options**, and **at most 5 when the question is being added** (`is_new_question=True`; editing an existing question has no ceiling, so a generated question carrying more options stays editable); the correct answer must reference a real option `index`; text, rationale, Bloom's level and relevance are all checked
- `validate_mapping()` — the competency triple is all-or-nothing, and mapping fields cannot be blanked. The KCM **vocabulary** check (against `resources/competencies.json`) runs only when a competency field is actually edited, because generated questions occasionally carry a label that is not an exact dataset match and a blanket check would make unrelated edits impossible on those questions
- `validate_assessment()` — assessment-level invariants (at least one question, unique ids), plus per-question rules scoped to the questions a save actually touches, so a gap in an older question cannot block an unrelated edit
- `EDITABLE_FIELDS` — the per-type allowlist of editable dotted paths. Server-owned fields (`question_id`, `question_type`, `provenance`) are never client-writable
- Errors are `code` + `field` + `question_id` + a `params` bag, and carry **no message string** — the client maps the code to its own copy, because it owns the user's language and this service serves twelve of them
- `classify_option_change()` — distinguishes a pure re-sequencing of the options from a real content or answer-key edit. Retained server-side because the **audit trail** records the distinction (`options_reordered`, `reindexed_only`); it is not presentation

#### `editing.py` — Editing operations
Pure functions: current `assessment_data` in, new copy plus audit rows out. No database or
HTTP concerns, so the rules are identical no matter which endpoint drove the change.

- `apply_question_edit()`, `apply_question_add()`, `apply_question_delete()`, `apply_question_reorder()`
- `diff_assessments()` — derives the same audit rows from a before/after comparison, backing the legacy whole-blob `PUT`
- Enforces the provenance transitions: `ai_generated` → `ai_assisted` on first edit; manually added questions are `human_authored` and stay so however often they are edited

Audit events are declared in `AUDIT_EVENT_CODES` — the six kinds of change the audit
trail records. Only the code is stored; the display name below is this document's
wording, not a field, because the copy belongs to the client:

| Audit event |
|---|
| Question Edit Saved |
| Question Added |
| Question Deleted |
| Question Reordered |
| Correct Answer Changed |
| Mapping Updated |

Each one is persisted to `interactive_assessment_audit` in the same transaction as the
assessment update, so an audit row exists if and only if the change was saved. Nothing
about a user's activity is recorded anywhere else.

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
Source of truth for almost all configuration. Loads `.env` at import time. If you add a new env var anywhere, add it here first. Exception: `cleanup.py` reads `CLEANUP_RETENTION_DAYS`, `CLEANUP_SCHEDULE_HOUR` and `CLEANUP_SCHEDULE_MINUTE` directly via `os.getenv()`, bypassing this module.

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
| `QUESTION_BATCH_SIZE` | Worker | Assessments larger than this go through the batched (sequential-call) generation path (default `25`) |
| `BATCH_MAX_ATTEMPTS` | Worker | Retry attempts per batch before the run stops and keeps what succeeded (default `2`) |
| `KAFKA_MAX_POLL_INTERVAL_MS` | Worker | How long one job may take before the broker assumes the consumer died and redelivers the message (default `3600000`) |
| `ENABLE_QUESTION_BATCHING` | Worker | `false` forces every request down the original single-call path (default `true`) |
| `NORMALIZE_OPTION_INDEX_BASE` | Worker | `false` disables the one-based→zero-based option index rebase on ingest (default `true`) |
| `MAX_QUESTIONS_PER_TYPE` | Worker | Per-question-type question limit (code dict, not an env var; default: no limit for any type; not currently enforced) |
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
| `GET` | `/history` | All jobs by the authenticated user |
| `GET` | `/download/{job_id}?format=<fmt>` | Download result (csv/csv_basic/json/pdf/docx) |
| **Editing workspace** | | |
| `POST` | `/questions/create/{job_id}` | Add a question manually — returns `201` on success |
| `POST` | `/questions/update/{job_id}` | Edit one question in place — `questionId` in the body |
| `POST` | `/questions/delete/{job_id}` | Delete a question — `questionId` in the body |
| `POST` | `/questions/order/{job_id}` | Reorder questions — `questionOrder` array in the body |
| `GET` | `/audit/{job_id}` | Audit trail of all human changes |
| `PUT` | `/update/{job_id}` | Replace the whole assessment (legacy; prefer the granular endpoints) |

Every editing call is validated, versioned and audited. Send the assessment's `version`
(body field or `If-Match` header) so a concurrent update fails with `409` instead of
silently overwriting someone else's change.

These endpoints persist, validate and audit; they do not present. There is no `dry_run`,
no `confirm` flag, no `alerts` array, no `announcement`, and no message strings on
errors — impact warnings, confirmation dialogs, screen-reader copy and error wording all
belong to the client, which rendered the before-state and knows the user's language. See
`integration/API_INTEGRATION_GUIDE.md` → *What the client owns*.

**Path shape.** These endpoints are routed by a Kong `API` entity (Kong 0.10–0.14), which
can only prefix-match: it strips the matched prefix and appends the remaining path
verbatim upstream. It cannot reorder segments, and it cannot express a path parameter
followed by further segments. So every path is a **static verb prefix followed by
`job_id` as the single trailing segment**, and every other identifier travels in the
request body. The verb comes *before* `job_id`, so a job id can never collide with a
route name, and there is deliberately no bare `/questions` route — on a longest-prefix
router it would shadow all five verb routes.

`PATCH`, `PUT` and `DELETE` are therefore all `POST`: the identifiers moved into the
body, and a `GET` or `DELETE` must not carry one (semantics are undefined and
intermediate proxies may drop it).

Request bodies use the Sunbird envelope, `{"request": { ... }}`. Field names are
camelCase (`questionId`, `questionOrder`, `questionType`, `eventCode`); the snake_case
equivalents are also accepted. Responses are unchanged and unwrapped.

See [integration/API_INTEGRATION_GUIDE.md](integration/API_INTEGRATION_GUIDE.md) for the
full request/response contracts.

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
| `course_weightage` | JSON | ❌ | Comprehensive only. Maps course IDs to weightage % |
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

Two tables, both created by `db.py` at startup (`CREATE TABLE IF NOT EXISTS` plus additive `ADD COLUMN IF NOT EXISTS` migrations — no manual migration step).

### `interactive_assessments`

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
| `version` | INTEGER | Assessment version. `1` on generation, +1 per saved edit. Drives optimistic concurrency |
| `ai_original_data` | JSONB | The pristine AI-generated assessment, never modified after generation — retained for audit |
| `edited_at` | TIMESTAMP | First human edit, or NULL if never edited |

### `interactive_assessment_audit`

One row per human change. Written in the same transaction as the assessment update, so a
row exists if and only if the change was actually persisted.

| Column | Type | Description |
|---|---|---|
| `id` | BIGSERIAL PRIMARY KEY | Insertion order — also the chronological order |
| `job_id` | TEXT | The assessment this change belongs to |
| `assessment_version` | INTEGER | The version this change produced |
| `event_code` | TEXT | `TEL-03` Question Edit Saved · `TEL-05` Question Added · `TEL-06` Question Deleted · `TEL-07` Question Reordered · `TEL-10` Correct Answer Changed · `TEL-11` Mapping Updated |
| `editor_id` | TEXT | The user who made the change |
| `question_id` | TEXT | Affected question |
| `question_type` | TEXT | `mcq` / `ftb` / `mtf` / `multichoice` / `truefalse` |
| `previous_position` | INTEGER | Position before the change |
| `new_position` | INTEGER | Position after the change |
| `changed_fields` | JSONB | `[{field, previous_value, new_value}]` |
| `original_question` | JSONB | The AI-generated question, captured on its first human edit |
| `question_snapshot` | JSONB | The question as it stands after this change |
| `details` | JSONB | Provenance transition, answer-key-changed flag, and similar context |
| `created_at` | TIMESTAMP | When the change was made |

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

-- How many assessments have been reviewed after generation
SELECT
  count(*)                                       AS total,
  count(*) FILTER (WHERE edited_at IS NOT NULL)  AS edited,
  round(100.0 * count(*) FILTER (WHERE edited_at IS NOT NULL) / count(*), 1) AS pct_edited
FROM interactive_assessments
WHERE status = 'COMPLETED';

-- Most frequently edited fields
SELECT c->>'field' AS field, count(*) AS edits
FROM interactive_assessment_audit,
     jsonb_array_elements(changed_fields) AS c
WHERE event_code = 'TEL-03'   -- Question Edit Saved
GROUP BY 1 ORDER BY edits DESC;

-- Change history for one assessment
SELECT assessment_version, event_code, editor_id, question_id,
       previous_position, new_position, changed_fields, created_at
FROM interactive_assessment_audit
WHERE job_id = '<job_id>'
ORDER BY id;
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

All formats are generated from the **persisted final assessment** — edited, added and
deleted questions and the saved `question_order` are reflected in every one, and questions
appear in the same sequence across all of them. PDF and DOCX render a single ordered list
with the question type as a per-question label, rather than grouping questions under
per-type headings.

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

### Question order lives in `question_order`, not in the buckets
`assessment_data.questions` is a dict of type buckets — it stores content, not sequence. The authoritative order is the top-level `assessment_data.question_order` array of question IDs. Anything that presents, exports or publishes questions must go through `iter_questions_in_order()`; iterating the buckets directly will silently ignore every reorder the reviewer made.

### `normalize_assessment()` is a read-time projection
Assessments generated before the editing workspace have no `question_id`, no `provenance` and no `question_order`. `normalize_assessment()` backfills all three deterministically (in canonical bucket order, i.e. the order the exporters used previously), and is called on every read and every write. That is why there is no data migration for `assessment_data` — but it also means **nothing may depend on a question ID staying the same across a re-generation**. IDs are stable for a given stored payload, not across regenerations.

### Provenance and question IDs are server-owned
Clients naturally echo the whole question object back, so `question_id`, `question_type` and `provenance` are ignored rather than rejected on input. Never treat a client-supplied provenance as authoritative: `editing.py` derives it, and the whole-blob `PUT` path explicitly restores it from the stored copy.

### Cache cloning clones the pristine AI copy, not the edited one
`find_job_by_prefix()` matches on `course_id LIKE <prefix>% AND status = 'COMPLETED'`, ordered by `updated_at DESC LIMIT 1` — there is no `version`/`edited_at` filter, so a reviewer-edited assessment is still eligible to match. What keeps another user's edits from leaking is which payload the caller clones, not which rows qualify: `api.py` clones `template['ai_original_data'] or template['assessment_data']` — the pristine AI copy, falling back to `assessment_data` only for legacy rows that predate the `ai_original_data` column. Excluding edited rows instead would force a fresh (paid) LLM generation whenever every existing copy of a signature had already been reviewed.

### Editing changes the version, so clients must round-trip it
Every successful editing call returns the new `version`. A client that does not carry it forward will get `409` on its next call (if it sends a stale one) or risk overwriting a concurrent change (if it sends none). The `409` path writes nothing.

This is also what satisfies "duplicate save requests must be prevented": two requests carrying the same version cannot both apply, and a versionless repeat diffs to nothing. There is no separate idempotency key.

### One save can write several audit rows
An answer-key change writes Question Edit Saved **and** Correct Answer Changed; a mapping change writes Question Edit Saved **and** Mapping Updated; a reorder writes one Question Reordered per question that moved. Rows from one save share an `assessment_version`, which is how they group back into a single reviewer action. Do not assume one row per save.

### The competency triple must be edited together
Changing only `competency_theme` leaves the stored sub-theme belonging to the old theme, and validation rejects it. Clients must send area, theme and sub-theme in the same request. `resources/competencies.json` is the vocabulary — note that it has exactly two areas (Behavioural, Functional) and that plausible-sounding themes like "Integrity" are not in it.

### Option `index` is the answer key, not array position
`correct_option_index` is matched against each option's own `index` field. `normalize_assessment()` guarantees that field exists, filling it zero-based. Before that, the exporters disagreed on the fallback when `index` was absent — PDF/DOCX assumed zero-based, CSV assumed one-based — which could mark different options correct in different download formats for the same question.

### Option indexes are zero-based, and that is now stated in three places
The first option is `index` 0. Until prompt version 4.3 nothing said so: `resources/prompts.yaml` never mentioned `index`, and `resources/schemas.json` typed both fields as a bare `integer`. The model therefore numbered its options 0-based on some generations and 1-based on others — the same request could come back either way, and within a batched job different batches could disagree.

The convention is now stated to the model in the `OPTION INDEXING` block of both prompt templates and in the `description` of `index` and `correct_option_index` in `schemas.json` (Vertex surfaces `response_schema` descriptions to the model), and re-checked in the FINAL SELF-VALIDATION list.

Because a prompt is a request and not a guarantee, `questions._rebase_option_indexes()` is the backstop: a question whose options are a clean `1..n` run, with every `correct_option_index` value inside that run, is shifted down to `0..n-1` **together with its answer key**. Identity-preserving — the same option stays correct — and anything ambiguous (a gap in the run, a stray index, an answer key already out of range) is left untouched rather than guessed at. `NORMALIZE_OPTION_INDEX_BASE=false` disables it.

### The rebase runs at ingest only, never on a stored assessment
`normalize_assessment()` takes `rebase_option_indexes=False` by default. The single caller that passes `True` is worker_service.py, on fresh LLM output before the first store. The read projection, all four edit paths, `diff_assessments()` and every exporter leave stored indexes exactly as they found them, so an assessment generated before prompt version 4.3 keeps its base for life.

That is deliberate, and the reason is the edit path rather than the rebase itself. `apply_question_edit()` diffs the incoming client payload against the stored question. A client that read a question before it was rebased and wrote it back after would be sending indexes on the other scale — a stale one-based `correct_option_index` would land on a different option, silently. Because an assessment's base never changes after creation, no client can ever hold a snapshot on the wrong scale, so that failure mode does not exist rather than being merely unlikely.

Leaving legacy assessments one-based costs nothing: every reader matches `correct_option_index` against each option's own `index` value, so a self-consistently one-based question has always rendered correctly. A reviewer who reorders its options renumbers it to `0..n-1` through the ordinary edit path anyway, with a proper audit trail.

### The missing-`index` fill is base-aware
An option arriving with no `index` at all used to be filled with its array position. On a one-based question that duplicates an existing index and makes the question unsaveable through validation. `_fill_missing_option_indexes()` now tries the positional fill first and keeps it whenever it is collision-free, shifting up by one only when it is not. So the result is identical to the old fill everywhere the old fill produced a valid question — checked against the old implementation across all 14,406 possible four-option index/answer-key shapes, where every one of the 1,842 divergences was a question the old fill had already made invalid. It can unblock an edit that used to fail; it cannot change one that used to work. This one is on every path, not just ingest, because it is a repair rather than a convention.

### Reordering options is an `options` + `correct_option_index` edit
Options are re-sequenced within a question through the ordinary `POST /questions/update/{job_id}` edit — there is no separate endpoint, and no server-side reorder operation. The client sends the full `options` list in its new order, renumbered `0..n-1`, **together with** the remapped `correct_option_index`: the index is the answer key, so a reorder that omits it silently marks a different option correct. Send both in one request and validation catches a mismatch; send them separately and the intermediate state is rejected.

A client's answer-key control must select an **option**, not an index. Offer bare index numbers next to a reorder control and the two readings of "3" — *the option currently at index 3* versus *the index the correct option should end up at* — pick different options, and the reviewer has no way to tell which one they got. The Streamlit editor labels every entry with the option's own text for exactly this reason.

`classify_option_change()` tells a pure re-sequencing (same option texts, new order) apart from a genuine content edit, and the **audit row** records the distinction — `options_reordered` on Question Edit Saved, `reindexed_only` on Correct Answer Changed. Without it the trail would read as a reviewer changing the answer key, which is the one thing an audit of an assessment must not get wrong.

A client showing a pre-save warning needs the same comparison, and must make it locally: same option texts in a new order means "options reordered", not "the correct answer will change".
