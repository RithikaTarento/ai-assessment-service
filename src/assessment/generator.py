import os
import json
import hashlib
import asyncio
import logging
import time
import random
import yaml
import fitz  # PyMuPDF
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError, ClientError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

from .config import (
    DATABASE_URL,
    GOOGLE_PROJECT_ID, GOOGLE_LOCATION, GENAI_MODEL_NAME,
    GOOGLE_APPLICATION_CREDENTIALS, PROMPT_VERSION, INTERACTIVE_COURSES_PATH
)

logger = logging.getLogger(__name__)

# Initialize GenAI Client
if GOOGLE_APPLICATION_CREDENTIALS:
    client = genai.Client(
        project=GOOGLE_PROJECT_ID,
        location=GOOGLE_LOCATION,
        vertexai=True
    )
else:
    client = None
    logger.warning("GOOGLE_APPLICATION_CREDENTIALS not set.")

# Load Resources
# Everything is now in the resources/ directory relative to this file
PACKAGE_DIR = Path(__file__).parent
RESOURCE_DIR = PACKAGE_DIR / "resources"

def load_yaml(filename):
    path = RESOURCE_DIR / filename
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}

def load_json(filename):
    path = RESOURCE_DIR / filename
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

ASSESSMENT_PROMPTS = load_yaml('prompts.yaml')
ASSESSMENT_SCHEMA_FILE = load_json('schemas.json')
ASSESSMENT_SCHEMA = ASSESSMENT_SCHEMA_FILE.get('full_schema', {})
KCM_DATASET = load_json('competencies.json')
KCM_DESCRIPTIONS_FILE = load_json('kcm_descriptions.json')

_active_kcm_cache = None

async def get_or_create_kcm_cache() -> str:
    global _active_kcm_cache
    if _active_kcm_cache:
        return _active_kcm_cache
        
    if not client or not KCM_DESCRIPTIONS_FILE:
        return None
        
    try:
        cache_content = [
             types.Content(role="user", parts=[
                 types.Part.from_text(text=f"SUPPLEMENTARY COMPETENCY REFERENCE DATASET:\n{json.dumps(KCM_DESCRIPTIONS_FILE, indent=2)}\n\nUse this detailed dataset as the authoritative reference for the behavioral indicators and level descriptions of all KCM competencies.")
             ])
        ]
        
        cached_context = await client.aio.caches.create(
            model=GENAI_MODEL_NAME,
            config=types.CreateCachedContentConfig(
                contents=cache_content,
            )
        )
        logger.info(f"Created new KCM Cache: {cached_context.name}")
        _active_kcm_cache = cached_context.name
        return _active_kcm_cache
    except Exception as e:
        logger.error(f"Failed to create KCM cache: {e}")
        return None

def compute_blooms_by_type(
    blooms_distribution: Dict[str, int],
    question_type_counts: Dict[str, int],
) -> Dict[str, List[str]]:
    """
    Converts bloom % distribution into exact per-type level lists.
    No ceiling restrictions — only the user's percentages and question counts matter.

    1. Convert percentages to integer counts (largest-remainder, sums to total).
    2. Build a flat pool of levels (shuffled).
    3. Distribute pool round-robin across types so each type gets a proportional mix.
    """
    BLOOM_ORDER = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]

    active_types = [t for t in question_type_counts if question_type_counts.get(t, 0) > 0]
    total = sum(question_type_counts[t] for t in active_types)

    # Step 1: percentage → integer counts (largest-remainder)
    active_levels = {k: v for k, v in blooms_distribution.items() if v > 0}
    items = sorted(active_levels.items(), key=lambda x: -x[1])
    level_counts: Dict[str, int] = {}
    remaining = total
    for i, (level, pct) in enumerate(items):
        if i == len(items) - 1:
            count = remaining
        else:
            count = round(total * pct / 100)
            count = min(count, remaining)
        level_counts[level] = count
        remaining -= count
        if remaining <= 0:
            break

    # Step 2: build flat pool sorted highest→lowest then shuffle
    pool: List[str] = []
    for level in reversed(BLOOM_ORDER):
        pool.extend([level] * level_counts.get(level, 0))
    random.shuffle(pool)

    # Step 3: distribute round-robin across types
    result: Dict[str, List[str]] = {t: [] for t in active_types}
    type_cycle = []
    for t in active_types:
        type_cycle.extend([t] * question_type_counts[t])
    random.shuffle(type_cycle)

    for t, level in zip(type_cycle, pool):
        result[t].append(level)

    return result


