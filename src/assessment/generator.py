import os
import json
import hashlib
import asyncio
import logging
import time
import random
import yaml
import fitz  # PyMuPDF
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError, ClientError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

from .config import (
    DATABASE_URL,
    GOOGLE_PROJECT_ID, GOOGLE_LOCATION, GENAI_MODEL_NAME,
    GOOGLE_APPLICATION_CREDENTIALS, PROMPT_VERSION, INTERACTIVE_COURSES_PATH,
    QUESTION_BATCH_SIZE, BATCH_MAX_ATTEMPTS, ENABLE_QUESTION_BATCHING,
)
from . import telemetry
from .batching import Batch, apportion, merge_batches, plan_batches, summarize_for_dedup
from .questions import BUCKET_BY_TYPE_KEY

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


def schema_without_blueprint() -> Dict[str, Any]:
    """
    `ASSESSMENT_SCHEMA` with the `blueprint` key removed.

    Used by every batch except the last. The last batch authors the blueprint and
    therefore goes out with `ASSESSMENT_SCHEMA` untouched — its call is
    configured identically to a single-call request.

    Derived rather than duplicated so it cannot drift from resources/schemas.json.
    Note the `questions` object is NOT narrowed: it keeps all five buckets
    required, so a batch emits empty arrays for the types it was told not to
    generate — exactly as a single call with a zero count for a type does.
    """
    properties = ASSESSMENT_SCHEMA.get('properties') or {}
    narrowed = {
        key: value for key, value in ASSESSMENT_SCHEMA.items()
        if key not in ('properties', 'required')
    }
    narrowed['properties'] = {k: v for k, v in properties.items() if k != 'blueprint'}
    narrowed['required'] = [
        r for r in (ASSESSMENT_SCHEMA.get('required') or []) if r != 'blueprint'
    ]
    return narrowed


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

def format_question_type_instructions(question_type_counts: Dict[str, int]) -> str:
    """
    The per-type count block used by both the single-call and batch prompts.

    Types with a zero count are still listed, marked DO NOT GENERATE, because
    naming every type is what stops the model volunteering a bucket that was not
    asked for. Extracted verbatim from `build_prompt` so both paths phrase the
    instruction identically.
    """
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

    return q_instructions


# Prompt-facing label for each short type key.
_TYPE_LABELS = {
    "mcq": "Multiple Choice Questions (MCQ)",
    "ftb": "Fill in the Blank Questions (FTB)",
    "mtf": "Match the Following Questions (MTF)",
    "multichoice": "Multi-Choice Questions",
    "truefalse": "True/False Questions",
}


def format_blooms_by_type(blooms_by_type: Dict[str, List[str]]) -> str:
    """
    The positional per-type Bloom's block.

    Used unchanged by both paths. `compute_blooms_by_type` is called once, on the
    whole request, exactly as the single-call path calls it; a batch is handed a
    contiguous slice of the result and renders it through this same function.

    The positional rule ("the Nth level listed = the Nth question of that type")
    resolves correctly inside a batch without rewording: the batch is shown only
    its own slice, numbered from one, and generates exactly that many of that
    type. It never has to know which slice it holds.
    """
    lines = []
    for qtype, levels in blooms_by_type.items():
        label = _TYPE_LABELS.get(qtype, qtype)
        level_str = ", ".join(levels)
        lines.append(f"     {label} ({len(levels)} questions): {level_str}")
    return (
        "Per-type Bloom's assignment (NON-NEGOTIABLE):\n"
        "     For each question type below, assign the listed Bloom's levels IN ORDER\n"
        "     to your questions of that type. The Nth level listed = the Nth question\n"
        "     of that type. Write the question content to genuinely reflect that level.\n"
        + "\n".join(lines)
    )


