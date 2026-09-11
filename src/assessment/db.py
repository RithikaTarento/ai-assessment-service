import asyncpg
import json
import logging
from typing import Optional, Dict, Any, List
from .config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS interactive_assessments (
    course_id TEXT PRIMARY KEY,
    user_id TEXT, -- Nullable for v1 compatibility
    status TEXT NOT NULL, -- 'PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED'
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    assessment_data JSONB,
    token_usage JSONB,
    error_message TEXT
);
"""

# Audit trail for every human change. One row per recorded change, written in
# the same transaction as the assessment update so an audit row exists if and
# only if the change persisted.
#
# `event_code` is the only identification of what happened. There is no display
# name column: a label like "Question Edit Saved" is English copy, and the copy
# belongs to the client — see `editing.AUDIT_EVENT_CODES`. Databases created
# before this change still carry a nullable `event_name` column holding the
# labels written back then; nothing reads or writes it any more, so it is left
# in place rather than dropped, and rows written from now on leave it NULL.
CREATE_AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS interactive_assessment_audit (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL,
    assessment_version INTEGER NOT NULL,
    event_code TEXT NOT NULL,          -- question_edit / question_delete
    editor_id TEXT NOT NULL,           -- who made the change (attributability)
    question_id TEXT,
    question_type TEXT,
    previous_position INTEGER,
    new_position INTEGER,
    changed_fields JSONB,              -- [{field, previous_value, new_value}]
    original_question JSONB,           -- original AI-generated question
    question_snapshot JSONB,           -- the question as it stands after the change
    details JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# Migrations applied on every startup — additive and idempotent, matching the
# existing CREATE TABLE IF NOT EXISTS approach.
MIGRATIONS_SQL = [
    "ALTER TABLE interactive_assessments ADD COLUMN IF NOT EXISTS user_id TEXT;",
    # Monotonic assessment version, used for optimistic concurrency
    # detection and recorded against every audit row.
    "ALTER TABLE interactive_assessments ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;",
    # The pristine AI-generated assessment, retained for audit
    # regardless of how many times a human edits the live copy.
    "ALTER TABLE interactive_assessments ADD COLUMN IF NOT EXISTS ai_original_data JSONB;",
    # First human edit timestamp — distinguishes generated-only assessments
    # from reviewed ones without inspecting the audit trail.
    "ALTER TABLE interactive_assessments ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP;",
    "CREATE INDEX IF NOT EXISTS idx_assessment_audit_job ON interactive_assessment_audit (job_id, id);",
]

def _json_encoder(value):
    return json.dumps(value)

def _json_decoder(value):
    return json.loads(value)

async def _init_connection(conn):
    for type_name in ['json', 'jsonb']:
        await conn.set_type_codec(
            type_name,
            encoder=_json_encoder,
            decoder=_json_decoder,
            schema='pg_catalog'
        )

async def init_db():
    global _pool

    # Idempotency: skip if already initialized
    if _pool is not None:
        return

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        max_inactive_connection_lifetime=1800,  # recycle idle connections after 30 min
        timeout=30,                              # raise if no connection available within 30s
        init=_init_connection,
    )
    logger.info("DB connection pool created (min=5, max=20, recycle=1800s, timeout=30s)")

    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(CREATE_TABLE_SQL)
            await conn.execute(CREATE_AUDIT_TABLE_SQL)
            for statement in MIGRATIONS_SQL:
                await conn.execute(statement)
    logger.info("DB schema verified")

async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        # Clear the handle too — `init_db` returns early when `_pool` is set, so
        # leaving a closed pool here would make a later init hand out dead
        # connections instead of reconnecting.
        _pool = None
        logger.info("DB connection pool closed")

def get_pool() -> asyncpg.Pool:
    if not _pool:
        raise RuntimeError("DB pool not initialized — call init_db() at startup")
    return _pool

async def get_assessment_status(course_id: str) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM interactive_assessments WHERE course_id = $1", course_id
        )
        return dict(row) if row else None

async def create_job(course_id: str, user_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO interactive_assessments (course_id, user_id, status, metadata, updated_at)
                VALUES ($1, $2, 'PENDING', $3, NOW())
                ON CONFLICT (course_id) DO UPDATE
                SET status = 'PENDING', user_id = EXCLUDED.user_id, metadata = EXCLUDED.metadata, updated_at = NOW(), error_message = NULL
            """, course_id, user_id, metadata)

async def update_job_status(course_id: str, status: str, error: str | None = None):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE interactive_assessments
                SET status = $2, error_message = $3, updated_at = NOW()
                WHERE course_id = $1
            """, course_id, status, error)

async def save_assessment_result(course_id: str, metadata: dict, assessment: dict, usage: dict):
    """
    Store the freshly generated assessment. The same payload is written to
    `ai_original_data`, which is never modified afterwards — that is the copy
    retained for audit. Version resets to 1 because a forced
    regeneration replaces any previously edited content.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE interactive_assessments
                SET status = 'COMPLETED',
                    metadata = $2,
                    assessment_data = $3,
                    ai_original_data = $3,
                    token_usage = $4,
                    version = 1,
                    edited_at = NULL,
                    updated_at = NOW()
                WHERE course_id = $1
            """, course_id, metadata, assessment, usage)

async def find_job_by_prefix(prefix: str) -> Optional[Dict[str, Any]]:
    """
    Find a completed assessment with the same parameter signature, to clone into
    another user's workspace.

    Edited assessments qualify too. What must not be handed to another user is a
    different reviewer's *edits* — presenting those as freshly AI-generated would
    break provenance and leave the clone with no audit trail for changes it
    contains. That is a question of which payload is copied, not which rows are
    eligible: the caller clones `ai_original_data`, the pristine AI copy retained
    for exactly this purpose, falling back to `assessment_data` for legacy rows
    that predate the column (where it is the original by definition).

    Excluding edited rows instead would force a fresh LLM generation whenever
    every existing copy of a signature had been reviewed — paying for a new
    assessment while the matching original sat unused in the same table.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM interactive_assessments
            WHERE course_id LIKE $1 || '%'
            AND status = 'COMPLETED'
            ORDER BY updated_at DESC
            LIMIT 1
        """, prefix)
        return dict(row) if row else None

async def create_completed_job(course_id: str, user_id: str, metadata: dict, assessment: dict, usage: dict):
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO interactive_assessments
                (course_id, user_id, status, metadata, assessment_data, ai_original_data,
                 token_usage, version, updated_at)
                VALUES ($1, $2, 'COMPLETED', $3, $4, $4, $5, 1, NOW())
                ON CONFLICT (course_id) DO NOTHING
            """, course_id, user_id, metadata, assessment, usage)