async def generate_assessment(
    question_type_counts: Dict[str, int],
    course_folder: Optional[Path] = None, # Deprecated in v3.2, kept for backward compat
    assessment_type: str = "final",
    difficulty_level: str = "Intermediate",
    total_questions: int = 5,
    time_to_complete: Optional[str] = None,
    additional_instructions: Optional[str] = None,
    input_language: str = "English",
    course_ids: List[str] = None,
    topic_names: Optional[List[str]] = None,
    blooms_distribution: Optional[Dict[str, int]] = None,
    enable_blooms: bool = True,
    course_weightage: Optional[str] = None,
    time_limit: Optional[int] = None,
    extra_files: Optional[List[Path]] = None,
    competency_area: Optional[str] = None,
    competency_themes: Optional[str] = None,
    competency_sub_themes: Optional[str] = None,
    course_names: Optional[List[str]] = None,
) -> Tuple[Dict, Dict, Dict]:
    """
    Generates assessment for one or multiple courses.
    Returns (aggregated_metadata, assessment_json, usage_metadata)
    """
    # Normalize inputs
    if not course_ids and course_folder:
        course_ids = [course_folder.name]
    
    # Normalize inputs
    if not course_ids and course_folder:
        course_ids = [course_folder.name]
    
    # if not course_ids:
    #     raise ValueError("No course_ids provided.")

    # Create Deterministic Composite Key for Caching (Sorted IDs)
    if course_ids:
        sorted_ids = sorted(course_ids)
        composite_id = f"comprehensive_{'_'.join(sorted_ids)}" if len(sorted_ids) > 1 else sorted_ids[0]
    else:
        composite_id = "custom_content_generation"
    
    base_path = Path(INTERACTIVE_COURSES_PATH)
    
    logger.info(f"Generating assessment for {composite_id} (Type: {assessment_type})")

    # 1. Aggregate Content from All Courses
    aggregated_metadata = {"courses": [], "content_availability": {}}
    combined_transcript = []
    combined_pdfs = []
    
    # Aggregate Learning Objectives specifically
    combined_learning_objectives = []
    
    # Deduplication Set (to prevent double-handling of leaf vs root downloads)
    seen_content_hashes = set()

    if course_ids:
        for idx, cid in enumerate(course_ids):
            c_path = base_path / cid
            if not c_path.exists():
                logger.warning(f"Course folder {cid} not found, skipping.")
                # If caller supplied a course name, inject it so LLM never outputs N/A
                fallback_name = (course_names or [])[idx] if course_names and idx < len(course_names) else None
                if fallback_name:
                    aggregated_metadata["courses"].append({"name": fallback_name, "identifier": cid})
                    logger.info(f"Injected fallback course name '{fallback_name}' for {cid}")
                continue
                
            # Metadata
            meta_path = c_path / "metadata.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                aggregated_metadata["courses"].append(meta)
                
                # Extract Learning Objectives from the newly formatted instructions array
                instructions = meta.get("instructions", [])
                if isinstance(instructions, list):
                    combined_learning_objectives.extend(instructions)
                elif isinstance(instructions, str) and instructions.strip():
                     # Fallback just in case some legacy string instructions slipped through
                     combined_learning_objectives.append(instructions)
                
            # Transcript (Recursive - find all english_subtitles.vtt in subfolders)
            vtt_found = False
            for vtt_path in c_path.rglob("english_subtitles.vtt"):
                 try:
                     text = await extract_vtt_text(vtt_path)
                     if not text: continue

                     # Deduplication Check
                     text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
                     if text_hash in seen_content_hashes:
                         logger.info(f"Skipping duplicate VTT content: {vtt_path.name}")
                         continue
                     seen_content_hashes.add(text_hash)

                     rel_path = vtt_path.relative_to(c_path)
                     combined_transcript.append(f"--- SOURCE: {cid} / {rel_path} ---\n{text}")
                     vtt_found = True
                 except Exception as e:
                     logger.warning(f"Failed to read VTT {vtt_path}: {e}")

            # PDFs (Recursive - find all PDFs in subfolders)
            pdf_found = False
            for pdf_file in c_path.rglob("*.pdf"):
                 try:
                    text = await extract_pdf_text(pdf_file)
                    if not text: continue

                    # Deduplication Check
                    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
                    if text_hash in seen_content_hashes:
                        logger.info(f"Skipping duplicate PDF content: {pdf_file.name}")
                        continue
                    seen_content_hashes.add(text_hash)

                    rel_path = pdf_file.relative_to(c_path)
                    combined_pdfs.append(f"--- SOURCE: {cid} / {rel_path} ---\n{text}")
                    pdf_found = True
                 except Exception as e:
                     logger.warning(f"Failed to read PDF {pdf_file}: {e}")

            aggregated_metadata["content_availability"][cid] = {
                "has_vtt": vtt_found,
                "has_pdf": pdf_found,
            }
    else:
        if assessment_type == "competency" and competency_area:
            # No course content — build context from KCM descriptions matching the requested area/themes/sub-themes
            themes_list = competency_themes if isinstance(competency_themes, list) else ([t.strip() for t in competency_themes.split(",") if t.strip()] if competency_themes else [])
            sub_themes_list = competency_sub_themes if isinstance(competency_sub_themes, list) else ([s.strip() for s in competency_sub_themes.split(",") if s.strip()] if competency_sub_themes else [])
            matched = [
                entry for entry in KCM_DESCRIPTIONS_FILE
                if (
                    entry.get("Area", "").lower() in [t.lower() for t in themes_list] or
                    entry.get("Label", "").lower() in [s.lower() for s in sub_themes_list]
                )
            ]
            if matched:
                kcm_content = "\n\n".join([
                    f"Sub-Theme: {e.get('Label')}\nArea: {e.get('Area')}\nDescription: {e.get('Description')}\nLevels: {json.dumps(e.get('Levels', {}))}"
                    for e in matched
                ])
                combined_transcript.append(f"--- KCM COMPETENCY REFERENCE ---\n{kcm_content}")
                logger.info(f"Competency-only mode: injected {len(matched)} KCM entries for area='{competency_area}' themes={themes_list} sub_themes={sub_themes_list}")
            aggregated_metadata["courses"].append({
                "name": f"Competency Assessment — {competency_area}",
                "code": "KCM_COMPETENCY",
                "description": f"Assessment generated from KCM competency framework. Area: {competency_area}, Themes: {', '.join(themes_list)}, Sub-Themes: {', '.join(sub_themes_list)}"
            })
        else:
            # Dummy Metadata for Custom Uploads
            aggregated_metadata["courses"].append({
                "name": "User Uploaded Content",
                "code": "CUSTOM_UPLOAD",
                "description": "Assessment generated from user provided files (PDF/VTT)."
            })

    # Process Extra Uploaded Files (from API)
    if extra_files:
        uploaded_vtt = False
        uploaded_pdf = False
        for fpath in extra_files:
            if fpath.suffix.lower() == '.pdf':
                text = await extract_pdf_text(fpath)
                if text:
                    combined_pdfs.append(f"--- UPLOADED FILE: {fpath.name} ---\n{text}")
                    uploaded_pdf = True
            elif fpath.suffix.lower() == '.vtt':
                text = await extract_vtt_text(fpath)
                if text:
                    combined_transcript.append(f"--- UPLOADED FILE: {fpath.name} ---\n{text}")
                    uploaded_vtt = True
        aggregated_metadata["content_availability"]["uploaded_files"] = {
            "has_vtt": uploaded_vtt,
            "has_pdf": uploaded_pdf,
        }

    final_transcript_str = "\n\n".join(combined_transcript) if combined_transcript else "N/A"
    final_pdf_str = "\n\n".join(combined_pdfs) if combined_pdfs else "N/A"
    
    # Format Learning Objectives into a nice readable list for the prompt
    if not combined_learning_objectives:
        final_lo_str = "None explicitly provided. Synthesize appropriate Learning Objectives based on the course content."
    else:
        # Deduplicate LOs just in case multiple modules had the exact same strings
        unique_los = list(dict.fromkeys(combined_learning_objectives))
        final_lo_str = "\n".join([f"- {lo}" for lo in unique_los])
    
    # 2. Format Bloom's Distribution
    if not enable_blooms:
        blooms_str = "Strictly Disabled - Do NOT force any specific Bloom's taxonomy mapping. Rely entirely on the requested difficulty level."
    elif not blooms_distribution:
        # Dynamic defaults based on Difficulty Level (PRD Requirement)
        if difficulty_level.lower() == "beginner":
            blooms_str = "Remember: 40%, Understand: 40%, Apply: 20%"
        elif difficulty_level.lower() == "intermediate":
            blooms_str = "Remember: 20%, Understand: 30%, Apply: 30%, Analyze: 20%"
        elif difficulty_level.lower() == "advanced":
             blooms_str = "Apply: 20%, Analyze: 40%, Evaluate: 30%, Create: 10%"
        else:
            blooms_str = "Remember: 20%, Understand: 25%, Apply: 25%, Analyze: 20%, Evaluate: 10%"
    else:
        # Distribute bloom levels per question type so the LLM gets unambiguous assignments.
        # A flat numbered list is unreliable because the LLM writes questions grouped by type,
        # not as a flat sequence — it cannot map "Question N" to the right type bucket.
        blooms_by_type = compute_blooms_by_type(blooms_distribution, question_type_counts or {})
        type_label_map = {
            "mcq": "Multiple Choice Questions (MCQ)",
            "ftb": "Fill in the Blank Questions (FTB)",
            "mtf": "Match the Following Questions (MTF)",
            "multichoice": "Multi-Choice Questions",
            "truefalse": "True/False Questions",
        }
        lines = []
        for qtype, levels in blooms_by_type.items():
            label = type_label_map.get(qtype, qtype)
            level_str = ", ".join(levels)
            lines.append(f"     {label} ({len(levels)} questions): {level_str}")
        blooms_str = (
            "Per-type Bloom's assignment (NON-NEGOTIABLE):\n"
            "     For each question type below, assign the listed Bloom's levels IN ORDER\n"
            "     to your questions of that type. The Nth level listed = the Nth question\n"
            "     of that type. Write the question content to genuinely reflect that level.\n"
            + "\n".join(lines)
        )
        logger.info(f"Blooms by type: { {k: v for k, v in blooms_by_type.items()} }")

    # 3. Format Topics
    topics_str = ", ".join(topic_names) if topic_names else "None specific (Cover all modules)"

    # 4. Format Course Weightage
    course_weightage_instruction = "Distribute questions roughly equally across courses, anchored by their content depth."
    if course_weightage:
        try:
            weights_dict = json.loads(course_weightage) if isinstance(course_weightage, str) else course_weightage
            instruction_list = [f"{cid}: {weight}%" for cid, weight in weights_dict.items()]
            course_weightage_instruction = "Distribute the generated questions STRICTLY according to the following percentages:\n" + "\n".join(instruction_list)
        except Exception as e:
            logger.warning(f"Failed to parse course weightage '{course_weightage}' - falling back to equal distribution. Error: {e}")

    # Build competency focus instruction for competency assessment type
    competency_focus_instruction = "Not applicable for this assessment type."
    if assessment_type == "competency" and competency_area:
        themes_list = competency_themes if isinstance(competency_themes, list) else ([t.strip() for t in competency_themes.split(",") if t.strip()] if competency_themes else [])
        sub_themes_list = competency_sub_themes if isinstance(competency_sub_themes, list) else ([s.strip() for s in competency_sub_themes.split(",") if s.strip()] if competency_sub_themes else [])
        competency_focus_instruction = (
            f"Competency Area: {competency_area}\n"
            f"Competency Themes: {', '.join(themes_list)}\n"
            f"Competency Sub-Themes: {', '.join(sub_themes_list)}\n"
            f"ALL questions MUST map to one of the above sub-themes. No other competencies are permitted."
        )

    # 5. Build Prompt
    prompt = build_prompt(
        question_type_counts=question_type_counts,
        course_context=json.dumps(aggregated_metadata, indent=2),
        learning_objectives_str=final_lo_str,
        transcript=final_transcript_str,
        pdf_snippets=final_pdf_str,
        assessment_type=assessment_type,
        difficulty_level=difficulty_level,
        total_questions=total_questions,
        time_to_complete=str(time_limit) + " minutes" if time_limit else None,
        additional_instructions=additional_instructions,
        input_language=input_language,
        topic_names=topics_str,
        blooms_distribution=blooms_str,
        course_weightage_instruction=course_weightage_instruction,
        competency_focus_instruction=competency_focus_instruction,
    )
    
    # 6. Call LLM
    response_text, usage = await call_llm(prompt)
    
    try:
        result_json = json.loads(response_text)
        return aggregated_metadata, result_json, usage
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response as JSON")
        raise ValueError("LLM response was not valid JSON")