def format_previously_generated(digest: List[Dict[str, str]]) -> str:
    """
    The "already generated" block appended to every batch after the first.

    This is the one thing a batch gets that a single call does not, and the only
    reason the batches run in sequence rather than at once: a batch that can see
    what has already been asked can avoid restating it.

    Grouped by type and carrying only the stem plus the labels a later batch can
    act on. The options, reasoning and rationale are deliberately left out — they
    would multiply the prompt several times over without telling it anything a
    duplicate check needs.
    """
    if not digest:
        return ""

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for entry in digest:
        grouped.setdefault(entry.get("type", ""), []).append(entry)

    lines: List[str] = []
    number = 0
    for type_key in [t for t in _TYPE_LABELS if t in grouped] + [
        t for t in grouped if t not in _TYPE_LABELS
    ]:
        lines.append(f"  {_TYPE_LABELS.get(type_key, type_key)}")
        for entry in grouped[type_key]:
            number += 1
            tags = [t for t in (entry.get("blooms_level"), entry.get("course_name")) if t]
            tag_str = f"[{' · '.join(tags)}] " if tags else ""
            lines.append(f"   {number:>3}. {tag_str}{entry['stem']}")

    return (
        "  ------------------------------------------------------------\n"
        "  ALREADY GENERATED IN EARLIER PARTS — DO NOT REPEAT\n"
        "  ------------------------------------------------------------\n"
        f"  The following {number} question(s) have already been generated for this\n"
        "  assessment. They are NOT part of your count.\n\n"
        + "\n".join(lines)
        + "\n\n"
        "  - Every question you write MUST be distinct from all of the above in the\n"
        "    CONCEPT it tests, not merely in wording. Duplicating, rephrasing, inverting, narrowing\n"
        "    or changing the question type of an existing question is a duplicate.\n"
        "  - A question is a duplicate if a person who knows the answer to one would\n"
        "  have a meaningful advantage answering the other.\n"
        "  - Same concept at a different Bloom's level = duplicate.\n"
        "  - A genuinely broader or narrower knowledge domain at a different Bloom's\n"
        "    level = permitted.\n\n"
        "  - This does NOT change your counts. Produce exactly the counts in section 9\n"
        "    AND exactly the per-course quantities in section 13.\n"
        "  - The course tag on each line above is there so you can avoid repeating a\n"
        "    concept — NOT as a signal that a course is 'done'. A course appearing\n"
        "    often above still receives its full quantity in this part. If avoiding a\n"
        "    duplicate is hard for a course you owe questions to, narrow the sub-topic\n"
        "    or change the knowledge domain within that course; do NOT move the\n"
        "    question to a course that looks less covered."
    )


def parse_course_weightage(course_weightage: Optional[Any]) -> Optional[Dict[str, float]]:
    """The weightage payload as a `{course_id: percentage}` dict, or None."""
    if not course_weightage:
        return None
    try:
        weights = (
            json.loads(course_weightage)
            if isinstance(course_weightage, str) else course_weightage
        )
        parsed = {
            str(cid): float(weight)
            for cid, weight in dict(weights).items()
            if float(weight) > 0
        }
        return parsed or None
    except Exception as e:
        logger.warning(
            f"Failed to parse course weightage '{course_weightage}' - falling back "
            f"to equal distribution. Error: {e}"
        )
        return None


def course_counts_from_weightage(
    course_weightage: Optional[Any],
    total_questions: int,
) -> Optional[Dict[str, int]]:
    """
    The weightage percentages as exact integer per-course counts for the whole
    assessment. Returns None when no usable weightage was supplied.

    Converted once, here, rather than per batch. Handing every batch the same
    percentages means every batch rounds them independently — 60% of a 25-question
    batch is 15, but eight of those is not 60% of 200 unless the arithmetic
    happens to be clean, and the last batch is never full. Splitting exact counts
    by largest remainder makes the parts sum to the whole by construction.
    """
    weights = parse_course_weightage(course_weightage)
    if not weights:
        return None
    return apportion(total_questions, weights, list(weights))


