# Docker Deployment Guide (POC Server)

This guide provides the minimal steps to deploy the Assessment Generation POC using Docker Compose.

## 1. Prerequisites
- **Docker** and **Docker Compose** installed on the server.
- **Port 8000** (API) and **Port 8501** (UI) must be open in the firewall.
- **System Libraries** (for PDF Generation): `libpango-1.0-0`, `libpangoft2-1.0-0`, `libjpeg62-turbo`, `libopenjp2-7` (Debian/Ubuntu).

## 2. Configuration
Create a `.env` file and a `credentials.json` file in the project root.

### .env file:
```bash
DATABASE_URL="postgresql://user:pass@db:5432/karmayogi_db"
GOOGLE_PROJECT_ID="your-project-id"
GOOGLE_LOCATION="us-central1"
GENAI_MODEL_NAME="gemini-2.5-pro"  # or any supported Vertex AI Gemini model
KARMAYOGI_API_KEY="your-api-key"

# Storage backend — "local" (default, Docker Compose) or "gcs" (Kubernetes/multi-pod)
# When set to "gcs", three types of data are shared via GCS:
#   1. User-uploaded files (standalone assessments)
#   2. Course content fetched from Learning API (VTT, PDF, metadata) — persisted permanently
#      so any pod can reuse it for regeneration without re-fetching from Learning API
#   3. Generated output files (CSV, PDF, DOCX) — written to /tmp per request, not stored in GCS
DOCUMENT_STORAGE_TYPE=local
GCS_CREDENTIALS="/app/gcs_credentials.json"        # path to GCS service account JSON (only when type=gcs)
GCS_BUCKET_NAME="your-gcs-bucket-name"             # only when type=gcs
GCS_UPLOAD_PREFIX="ai-assessments/uploads"         # prefix for user-uploaded files
GCS_COURSE_CONTENT_PREFIX="ai-assessments/course-content"  # prefix for fetched course VTT/PDF/metadata
GCS_OUTPUT_PREFIX="ai-assessments/outputs"         # reserved for future use
```

### credentials.json:
Place your Google Vertex AI service account key file in the root as `credentials.json`.

### gcs_credentials.json:
Place your GCS service account key file in the root as `gcs_credentials.json`. This is a separate service account with `Storage Object Admin` permissions on the GCS bucket.

## 3. Deployment Commands

```bash
# Build and start all services (DB, Kafka, Zookeeper, API, Worker, UI) in the background
docker-compose up -d --build
```

## 4. Verification
- **Swagger Documentation**: `http://<server-ip>:8000/docs` (Base redirect still works)
- **API v1 Status**: `http://<server-ip>:8000/ai-assessment-generation/v1/status/{course_id}`
- **Streamlit UI**: `http://<server-ip>:8501`
- **Health Check**: `http://<server-ip>:8000/health`

## 5. Persistence
The `postgres_data` and `interactive_courses_data` are mounted as volumes to ensure data persists across container restarts.

## 6. Multi-Pod (Kubernetes) Notes

When running API and Worker as separate pods, set `DOCUMENT_STORAGE_TYPE=gcs`. The Worker uses a three-tier cache for course content:

1. **Local disk** — fastest, warm for the lifetime of the pod
2. **GCS** — shared across all pods; populated after the first fetch, reused on every subsequent job for the same course
3. **Learning API** — only hit on a cold GCS miss (first time a course is requested)

Generated output files (CSV, PDF, DOCX, JSON) are written to `/tmp` on the API pod per request and streamed directly to the caller — they are not stored in GCS and do not require a shared volume.