def build_prompt(
    question_type_counts:Dict[str, int],
    course_context: str,
    learning_objectives_str: str,
    transcript: str,
    pdf_snippets: str,
    assessment_type: str,
    difficulty_level: str,
    total_questions: int,
    time_to_complete: Optional[str],
    additional_instructions: Optional[str],
    input_language: str,
    topic_names: str,
    blooms_distribution: str,
    course_weightage_instruction: str,
    competency_focus_instruction: str = "Not applicable for this assessment type.",
) -> str:
    prompt_template = ASSESSMENT_PROMPTS.get('system_prompt_template', '')
    
    # Placeholder Replacement
    prompt = prompt_template.replace("{course_context}", course_context)
    prompt = prompt.replace("{learning_objectives_str}", learning_objectives_str)
    prompt = prompt.replace("{content_context}", f"TRANSCRIPTS:\n{transcript}\n\nPDF CONTENT:\n{pdf_snippets}")
    prompt = prompt.replace("{additional_instructions}", additional_instructions or "None provided")
    prompt = prompt.replace("{input_language}", input_language or "English")
    prompt = prompt.replace("{kcm_dataset}", json.dumps(KCM_DATASET, indent=2))
    
    prompt = prompt.replace("{assessment_type}", assessment_type or "comprehensive")
    prompt = prompt.replace("{difficulty_level}", difficulty_level or "Medium")
    prompt = prompt.replace("{total_questions_x3}", str(total_questions))
    prompt = prompt.replace("{time_to_complete}", time_to_complete or "Not provided (use standard pacing)")
    prompt = prompt.replace("{course_weightage_instruction}", course_weightage_instruction)
    prompt = prompt.replace("{competency_focus_instruction}", competency_focus_instruction)

    # v3.3 Specifics (Question Types)
    if not question_type_counts:
        raise ValueError("question_type_counts cannot be empty.")
        
    q_instructions = ""
    if question_type_counts.get('mcq', 0) > 0:
        count = question_type_counts['mcq']
        q_instructions += f"\n     - {count} Multiple Choice Questions (MCQs)"
    else:
        q_instructions += "\n     - 0 Multiple Choice Questions (MCQs) [DO NOT GENERATE]"

    if question_type_counts.get('ftb', 0) > 0:
        count = question_type_counts['ftb']
        q_instructions += f"\n     - {count} Fill in the Blank Questions (FTBs)"
    else:
        q_instructions += "\n     - 0 Fill in the Blank Questions (FTBs) [DO NOT GENERATE]"

    if question_type_counts.get('mtf', 0) > 0:
        count = question_type_counts['mtf']
        q_instructions += f"\n     - {count} Match the Following Questions (MTFs)"
    else:
        q_instructions += "\n     - 0 Match the Following Questions (MTFs) [DO NOT GENERATE]"

    if question_type_counts.get('multichoice', 0) > 0:
        count = question_type_counts['multichoice']
        q_instructions += f"\n     - {count} Multi-Choice Questions"
    else:
        q_instructions += "\n     - 0 Multi-Choice Questions [DO NOT GENERATE]"
        
    if question_type_counts.get('truefalse', 0) > 0:
        count = question_type_counts['truefalse']
        q_instructions += f"\n     - {count} True/False Questions"
    else:
        q_instructions += "\n     - 0 True/False Questions [DO NOT GENERATE]"

    prompt = prompt.replace("{question_type_instructions}", q_instructions)
    logger.info(f"Q Counts: {question_type_counts} | Inst: {q_instructions}")


    # v3.2 Specifics
    prompt = prompt.replace("{topic_names}", topic_names)
    prompt = prompt.replace("{blooms_distribution}", blooms_distribution)
    
    prompt = prompt.replace("{p_version}", PROMPT_VERSION)
    prompt = prompt.replace("{a_version}", "api/v1")

    return prompt