def build_course_distribution_instruction(
    course_weightage: Optional[Any] = None,
    course_counts: Optional[Dict[str, int]] = None,
) -> str:
    """
    The per-course sourcing instruction.

    `course_counts` is supplied only on the batched path: this batch's exact share
    of each course, already apportioned. The single-call path passes nothing and
    gets the percentage wording.

    Still says nothing about which specific question types come from which course
    — the cross product is left to the model. What it no longer leaves implicit is
    that the quota has to be spread across the types rather than parked in one of
    them; that rule lives in section 13 of the template, which observed runs
    showed was being treated as advisory next to the per-type counts.
    """
    if course_counts:
        lines = [f"  - {cid}: EXACTLY {count} question(s)" for cid, count in course_counts.items()]
        return (
            "Draw the questions in THIS PART from each course in these EXACT "
            "quantities. Each key is a course identifier from input 2 — resolve it "
            "to that course's `name` and put that exact string in `course_name`:\n"
            + "\n".join(lines)
            + "\n"
            "These quantities are for THIS PART ONLY and already account for the "
            "parts before it. They are not a target to approximate: the count of "
            "questions bearing each course name must match the number above exactly."
        )

    # Unchanged from the single-call path, down to the wording and the
    # fall-through on a malformed payload.
    if course_weightage:
        try:
            weights_dict = json.loads(course_weightage) if isinstance(course_weightage, str) else course_weightage
            instruction_list = [f"  - {cid}: EXACTLY {weight}% of the total" for cid, weight in weights_dict.items()]
            return (
                "Distribute the generated questions STRICTLY according to the "
                "following percentages. Each key is a course identifier from input 2 "
                "— resolve it to that course's `name` and put that exact string in "
                "`course_name`:\n"
                + "\n".join(instruction_list)
                + "\n"
                "Convert each percentage against the total question count and hold "
                "to the resulting whole numbers."
            )
        except Exception as e:
            logger.warning(f"Failed to parse course weightage '{course_weightage}' - falling back to equal distribution. Error: {e}")

    return "Distribute questions roughly equally across courses, anchored by their content depth."


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
    #    `blooms_by_type` is kept alongside the formatted string: the batched path
    #    slices it per batch, and it stays empty on the two branches that have no
    #    per-type assignment to slice (disabled, and percentage defaults).
    blooms_by_type: Dict[str, List[str]] = {}
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
        blooms_str = format_blooms_by_type(blooms_by_type)
        logger.info(f"Blooms by type: { {k: v for k, v in blooms_by_type.items()} }")

    # 3. Format Topics
    topics_str = ", ".join(topic_names) if topic_names else "None specific (Cover all modules)"

    # 4. Format Course Weightage
    course_weightage_instruction = build_course_distribution_instruction(course_weightage)

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

    # 5. Choose the generation path.
    #    The per-type counts are what actually gets generated, so they — not
    #    `total_questions` — decide whether the request needs splitting. A request
    #    at or under the batch size takes exactly the path it always has.
    requested_total = sum(int(v or 0) for v in (question_type_counts or {}).values())
    use_batching = ENABLE_QUESTION_BATCHING and requested_total > max(1, QUESTION_BATCH_SIZE)

    shared_prompt_inputs = dict(
        course_context=json.dumps(aggregated_metadata, indent=2),
        learning_objectives_str=final_lo_str,
        transcript=final_transcript_str,
        pdf_snippets=final_pdf_str,
        assessment_type=assessment_type,
        difficulty_level=difficulty_level,
        time_to_complete=str(time_limit) + " minutes" if time_limit else None,
        additional_instructions=additional_instructions,
        input_language=input_language,
        topic_names=topics_str,
        competency_focus_instruction=competency_focus_instruction,
    )

    if not use_batching:
        # ---- Single-call path (unchanged) ----
        prompt = build_prompt(
            question_type_counts=question_type_counts,
            total_questions=total_questions,
            blooms_distribution=blooms_str,
            course_weightage_instruction=course_weightage_instruction,
            **shared_prompt_inputs,
        )

        # 6. Call LLM
        response_text, usage = await call_llm(prompt)

        try:
            result_json = json.loads(response_text)
        except json.JSONDecodeError:
            logger.error("Failed to parse LLM response as JSON")
            raise ValueError("LLM response was not valid JSON")
    else:
        # ---- Batched path ----
        result_json, usage = await _generate_in_batches(
            question_type_counts=question_type_counts,
            requested_total=requested_total,
            blooms_by_type=blooms_by_type,
            blooms_str=blooms_str,
            course_weightage=course_weightage,
            shared_prompt_inputs=shared_prompt_inputs,
            aggregated_metadata=aggregated_metadata,
            time_limit=time_limit,
            job_id=composite_id,
        )

    return aggregated_metadata, result_json, usage

