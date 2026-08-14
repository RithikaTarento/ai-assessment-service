
import asyncio
import logging
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import shutil

# Reuse existing modules
from .db import init_db, close_db, update_job_status, save_assessment_result
from .fetcher import fetch_course_data
from .generator import generate_assessment
from .events import get_kafka_consumer, send_completion_event, stop_kafka_producer
from .config import INTERACTIVE_COURSES_PATH
from .storage import get_storage_service

log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] [WORKER] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "worker.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

async def process_job(payload: Dict[str, Any]):
    """
    Core Logic: Fetches content and runs LLM generation.
    """
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    course_ids = payload.get('course_ids', [])
    assessment_type = payload.get('assessment_type')
    t_start = time.monotonic()

    logger.info(f"[{job_id}] Job received | user={user_id} | type={assessment_type} | courses={course_ids} | questions={payload.get('total_questions')} | language={payload.get('language')} | difficulty={payload.get('difficulty')}")

    try:
        # 1. Update Status
        await update_job_status(job_id, "IN_PROGRESS")
        logger.info(f"[{job_id}] Status → IN_PROGRESS")

        # 2. Setup Paths
        base_path = Path(INTERACTIVE_COURSES_PATH)
        extra_files_str = payload.get('extra_files', [])
        stored_paths_to_cleanup = []
        extra_files = []
        if extra_files_str:
            storage = get_storage_service()
            for stored_path in extra_files_str:
                local_dest = Path(INTERACTIVE_COURSES_PATH) / "storage_downloads" / job_id / Path(stored_path).name
                storage.read_file(stored_path, local_dest)
                extra_files.append(local_dest)
                stored_paths_to_cleanup.append(stored_path)
                logger.info(f"[{job_id}] File retrieved from storage: {stored_path} → {local_dest}")
        if extra_files:
            logger.info(f"[{job_id}] Extra files ready: {[str(p) for p in extra_files]}")

        # 3. Fetch Course Content
        for cid in course_ids:
            logger.info(f"[{job_id}] Fetching content for course: {cid}")
            success = await fetch_course_data(cid, base_path)
            if success:
                logger.info(f"[{job_id}] Content fetched successfully for course: {cid}")
            else:
                logger.warning(f"[{job_id}] Content fetch failed for course: {cid} — will attempt generation without it")

        # 4. Generate with LLM
        logger.info(f"[{job_id}] Starting LLM generation | model={os.getenv('GENAI_MODEL_NAME', 'unknown')}")
        t_llm = time.monotonic()
        metadata, assessment, usage = await generate_assessment(
            course_ids=course_ids,
            assessment_type=assessment_type,
            difficulty_level=payload.get('difficulty'),
            total_questions=payload.get('total_questions'),
            question_type_counts=payload.get('question_type_counts'),
            additional_instructions=payload.get('additional_instructions'),
            input_language=payload.get('language'),
            topic_names=payload.get('topic_names'),
            blooms_distribution=payload.get('blooms_distribution'),
            enable_blooms=payload.get('enable_blooms', True),
            course_weightage=payload.get('course_weightage'),
            time_limit=payload.get('time_limit'),
            extra_files=extra_files,
            competency_area=payload.get('competency_area'),
            competency_themes=payload.get('competency_themes'),
            competency_sub_themes=payload.get('competency_sub_themes'),
            course_names=payload.get('course_names', []),
        )
        llm_duration = round(time.monotonic() - t_llm, 2)
        input_tokens = usage.get('prompt_token_count', 'N/A') if usage else 'N/A'
        output_tokens = usage.get('candidates_token_count', 'N/A') if usage else 'N/A'
        thinking_tokens = usage.get('thoughts_token_count', 'N/A') if usage else 'N/A'
        total_tokens = usage.get('total_token_count', 'N/A') if usage else 'N/A'
        logger.info(f"[{job_id}] LLM generation complete | duration={llm_duration}s | input_tokens={input_tokens} | thinking_tokens={thinking_tokens} | output_tokens={output_tokens} | total_tokens={total_tokens}")

        # 5. Build final metadata from the Kafka payload — single source of truth.
        #    The API sends a complete, self-contained payload; the worker no longer
        #    reads back from DB to reconstruct what the user requested.
        metadata['course_ids'] = course_ids
        metadata['course_names'] = payload.get('course_names', [])
        metadata['config'] = payload.get('config') or {
            "assessment_type": assessment_type,
            "difficulty": payload.get('difficulty'),
            "total_questions": payload.get('total_questions'),
            "question_type_counts": payload.get('question_type_counts'),
            "language": payload.get('language'),
            "time_limit": payload.get('time_limit'),
            "course_weightage": payload.get('course_weightage'),
            "competency_area": payload.get('competency_area'),
            "competency_themes": payload.get('competency_themes'),
            "competency_sub_themes": payload.get('competency_sub_themes'),
            "topic_names": payload.get('topic_names'),
            "blooms_config": payload.get('blooms_distribution'),
            "enable_blooms": payload.get('enable_blooms'),
            "additional_instructions": payload.get('additional_instructions'),
        }

        # 6. Save Result
        logger.info(f"[{job_id}] Saving metadata | course_ids={metadata.get('course_ids')} | course_names={metadata.get('course_names')}")
        await save_assessment_result(job_id, metadata, assessment, usage)
        logger.info(f"[{job_id}] Result saved to DB")

        # 7. Cleanup uploaded files from storage after successful processing
        if stored_paths_to_cleanup:
            storage = get_storage_service()
            for stored_path in stored_paths_to_cleanup:
                storage.delete_file(stored_path)

        # 8. Notify Completion
        await send_completion_event(job_id, user_id, "COMPLETED", {"course_ids": course_ids})
        total_duration = round(time.monotonic() - t_start, 2)
        logger.info(f"[{job_id}] Job COMPLETED | total_duration={total_duration}s")

    except Exception as e:
        total_duration = round(time.monotonic() - t_start, 2)
        logger.exception(f"[{job_id}] Job FAILED | duration={total_duration}s | error={e}")
        await update_job_status(job_id, "FAILED", str(e))
        await send_completion_event(job_id, user_id, "FAILED", {"error": str(e)})

async def run_worker():
    logger.info("Starting Worker Service...")
    
    # Init DB Connection
    await init_db()
    
    consumer = get_kafka_consumer()
    await consumer.start()
    logger.info("Kafka Consumer Started. Listening for tasks...")
    
    try:
        async for msg in consumer:
            msg_key = msg.key.decode() if isinstance(msg.key, bytes) else str(msg.key)
            logger.info(f"Kafka message received | key={msg_key} | partition={msg.partition} | offset={msg.offset}")
            try:
                payload = msg.value.get('payload')
                if payload:
                    job_id = payload.get('job_id', 'unknown')
                    logger.info(f"[{job_id}] Dispatching to process_job")
                    await process_job(payload)
                else:
                    logger.warning(f"Kafka message key={msg_key} had empty payload — skipping")
            except Exception as e:
                logger.error(f"Unhandled error processing Kafka message key={msg_key} | error={e}", exc_info=True)
            finally:
                # Manual Commit (if we disable auto-commit) or reliance on auto-commit
                pass
    except KeyboardInterrupt:
        logger.info("Stopping Worker...")
    finally:
        await consumer.stop()
        await stop_kafka_producer()
        await close_db()
        logger.info("Worker Stopped")

if __name__ == "__main__":
    asyncio.run(run_worker())