def _should_retry(e: BaseException) -> bool:
    if isinstance(e, (ServerError, asyncio.TimeoutError)):
        return True
    # Retry cache-related errors (expired or not found) — the except block in
    # call_llm resets _active_kcm_cache so the next attempt recreates it.
    # Vertex returns 404 ("not found") or 400 ("expired") depending on the model.
    if isinstance(e, ClientError):
        msg = str(e).lower()
        if "cache" in msg and ("expired" in msg or "404" in msg or "not found" in msg):
            return True
        # Retry 429 RESOURCE_EXHAUSTED — Vertex AI rate limit hit under concurrent load.
        if "429" in msg or "resource_exhausted" in msg:
            return True
    return False

@retry(
    retry=retry_if_exception(_should_retry),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def call_llm(prompt: str) -> Tuple[str, Dict[str, Any]]:
    global _active_kcm_cache
    if not client:
        raise RuntimeError("GenAI client is not initialized.")
        
    logger.info("Calling GenAI model: %s", GENAI_MODEL_NAME)
    
    cache_name = await get_or_create_kcm_cache()
    
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ASSESSMENT_SCHEMA, # Use the correct Assessment Schema
        temperature=0.1,
    )
    if cache_name:
        config.cached_content = cache_name

    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    try:
        response = await client.aio.models.generate_content(
            model=GENAI_MODEL_NAME,
            contents=contents,
            config=config
        )
    except Exception as e:
        # Check if cache expired to trigger recreation on retry
        if "Cached content not found" in str(e) or "404" in str(e) or "invalid" in str(e).lower() or "cache" in str(e).lower():
            logger.warning("KCM Cache may have expired or is invalid. Resetting and triggering retry...")
            _active_kcm_cache = None
        raise e
    
    llm_usage = {}
    if response.usage_metadata:
        llm_usage = response.usage_metadata.to_json_dict()

    if not response.text:
        raise RuntimeError("LLM returned an empty response text.")
    
    return response.text, llm_usage

async def extract_vtt_text(vtt_path: Path) -> str:
    def _read_and_clean():
        text_lines = []
        try:
            raw = vtt_path.read_text(encoding='utf-8')
        except Exception:
            raw = vtt_path.read_text(encoding='latin-1')
            
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.upper().startswith('WEBVTT') or '-->' in line or line.isdigit():
                continue
            if line.startswith('//'):
                continue
            text_lines.append(line)
        return '\n'.join(text_lines)

    return await asyncio.to_thread(_read_and_clean)

def extract_pdf_text_sync(pdf_path: Path) -> str:
    text_parts = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            page_text = page.get_text().strip()
            if page_text:
                text_parts.append(page_text)
        doc.close()
    except Exception as e:
        logger.exception('PDF extraction failed for %s: %s', pdf_path, e)
    return '\n\n'.join(text_parts)

async def extract_pdf_text(pdf_path: Path) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, extract_pdf_text_sync, pdf_path)