async def _generate_in_batches(
    *,
    question_type_counts: Dict[str, int],
    requested_total: int,
    blooms_by_type: Dict[str, List[str]],
    blooms_str: str,
    course_weightage: Optional[Any],
    shared_prompt_inputs: Dict[str, Any],
    aggregated_metadata: Dict[str, Any],
    time_limit: Optional[int],
    job_id: Optional[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Generate a large assessment as several calls made ONE AFTER ANOTHER.

    Every batch renders the same `system_prompt_template` a single call renders,
    with the same course context, transcripts, KCM dataset, governance rules and
    item-writing standards. What differs is only what goes into the placeholders
    that already exist: this batch's question counts, its slice of the Bloom's
    assignment, its share of the course counts — and one block appended at the
    end listing the questions the earlier batches already produced.

    That list is the reason the calls are sequential. It is also why none of the
    parallel design's defences against two blind batches colliding are needed.

    The blueprint is authored by the LAST batch, the only call that has seen the
    whole assessment. Its prompt and response schema are identical to a single
    call's; the earlier batches have the blueprint section blanked and the key
    dropped from their schema.
    """
    course_targets = course_counts_from_weightage(course_weightage, requested_total)
    batches = plan_batches(question_type_counts, blooms_by_type, course_targets)
    if not batches:
        raise ValueError("No questions were requested — nothing to generate.")

    logger.info(
        f"[{job_id}] Sequential batched generation | {len(batches)} batches | "
        f"{requested_total} questions"
        + (f" | course targets {course_targets}" if course_targets else "")
    )
    for batch in batches:
        logger.info(
            f"[{job_id}] {batch.describe()}"
            + (f" | courses {batch.course_counts}" if batch.course_counts else "")
        )

    digest: List[Dict[str, str]] = []
    payloads: List[Any] = []
    usages: List[Any] = []

    # Section 10 asks the model to set question depth against the time available.
    # The limit is the whole assessment's but a batch holds a slice, so left as-is
    # a 25-question batch divides the whole budget by its own volume and reads a
    # per-question allowance several times larger than the real one.
    batch_prompt_inputs = dict(shared_prompt_inputs)
    batch_prompt_inputs["time_to_complete"] = _format_batch_time_limit(
        shared_prompt_inputs.get("time_to_complete"), requested_total,
    )

    for batch in batches:
        prompt = build_prompt(
            question_type_counts=batch.type_counts,
            total_questions=batch.total,
            blooms_distribution=(
                format_blooms_by_type(batch.blooms_by_type)
                if batch.blooms_by_type else blooms_str
            ),
            course_weightage_instruction=build_course_distribution_instruction(
                course_weightage, batch.course_counts or None,
            ),
            total_questions_text=(
                _format_final_batch_total(batch, requested_total, question_type_counts)
                if batch.is_final else None
            ),
            blueprint_section=None if batch.is_final else "",
            output_format_section=(
                None if batch.is_final
                else ASSESSMENT_PROMPTS.get("output_format_section_batch", "")
            ),
            previously_generated=format_previously_generated(digest),
            **batch_prompt_inputs,
        )
        schema = ASSESSMENT_SCHEMA if batch.is_final else schema_without_blueprint()

        try:
            payload, usage = await _generate_question_batch(
                prompt=prompt, schema=schema, batch=batch, job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 — already retried inside
            # Stop rather than skip ahead. A later batch is written against the
            # list of what came before, so generating past a hole gives every
            # remaining call an incomplete picture — and each one costs minutes
            # on a job already known to come up short.
            logger.error(
                f"[{job_id}] {batch.describe()} failed after retries — keeping the "
                f"{len(payloads)} batch(es) already generated | {exc}"
            )
            break

        payloads.append(payload)
        usages.append(usage)
        digest.extend(summarize_for_dedup(payload))
        logger.info(
            f"[{job_id}] {batch.describe()} complete | "
            f"{len(digest)}/{requested_total} questions so far"
        )

    if not payloads:
        raise RuntimeError(f"Every question batch failed for job {job_id}")

    merged_questions = merge_batches(payloads)

    produced = sum(len(v) for v in merged_questions.values())
    if produced != requested_total:
        # Either a batch was abandoned above, or one returned fewer questions than
        # it was told to. Surfaced rather than repaired — a corrective pass is a
        # separate decision, and the counts are what the caller asked for.
        shortfall: Dict[str, int] = {}
        for type_key, bucket in BUCKET_BY_TYPE_KEY.items():
            asked = int((question_type_counts or {}).get(type_key) or 0)
            missing = asked - len(merged_questions.get(bucket, []))
            if asked and missing:
                shortfall[type_key] = missing
        logger.warning(
            f"[{job_id}] Batched generation produced {produced} questions, "
            f"{requested_total} were requested. Shortfall by type: "
            f"{shortfall or 'none — a batch returned an unrequested type'}"
        )

    # The last batch to SUCCEED carries the blueprint. When the run was cut short
    # that batch was not the final one and has none, so the blueprint is empty and
    # `_recount_generated_fields` fills in what can be counted; exporters already
    # fall back to "N/A" for the three fields they read.
    last = payloads[-1] if isinstance(payloads[-1], dict) else {}
    blueprint = last.get("blueprint") if isinstance(last.get("blueprint"), dict) else {}
    if not blueprint:
        logger.warning(
            f"[{job_id}] No blueprint in the final batch response — the run was cut "
            f"short, or the model omitted it. Emitting the counted fields only."
        )

    assessment = {
        "blueprint": _recount_generated_fields(
            blueprint, merged_questions,
            assessment_type=shared_prompt_inputs.get("assessment_type"),
            difficulty_level=shared_prompt_inputs.get("difficulty_level"),
            input_language=shared_prompt_inputs.get("input_language"),
            aggregated_metadata=aggregated_metadata,
            time_limit=time_limit,
        ),
        "questions": merged_questions,
    }

    usage = _merge_usage(usages)
    logger.info(
        f"[{job_id}] Batched generation complete | {produced} questions | "
        f"{len(payloads)} batches | total_tokens={usage.get('total_token_count', 'N/A')}"
    )
    return assessment, usage


def _format_batch_time_limit(
    time_to_complete: Optional[str],
    requested_total: int,
) -> Optional[str]:
    """
    What `{time_to_complete}` renders to on the batched path.

    A batch is given the WHOLE assessment's limit but holds only a slice of the
    questions, so dividing one by the other yields a per-question budget several
    times too generous — a 25-question batch told "120 minutes" reads 4.8 minutes
    a question when a 200-question assessment actually allows 0.6, and writes
    deeper questions than the pacing supports.

    The derived figure is stated outright rather than left to be worked out,
    because doing that division against the wrong denominator is the whole
    failure mode. The single-call path passes nothing here and renders the plain
    limit exactly as it always has.
    """
    if not time_to_complete:
        return time_to_complete

    per_question = ""
    if requested_total > 0:
        minutes = re.search(r"\d+(?:\.\d+)?", str(time_to_complete))
        if minutes:
            per_question = (
                f" — approximately {float(minutes.group()) / requested_total:.2g} "
                f"minute(s) per question"
            )

    return (
        f"{time_to_complete} for the COMPLETE assessment of {requested_total} "
        f"question(s){per_question}.\n"
        f"     This is NOT the budget for this part alone. Pace question depth "
        f"against the whole-assessment figure above."
    )


def _format_final_batch_total(
    batch: Batch,
    requested_total: int,
    question_type_counts: Dict[str, int],
) -> str:
    """
    What `{total_questions_x3}` renders to for the last batch.

    That batch does two jobs: it writes its own slice, and it authors the
    blueprint for the whole assessment. The blueprint needs the whole-assessment
    total and per-type counts — for its Assessment Scope Summary, Question Type
    Suitability and Time Appropriateness Validation fields — while the generation
    instruction still has to be this batch's own counts. Both are stated here,
    labelled, rather than adding a placeholder to the template.

    The whole-assessment figures are written inline and NOT through
    `format_question_type_instructions`: rendering them as a second bulleted list
    in the same shape as the one directly above would put two sets of counts in
    identical formatting inside section 9, which invites the model to generate
    against the wrong one.
    """
    breakdown = "; ".join(
        f"{int(count)} {_TYPE_LABELS.get(key, key)}"
        for key, count in (question_type_counts or {}).items()
        if int(count or 0) > 0
    )
    return (
        f"{batch.total} for THIS PART — generate exactly the counts listed above, "
        f"and nothing more.\n"
        f"     FOR THE BLUEPRINT ONLY (do NOT generate against these): the whole "
        f"assessment is {requested_total} question(s) — {breakdown}. Describe those "
        f"totals in the blueprint; generate only THIS PART's counts."
    )


async def _generate_question_batch(
    *,
    prompt: str,
    schema: Dict[str, Any],
    batch: Batch,
    job_id: Optional[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Generate one batch, retrying on an unusable response.

    A job is now several calls, so the chance of one failing is materially higher
    than with a single call — but equally, one bad response no longer has to lose
    the whole assessment. `call_llm`'s own retry covers server, quota and cache
    errors; this covers a response that arrived but cannot be used.
    """
    attempts = max(1, BATCH_MAX_ATTEMPTS)
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            response_text, usage = await call_llm(prompt, schema=schema)
            payload = json.loads(response_text)
            if not isinstance(payload, dict):
                raise ValueError(f"Batch response was {type(payload).__name__}, expected an object")
            return payload, usage
        except json.JSONDecodeError as e:
            last_error = ValueError(f"Batch response was not valid JSON: {e}")
        except Exception as e:  # noqa: BLE001 — retried, then re-raised below
            last_error = e

        if attempt < attempts:
            logger.warning(
                f"[{job_id}] {batch.describe()} attempt {attempt}/{attempts} "
                f"failed, retrying | {last_error}"
            )

    raise last_error if last_error else RuntimeError(f"{batch.describe()} failed")


# `competency_area` on a question carries the KCM `Type`, which is spelled several
# ways across the 109 entries ("Behavioural", "Behavioral", "Behavioural - Core")
# and is absent on two. Normalised so grouping into the blueprint's
# functional/behavioral/domain keys does not depend on the spelling.
_BEHAVIOURAL_PREFIXES = ("behaviour", "behavior")
_FUNCTIONAL_PREFIXES = ("functional",)


def _competency_bucket(competency_area: Any) -> str:
    kind = str(competency_area or "").strip().lower()
    if kind.startswith(_BEHAVIOURAL_PREFIXES):
        return "behavioral"
    if kind.startswith(_FUNCTIONAL_PREFIXES):
        return "functional"
    return "domain"


def _iter_questions(merged_questions: Dict[str, List[Dict[str, Any]]]):
    for questions in (merged_questions or {}).values():
        if not isinstance(questions, list):
            continue
        for question in questions:
            if isinstance(question, dict):
                yield question


def _recount_generated_fields(
    blueprint: Dict[str, Any],
    merged_questions: Dict[str, List[Dict[str, Any]]],
    *,
    assessment_type: Optional[str] = None,
    difficulty_level: Optional[str] = None,
    input_language: Optional[str] = None,
    aggregated_metadata: Optional[Dict[str, Any]] = None,
    time_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Overwrite the blueprint fields the model cannot state correctly.

    The blueprint is written by the last batch, from the same section of the same
    template a single call uses. Most of its fields are properties of the request
    that the batch is told outright. Two groups are not:

      * **Tallies over the generated questions** — `blooms_taxonomy_mapping`,
        `difficulty_distribution`, `unified_competency_map`. No single call holds
        every question: the last batch sees its own in full and the rest only as
        stems. Counting them here is exact; asking a model to tally two hundred
        items is not.

      * **The assessment's size and pacing** — the question total in
        `assessment_scope_summary`, and `time_appropriateness_validation`. Both
        are arithmetic over values already known here, and the batch authoring
        them has only ever generated its own slice — in practice it reports that
        slice's count and pacing rather than the whole request's.

    Everything else is left exactly as the model wrote it. A tallied field is
    only replaced when there is something to replace it with, so a payload
    carrying none of the underlying data keeps the model's version.
    """
    out = dict(blueprint or {})
    produced = sum(
        len(v) for v in (merged_questions or {}).values() if isinstance(v, list)
    )

    # --- size and pacing: computed, never authored ---
    courses: List[str] = []
    for course in (aggregated_metadata or {}).get("courses", []) or []:
        name = str((course or {}).get("name") or "").strip() if isinstance(course, dict) else ""
        if name and name not in courses:
            courses.append(name)
    out["assessment_scope_summary"] = (
        f"{str(assessment_type or 'Assessment').capitalize()} assessment of "
        f"{produced} question(s) at {difficulty_level} difficulty, authored in "
        f"{input_language}, drawn from "
        f"{', '.join(courses) if courses else 'user-provided content'}."
    )

    limit = int(time_limit) if time_limit else 0
    if limit > 0 and produced > 0:
        out["time_appropriateness_validation"] = (
            f"{limit} minute(s) for {produced} question(s) is "
            f"{round(limit / produced, 2)} minute(s) per question at "
            f"{difficulty_level} difficulty."
        )
    else:
        out["time_appropriateness_validation"] = (
            f"No time limit was configured for this assessment ({produced} "
            f"question(s), {difficulty_level} difficulty); standard pacing applies."
        )

    blooms: Counter = Counter()
    difficulty: Counter = Counter()
    competencies: Dict[str, List[str]] = {"functional": [], "behavioral": [], "domain": []}

    for question in _iter_questions(merged_questions):
        level = str(question.get("blooms_level") or "").strip()
        if level:
            blooms[level] += 1
        difficulty_level = str(question.get("difficulty_level") or "").strip()
        if difficulty_level:
            difficulty[difficulty_level] += 1

        reasoning = question.get("reasoning")
        alignment = reasoning.get("competency_alignment") if isinstance(reasoning, dict) else None
        kcm = alignment.get("kcm") if isinstance(alignment, dict) else None
        if not isinstance(kcm, dict):
            continue
        sub_theme = str(kcm.get("competency_sub_theme") or "").strip()
        if not sub_theme:
            continue
        theme = str(kcm.get("competency_theme") or "").strip()
        rendered = f"{theme} > {sub_theme}" if theme else sub_theme
        bucket = _competency_bucket(kcm.get("competency_area"))
        if rendered not in competencies[bucket]:
            competencies[bucket].append(rendered)

    if blooms:
        out["blooms_taxonomy_mapping"] = dict(blooms)
    if difficulty:
        out["difficulty_distribution"] = ", ".join(
            f"{level}: {count}" for level, count in difficulty.items()
        )
    if any(competencies.values()):
        out["unified_competency_map"] = competencies

    return out


# Token counters summed across a batched job. Anything else in a usage payload
# (cache hit counts, modality breakdowns) is left to the first batch's value.
_USAGE_TOKEN_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "cached_content_token_count",
    "total_token_count",
)


def _merge_usage(usage_list: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Sum usage across batches into the shape a single call returns.

    worker_service reads one dict for both the completion log and the
    `token_usage` column, so keeping the shape identical means the worker needs
    no changes — and without the summing a batched job would report only one
    batch's tokens.
    """
    usable = [u for u in usage_list if isinstance(u, dict)]
    if not usable:
        return {}

    merged: Dict[str, Any] = dict(usable[0])
    for field in _USAGE_TOKEN_FIELDS:
        total = 0
        present = False
        for usage in usable:
            value = usage.get(field)
            if isinstance(value, (int, float)):
                total += value
                present = True
        if present:
            merged[field] = int(total)

    merged["batch_count"] = len(usable)
    merged["per_batch_usage"] = usable
    return merged


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
    *,
    total_questions_text: Optional[str] = None,
    blueprint_section: Optional[str] = None,
    output_format_section: Optional[str] = None,
    previously_generated: str = "",
) -> str:
    """
    Render `system_prompt_template`.

    The keyword-only arguments are what the batched path varies and the
    single-call path never passes. Each defaults to the text that was previously
    inline in the template, so a call that omits them renders the prompt
    byte-for-byte as it did before those placeholders existed.

      * `total_questions_text` — overrides "Total Questions = N". Used by the
        final batch, which states its own count and the whole assessment's.
      * `blueprint_section` — "" on every batch but the last, which is the only
        one that authors a blueprint.
      * `output_format_section` — the questions-only variant for those batches.
      * `previously_generated` — the list of questions earlier batches produced.
    """
    prompt_template = ASSESSMENT_PROMPTS.get('system_prompt_template', '')

    if blueprint_section is None:
        blueprint_section = ASSESSMENT_PROMPTS.get('blueprint_section', '')
    if output_format_section is None:
        output_format_section = ASSESSMENT_PROMPTS.get('output_format_section', '')

    # Sections are stored as YAML block scalars, which carry a trailing newline
    # the template already supplies. An empty section takes its own blank line
    # with it, so blanking one leaves no gap behind.
    prompt = prompt_template
    if blueprint_section.strip():
        prompt = prompt.replace("{blueprint_section}", blueprint_section.rstrip("\n"))
    else:
        prompt = prompt.replace("{blueprint_section}\n\n", "")
    prompt = prompt.replace("{output_format_section}", output_format_section.rstrip("\n"))
    if previously_generated.strip():
        prompt = prompt.replace("{previously_generated}", previously_generated.rstrip("\n"))
    else:
        prompt = prompt.replace("{previously_generated}\n", "")

    # Placeholder Replacement
    prompt = prompt.replace("{course_context}", course_context)
    prompt = prompt.replace("{learning_objectives_str}", learning_objectives_str)
    prompt = prompt.replace("{content_context}", f"TRANSCRIPTS:\n{transcript}\n\nPDF CONTENT:\n{pdf_snippets}")
    prompt = prompt.replace("{additional_instructions}", additional_instructions or "None provided")
    prompt = prompt.replace("{input_language}", input_language or "English")
    prompt = prompt.replace("{kcm_dataset}", json.dumps(KCM_DATASET, indent=2))
    
    prompt = prompt.replace("{assessment_type}", assessment_type or "comprehensive")
    prompt = prompt.replace("{difficulty_level}", difficulty_level or "Medium")
    prompt = prompt.replace(
        "{total_questions_x3}",
        total_questions_text if total_questions_text is not None else str(total_questions),
    )
    prompt = prompt.replace("{time_to_complete}", time_to_complete or "Not provided (use standard pacing)")
    prompt = prompt.replace("{course_weightage_instruction}", course_weightage_instruction)
    prompt = prompt.replace("{competency_focus_instruction}", competency_focus_instruction)

    # v3.3 Specifics (Question Types)
    if not question_type_counts:
        raise ValueError("question_type_counts cannot be empty.")

    q_instructions = format_question_type_instructions(question_type_counts)

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
async def call_llm(
    prompt: str,
    *,
    schema: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    global _active_kcm_cache
    if not client:
        raise RuntimeError("GenAI client is not initialized.")

    logger.info("Calling GenAI model: %s", GENAI_MODEL_NAME)

    cache_name = await get_or_create_kcm_cache()

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        # Defaults to the full assessment schema, which is what the single-call
        # path and the final batch both use. Only the earlier batches pass
        # anything — the same schema with the `blueprint` key dropped.
        response_schema=schema if schema is not None else ASSESSMENT_SCHEMA,
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