# ==========================================================================
# Editing workspace persistence
# ==========================================================================

# Ownership predicate for writes. Must stay in step with `api._owns`, which
# guards the same operations before they reach here.
#
# `user_id` is nullable for v1 compatibility, so assessments created before the
# column existed have no owner recorded. Their owner is still recoverable:
# `course_id` has always been built as f"{composite_id}_{user_id}", so a legacy
# row belongs to whoever's id it ends with. Without this clause `api._owns`
# admits those rows while the UPDATE below matches none of them, and the caller
# reports the resulting zero-row result as a version conflict — a dead end no
# amount of reloading can clear.
#
# `right(...)` rather than LIKE because `_` is a LIKE wildcard and user ids
# contain underscores; this is an exact suffix test.
_OWNS_SQL = (
    "(user_id = $2 OR (user_id IS NULL "
    "AND right(course_id, length($2) + 1) = '_' || $2))"
)


async def save_edited_assessment(
    job_id: str,
    user_id: str,
    expected_version: int,
    new_assessment_data: dict,
    audit_rows: Optional[List[Dict[str, Any]]] = None,
) -> Optional[int]:
    """
    Persist an edit, addition, deletion or reorder, together with its audit
    trail, in a single transaction.

    The UPDATE is a compare-and-swap on `version`, which is how concurrent
    updates are detected: if another writer committed since the caller
    read the row, zero rows match and no change is applied.

    Returns the new version on success, or None when the assessment does not
    exist, is not owned by `user_id`, or lost the version race. Because the
    write and its audit rows share one transaction, a failure at any point
    leaves the stored assessment exactly as it was.

    Ownership is matched with `_OWNS_SQL`, which also accepts assessments
    created before the `user_id` column existed; those rows record their owner
    as part of this write and behave normally from then on.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            new_version = await conn.fetchval(f"""
                UPDATE interactive_assessments
                SET assessment_data = $4,
                    -- Claim a legacy row on its first edit, so the suffix
                    -- fallback in `_OWNS_SQL` is needed only once per row.
                    user_id = COALESCE(user_id, $2),
                    version = version + 1,
                    edited_at = COALESCE(edited_at, NOW()),
                    ai_original_data = COALESCE(ai_original_data, assessment_data),
                    updated_at = NOW()
                WHERE course_id = $1
                  AND {_OWNS_SQL}
                  AND version = $3
                  AND status = 'COMPLETED'
                RETURNING version
            """, job_id, user_id, expected_version, new_assessment_data)

            if new_version is None:
                return None

            for row in audit_rows or []:
                await conn.execute("""
                    INSERT INTO interactive_assessment_audit
                    (job_id, assessment_version, event_code, editor_id,
                     question_id, question_type, previous_position, new_position,
                     changed_fields, original_question, question_snapshot, details)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                    job_id,
                    new_version,
                    row.get("event_code"),
                    row.get("editor_id") or user_id,
                    row.get("question_id"),
                    row.get("question_type"),
                    row.get("previous_position"),
                    row.get("new_position"),
                    row.get("changed_fields"),
                    row.get("original_question"),
                    row.get("question_snapshot"),
                    row.get("details"),
                )

            return new_version


async def get_audit_trail(
    job_id: str, limit: int = 200, offset: int = 0
) -> List[Dict[str, Any]]:
    """Change history for one assessment, oldest first."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, job_id, assessment_version, event_code, editor_id,
                   question_id, question_type, previous_position, new_position,
                   changed_fields, original_question, question_snapshot, details, created_at
            FROM interactive_assessment_audit
            WHERE job_id = $1
            ORDER BY id ASC
            LIMIT $2 OFFSET $3
        """, job_id, limit, offset)
        return [dict(row) for row in rows]


async def update_job_result(job_id: str, user_id: str, new_assessment_data: dict) -> bool:
    """
    Legacy whole-blob save. Retained for backward compatibility; the API layer
    now routes `PUT /update/{job_id}` through `save_edited_assessment` so that
    validation, versioning and auditing apply. Kept unversioned deliberately —
    callers of this function have no version to compare against.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            result = await conn.execute("""
                UPDATE interactive_assessments
                SET assessment_data = $3,
                    version = version + 1,
                    edited_at = COALESCE(edited_at, NOW()),
                    updated_at = NOW()
                WHERE course_id = $1 AND user_id = $2
            """, job_id, user_id, new_assessment_data)
            return result != "UPDATE 0"

async def get_user_assessments_history(user_id: str) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT course_id as job_id, status, created_at, updated_at, metadata,
                   error_message, version, edited_at
            FROM interactive_assessments
            WHERE user_id = $1
            ORDER BY updated_at DESC
        """, user_id)
        return [dict(row) for row in rows]
