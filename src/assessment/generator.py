import os
import json
import hashlib
import asyncio
import logging
import time
import random
import yaml
import fitz  # PyMuPDF
import docx  # python-docx
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List, TypedDict
from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError, ClientError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

from .config import (
    DATABASE_URL,
    GOOGLE_PROJECT_ID, GOOGLE_LOCATION, GENAI_MODEL_NAME,
    GOOGLE_APPLICATION_CREDENTIALS, PROMPT_VERSION, INTERACTIVE_COURSES_PATH,
    QUESTION_BATCH_SIZE, BATCH_MAX_ATTEMPTS, ENABLE_QUESTION_BATCHING, TOPUP_MAX_ROUNDS,
    CASE_STUDY_MIN_LEVEL_PCT,
)
from .batching import Batch, apportion, merge_batches, plan_batches, summarize_for_dedup
from .questions import new_question_id
from .response_recovery import (
    Buckets,
    count_by_type,
    extract_buckets,
    remaining_blooms,
    remaining_course_counts,
    requested_counts,
    shortfall,
    trim_to_counts,
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
        "    CONCEPT it tests, not merely in wording. Duplicating, duplicating verbatim, rephrasing, inverting, narrowing\n"
        "    or changing the question type of an existing question is a duplicate.\n"
        "  - A question is a duplicate if a person who knows the answer to one would\n"
        "    have a meaningful advantage answering the other.\n"
        "  - Same concept at a different Bloom's level = duplicate.\n"
        "  - A genuinely broader or narrower knowledge domain at a different Bloom's\n"
        "    level = permitted.\n\n"
        "  - This does NOT change your counts. Produce exactly the counts in section 10\n"
        "    AND exactly the per-course quantities in section 14.\n"
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
    them; that rule lives in section 14 of the template, which observed runs
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


# Bloom's percentages used when Bloom's is enabled but no distribution was
# supplied (PRD defaults by difficulty level).
DEFAULT_BLOOMS_BY_DIFFICULTY: Dict[str, Dict[str, int]] = {
    "beginner": {"Remember": 40, "Understand": 40, "Apply": 20},
    "intermediate": {"Remember": 20, "Understand": 30, "Apply": 30, "Analyze": 20},
    "advanced": {"Apply": 20, "Analyze": 40, "Evaluate": 30, "Create": 10},
}
DEFAULT_BLOOMS_FALLBACK: Dict[str, int] = {
    "Remember": 20, "Understand": 25, "Apply": 25, "Analyze": 20, "Evaluate": 10,
}

CASE_STUDY_BLOOMS = ["Apply", "Analyze", "Evaluate", "Create"]


def case_study_levels(
    level_weights: Dict[str, float],
    difficulty_level: Optional[str],
) -> List[str]:
    """
    The higher-order Bloom's levels the prompt offers as eligible for case studies.

    `level_weights` must describe the WHOLE assessment — question counts from
    `compute_blooms_by_type`, or the default percentages — never one batch's
    slice. Slices are shuffled runs of the assignment, so a level at 5% overall
    can look like 25% inside a small batch; deciding once, here, gives every
    batch and top-up call the same answer.

    A level qualifies at CASE_STUDY_MIN_LEVEL_PCT or more of the total. This
    only decides which levels MAY carry a case study — whether any question does
    is left to the model.
    """
    total = sum(level_weights.values())
    if total <= 0:
        return []
    return [
        level for level in CASE_STUDY_BLOOMS
        if level_weights.get(level, 0) * 100 / total >= CASE_STUDY_MIN_LEVEL_PCT
    ]


def _split_names(value: Any) -> List[str]:
    """A list as given, or a comma-separated string split into stripped names."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    return [name.strip() for name in value.split(",") if name.strip()]


def _read_course_metadata(c_path: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """A course folder's metadata.json and the learning objectives it lists."""
    meta_path = c_path / "metadata.json"
    if not meta_path.exists():
        return None, []
    meta = json.loads(meta_path.read_text(encoding='utf-8'))

    # Extract Learning Objectives from the newly formatted instructions array
    instructions = meta.get("instructions", [])
    if isinstance(instructions, list):
        return meta, list(instructions)
    if isinstance(instructions, str) and instructions.strip():
        # Fallback just in case some legacy string instructions slipped through
        return meta, [instructions]
    return meta, []


async def _collect_course_files(
    c_path: Path,
    cid: str,
    pattern: str,
    extractor,
    label: str,
    seen_content_hashes: set,
) -> Tuple[List[str], bool]:
    """
    The text of every `pattern` file under one course folder (recursive), one
    section per file. Empty files and content already seen — in this course or
    an earlier one, as a VTT or a PDF — are skipped.

    Returns the sections and whether any file contributed.
    """
    sections: List[str] = []
    found = False
    for path in c_path.rglob(pattern):
        try:
            text = await extractor(path)
            if not text:
                continue

            # Deduplication Check
            text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
            if text_hash in seen_content_hashes:
                logger.info(f"Skipping duplicate {label} content: {path.name}")
                continue
            seen_content_hashes.add(text_hash)

            rel_path = path.relative_to(c_path)
            sections.append(f"--- SOURCE: {cid} / {rel_path} ---\n{text}")
            found = True
        except Exception as e:
            logger.warning(f"Failed to read {label} {path}: {e}")
    return sections, found


async def _aggregate_course_content(
    course_ids: List[str],
    course_names: Optional[List[str]],
    aggregated_metadata: Dict[str, Any],
) -> Tuple[List[str], List[str], List[str]]:
    """
    Read every course folder: its metadata, learning objectives, transcripts
    and PDFs. Course metadata and per-course content availability are recorded
    on `aggregated_metadata`.

    Returns (transcript sections, PDF sections, learning objectives).
    """
    base_path = Path(INTERACTIVE_COURSES_PATH)
    transcripts: List[str] = []
    pdfs: List[str] = []
    learning_objectives: List[str] = []

    # Deduplication Set (to prevent double-handling of leaf vs root downloads)
    seen_content_hashes: set = set()

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

        meta, course_objectives = _read_course_metadata(c_path)
        if meta is not None:
            aggregated_metadata["courses"].append(meta)
            learning_objectives.extend(course_objectives)

        # Transcript (Recursive - find all english_subtitles.vtt in subfolders)
        vtt_sections, vtt_found = await _collect_course_files(
            c_path, cid, "english_subtitles.vtt", extract_vtt_text, "VTT", seen_content_hashes,
        )
        # PDFs (Recursive - find all PDFs in subfolders)
        pdf_sections, pdf_found = await _collect_course_files(
            c_path, cid, "*.pdf", extract_pdf_text, "PDF", seen_content_hashes,
        )
        transcripts.extend(vtt_sections)
        pdfs.extend(pdf_sections)

        aggregated_metadata["content_availability"][cid] = {
            "has_vtt": vtt_found,
            "has_pdf": pdf_found,
        }

    return transcripts, pdfs, learning_objectives


def _competency_only_context(
    competency_area: str,
    competency_themes: Any,
    competency_sub_themes: Any,
) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Context for a competency assessment with no course content: the KCM
    descriptions matching the requested themes / sub-themes, and the course
    entry that stands in for a real course.

    Returns (KCM transcript section or None when nothing matched, course entry).
    """
    themes_list = _split_names(competency_themes)
    sub_themes_list = _split_names(competency_sub_themes)
    matched = [
        entry for entry in KCM_DESCRIPTIONS_FILE
        if (
            entry.get("Area", "").lower() in [t.lower() for t in themes_list] or
            entry.get("Label", "").lower() in [s.lower() for s in sub_themes_list]
        )
    ]
    kcm_section = None
    if matched:
        kcm_content = "\n\n".join([
            f"Sub-Theme: {e.get('Label')}\nArea: {e.get('Area')}\nDescription: {e.get('Description')}\nLevels: {json.dumps(e.get('Levels', {}))}"
            for e in matched
        ])
        kcm_section = f"--- KCM COMPETENCY REFERENCE ---\n{kcm_content}"
        logger.info(f"Competency-only mode: injected {len(matched)} KCM entries for area='{competency_area}' themes={themes_list} sub_themes={sub_themes_list}")
    course = {
        "name": f"Competency Assessment — {competency_area}",
        "code": "KCM_COMPETENCY",
        "description": f"Assessment generated from KCM competency framework. Area: {competency_area}, Themes: {', '.join(themes_list)}, Sub-Themes: {', '.join(sub_themes_list)}"
    }
    return kcm_section, course


async def _collect_uploads(
    extra_files: Optional[List[Path]],
    transcript_files: Optional[List[Path]],
) -> Tuple[List[str], List[str], Dict[str, bool]]:
    """
    Text of the files uploaded through the API. The field decides the prompt
    section, the extension only decides how text is extracted:
    `transcript_files` go to TRANSCRIPTS, `extra_files` (supporting documents)
    to document content.

    Returns (transcript sections, document sections, content availability).
    """
    transcripts: List[str] = []
    documents: List[str] = []
    uploads = [(p, True) for p in (transcript_files or [])] + [
        (p, False) for p in (extra_files or [])
    ]
    for fpath, is_transcript in uploads:
        text = await extract_uploaded_text(fpath)
        if not text:
            logger.warning(f"No text extracted from uploaded file {fpath.name} — it will not contribute to the prompt")
            continue
        if is_transcript:
            transcripts.append(f"--- UPLOADED TRANSCRIPT: {fpath.name} ---\n{text}")
        else:
            documents.append(f"--- UPLOADED FILE: {fpath.name} ---\n{text}")
    # Key names kept for API compatibility: has_vtt = transcript content,
    # has_pdf = supporting-document content (any accepted format).
    availability = {"has_vtt": bool(transcripts), "has_pdf": bool(documents)}
    return transcripts, documents, availability


def _format_learning_objectives(learning_objectives: List[str]) -> str:
    """Learning objectives as a readable list for the prompt."""
    if not learning_objectives:
        return "None explicitly provided. Synthesize appropriate Learning Objectives based on the course content."
    # Deduplicate LOs just in case multiple modules had the exact same strings
    unique_los = list(dict.fromkeys(learning_objectives))
    return "\n".join([f"- {lo}" for lo in unique_los])


def _blooms_inputs(
    enable_blooms: bool,
    blooms_distribution: Optional[Dict[str, int]],
    difficulty_level: Optional[str],
    question_type_counts: Optional[Dict[str, int]],
) -> Tuple[Dict[str, List[str]], Dict[str, float], str]:
    """
    The Bloom's inputs for the prompt.

    Returns:
      * `blooms_by_type` — the per-type assignment. The batched path slices it
        per batch; it stays empty on the two branches that have no per-type
        assignment to slice (disabled, and percentage defaults).
      * `blooms_weights` — the whole assessment's mix, for `case_study_levels`.
      * the formatted string the prompt renders.
    """
    if not enable_blooms:
        return {}, {}, "Strictly Disabled - Do NOT force any specific Bloom's taxonomy mapping. Rely entirely on the requested difficulty level."

    if not blooms_distribution:
        # Dynamic defaults based on Difficulty Level (PRD Requirement)
        blooms_weights = DEFAULT_BLOOMS_BY_DIFFICULTY.get(
            (difficulty_level or "").lower(), DEFAULT_BLOOMS_FALLBACK,
        )
        blooms_str = ", ".join(f"{level}: {pct}%" for level, pct in blooms_weights.items())
        return {}, blooms_weights, blooms_str

    # Distribute bloom levels per question type so the LLM gets unambiguous assignments.
    # A flat numbered list is unreliable because the LLM writes questions grouped by type,
    # not as a flat sequence — it cannot map "Question N" to the right type bucket.
    blooms_by_type = compute_blooms_by_type(blooms_distribution, question_type_counts or {})
    blooms_str = format_blooms_by_type(blooms_by_type)
    blooms_weights = Counter(level for levels in blooms_by_type.values() for level in levels)
    logger.info(f"Blooms by type: { {k: v for k, v in blooms_by_type.items()} }")
    return blooms_by_type, blooms_weights, blooms_str


def _competency_focus_instruction(
    assessment_type: str,
    competency_area: Optional[str],
    competency_themes: Any,
    competency_sub_themes: Any,
) -> str:
    """The competency focus instruction; only a competency assessment gets one."""
    if not (assessment_type == "competency" and competency_area):
        return "Not applicable for this assessment type."
    themes_list = _split_names(competency_themes)
    sub_themes_list = _split_names(competency_sub_themes)
    return (
        f"Competency Area: {competency_area}\n"
        f"Competency Themes: {', '.join(themes_list)}\n"
        f"Competency Sub-Themes: {', '.join(sub_themes_list)}\n"
        f"ALL questions MUST map to one of the above sub-themes. No other competencies are permitted."
    )


async def _reconcile_single_call(
    result_json: Any,
    usage: Dict[str, Any],
    *,
    question_type_counts: Dict[str, int],
    requested_total: int,
    blooms_by_type: Dict[str, List[str]],
    blooms_str: str,
    course_weightage: Optional[Any],
    aggregated_metadata: Dict[str, Any],
    shared_prompt_inputs: Dict[str, Any],
    time_limit: Optional[int],
    job_id: str,
) -> Tuple[Any, Dict[str, Any]]:
    """
    Bring a single-call response's per-type counts to the request. A response
    whose counts already match is returned exactly as the model wrote it.
    Nothing here may fail the job: on any error the response is returned as
    generated, exactly as before this step existed.
    """
    try:
        blueprint = result_json.get("blueprint") if isinstance(result_json, dict) else None
        reconciled = await _reconcile_counts(
            extract_buckets(result_json),
            blueprint if isinstance(blueprint, dict) else {},
            question_type_counts=question_type_counts,
            requested_total=requested_total,
            blooms_by_type=blooms_by_type,
            blooms_str=blooms_str,
            course_weightage=course_weightage,
            aggregated_metadata=aggregated_metadata,
            shared_prompt_inputs=shared_prompt_inputs,
            job_id=job_id,
        )
        if reconciled:
            questions, blueprint, topup_usages = reconciled
            # The model's blueprint tallied the questions it wrote, not the
            # set kept, so its counted fields are recounted — as the batched
            # path always does.
            reconciled_json = dict(result_json) if isinstance(result_json, dict) else {}
            reconciled_json["questions"] = questions
            reconciled_json["blueprint"] = _recount_generated_fields(
                blueprint, questions,
                assessment_type=shared_prompt_inputs.get("assessment_type"),
                difficulty_level=shared_prompt_inputs.get("difficulty_level"),
                input_language=shared_prompt_inputs.get("input_language"),
                aggregated_metadata=aggregated_metadata,
                time_limit=time_limit,
            )
            reconciled_usage = _merge_usage([usage] + topup_usages) if topup_usages else usage
            return reconciled_json, reconciled_usage
    except Exception:
        logger.exception(
            f"[{job_id}] Count reconciliation failed — returning the "
            f"response as generated"
        )
    return result_json, usage


@dataclass
class GenerationRequest:
    """Everything one `generate_assessment` call needs, as sent by the worker."""

    question_type_counts: Dict[str, int]
    course_folder: Optional[Path] = None  # Deprecated in v3.2, kept for backward compat
    assessment_type: str = "final"
    difficulty_level: str = "Intermediate"
    total_questions: int = 5
    time_to_complete: Optional[str] = None
    additional_instructions: Optional[str] = None
    input_language: str = "English"
    course_ids: Optional[List[str]] = None
    topic_names: Optional[List[str]] = None
    blooms_distribution: Optional[Dict[str, int]] = None
    enable_blooms: bool = True
    course_weightage: Optional[str] = None
    time_limit: Optional[int] = None
    extra_files: Optional[List[Path]] = None
    transcript_files: Optional[List[Path]] = None
    competency_area: Optional[str] = None
    competency_themes: Optional[str] = None
    competency_sub_themes: Optional[str] = None
    course_names: Optional[List[str]] = None


async def generate_assessment(request: GenerationRequest) -> Tuple[Dict, Dict, Dict]:
    """
    Generates assessment for one or multiple courses.
    Returns (aggregated_metadata, assessment_json, usage_metadata)
    """
    question_type_counts = request.question_type_counts
    course_folder = request.course_folder
    assessment_type = request.assessment_type
    difficulty_level = request.difficulty_level
    total_questions = request.total_questions
    additional_instructions = request.additional_instructions
    input_language = request.input_language
    course_ids = request.course_ids
    topic_names = request.topic_names
    blooms_distribution = request.blooms_distribution
    enable_blooms = request.enable_blooms
    course_weightage = request.course_weightage
    time_limit = request.time_limit
    extra_files = request.extra_files
    transcript_files = request.transcript_files
    competency_area = request.competency_area
    competency_themes = request.competency_themes
    competency_sub_themes = request.competency_sub_themes
    course_names = request.course_names

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

    logger.info(f"Generating assessment for {composite_id} (Type: {assessment_type})")

    # 1. Aggregate Content — from the course folders, or the KCM descriptions
    #    for a competency assessment, or nothing but the uploaded files.
    aggregated_metadata = {"courses": [], "content_availability": {}}
    combined_transcript: List[str] = []
    combined_pdfs: List[str] = []
    combined_learning_objectives: List[str] = []

    if course_ids:
        combined_transcript, combined_pdfs, combined_learning_objectives = (
            await _aggregate_course_content(course_ids, course_names, aggregated_metadata)
        )
    elif assessment_type == "competency" and competency_area:
        kcm_section, course = _competency_only_context(
            competency_area, competency_themes, competency_sub_themes,
        )
        if kcm_section:
            combined_transcript.append(kcm_section)
        aggregated_metadata["courses"].append(course)
    else:
        # Dummy Metadata for Custom Uploads
        aggregated_metadata["courses"].append({
            "name": "User Uploaded Content",
            "code": "CUSTOM_UPLOAD",
            "description": "Assessment generated from user provided files (transcripts/documents)."
        })

    # Process Uploaded Files (from API).
    if extra_files or transcript_files:
        uploaded_transcripts, uploaded_docs, availability = await _collect_uploads(
            extra_files, transcript_files,
        )
        combined_transcript.extend(uploaded_transcripts)
        combined_pdfs.extend(uploaded_docs)
        aggregated_metadata["content_availability"]["uploaded_files"] = availability

    final_transcript_str = "\n\n".join(combined_transcript) if combined_transcript else "N/A"
    final_pdf_str = "\n\n".join(combined_pdfs) if combined_pdfs else "N/A"
    final_lo_str = _format_learning_objectives(combined_learning_objectives)

    # 2. Format Bloom's Distribution
    blooms_by_type, blooms_weights, blooms_str = _blooms_inputs(
        enable_blooms, blooms_distribution, difficulty_level, question_type_counts,
    )

    eligible_case_study_levels = case_study_levels(blooms_weights, difficulty_level)
    logger.info(f"Case study eligible levels: {eligible_case_study_levels or 'None'}")

    # 3. Format Topics
    topics_str = ", ".join(topic_names) if topic_names else "None specific (Cover all modules)"

    # 4. Format Course Weightage
    course_weightage_instruction = build_course_distribution_instruction(course_weightage)

    # Build competency focus instruction for competency assessment type
    competency_focus_instruction = _competency_focus_instruction(
        assessment_type, competency_area, competency_themes, competency_sub_themes,
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
        case_study_levels=", ".join(eligible_case_study_levels) or "None",
    )

    if use_batching:
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

    # ---- Single-call path (unchanged) ----
    prompt = build_prompt(
        shared_prompt_inputs,
        question_type_counts=question_type_counts,
        total_questions=total_questions,
        blooms_distribution=blooms_str,
        course_weightage_instruction=course_weightage_instruction,
    )

    # 6. Call LLM
    response_text, usage = await call_llm(prompt)

    try:
        result_json = json.loads(response_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response as JSON")
        raise ValueError("LLM response was not valid JSON")

    # 7. Bring the per-type counts to the request.
    result_json, usage = await _reconcile_single_call(
        result_json, usage,
        question_type_counts=question_type_counts,
        requested_total=requested_total,
        blooms_by_type=blooms_by_type,
        blooms_str=blooms_str,
        course_weightage=course_weightage,
        aggregated_metadata=aggregated_metadata,
        shared_prompt_inputs=shared_prompt_inputs,
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

    payloads, usages = await _run_batches(
        batches,
        digest=[],
        question_type_counts=question_type_counts,
        requested_total=requested_total,
        blooms_str=blooms_str,
        course_weightage=course_weightage,
        batch_prompt_inputs=_batch_prompt_inputs(shared_prompt_inputs, requested_total),
        job_id=job_id,
    )

    if not payloads:
        raise RuntimeError(f"Every question batch failed for job {job_id}")

    merged_questions = merge_batches(payloads)

    # The last batch to SUCCEED carries the blueprint. When the run was cut short
    # that batch was not the final one and has none; the last top-up call then
    # authors it, and failing that `_recount_generated_fields` fills in what can
    # be counted; exporters already fall back to "N/A" for the three fields they
    # read.
    last = payloads[-1] if isinstance(payloads[-1], dict) else {}
    blueprint = last.get("blueprint") if isinstance(last.get("blueprint"), dict) else {}

    # Either a batch was abandoned above, or one returned a different number of
    # questions of some type than it was told to. Nothing here may fail the
    # job: on any error the merged batches are returned as generated, exactly
    # as before this step existed.
    try:
        reconciled = await _reconcile_counts(
            extract_buckets({"questions": merged_questions}),
            blueprint,
            question_type_counts=question_type_counts,
            requested_total=requested_total,
            blooms_by_type=blooms_by_type,
            blooms_str=blooms_str,
            course_weightage=course_weightage,
            aggregated_metadata=aggregated_metadata,
            shared_prompt_inputs=shared_prompt_inputs,
            job_id=job_id,
        )
    except Exception:
        logger.exception(
            f"[{job_id}] Count reconciliation failed — returning the batches "
            f"as generated"
        )
        reconciled = None
    if reconciled:
        merged_questions, blueprint, topup_usages = reconciled
        usages.extend(topup_usages)

    produced = sum(len(v) for v in merged_questions.values())
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
        f"{len(usages)} calls | total_tokens={usage.get('total_token_count', 'N/A')}"
    )
    return assessment, usage


def _batch_prompt_inputs(
    shared_prompt_inputs: Dict[str, Any],
    requested_total: int,
) -> Dict[str, Any]:
    """
    `shared_prompt_inputs` as rendered for a call that holds only PART of the
    assessment — every batch, and every top-up call on either path.

    Section 11 asks the model to set question depth against the time available.
    The limit is the whole assessment's but a batch holds a slice, so left as-is
    a 25-question batch divides the whole budget by its own volume and reads a
    per-question allowance several times larger than the real one.
    """
    batch_prompt_inputs = dict(shared_prompt_inputs)
    batch_prompt_inputs["time_to_complete"] = _format_batch_time_limit(
        shared_prompt_inputs.get("time_to_complete"), requested_total,
    )
    return batch_prompt_inputs


async def _run_batches(
    batches: List[Batch],
    *,
    digest: List[Dict[str, str]],
    question_type_counts: Dict[str, int],
    requested_total: int,
    blooms_str: str,
    course_weightage: Optional[Any],
    batch_prompt_inputs: Dict[str, Any],
    job_id: Optional[str],
    label: str = "",
) -> Tuple[List[Any], List[Any]]:
    """
    Generate `batches` one after another. Each is shown `digest` — the questions
    already generated — which is extended in place as batches complete.

    Returns the payloads and usages of the batches that succeeded, stopping at
    the first that fails after its retries.
    """
    payloads: List[Any] = []
    usages: List[Any] = []

    for batch in batches:
        prompt = build_prompt(
            batch_prompt_inputs,
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
                f"[{job_id}] {label}{batch.describe()} failed after retries — keeping "
                f"the {len(payloads)} batch(es) already generated | {exc}"
            )
            break

        payloads.append(payload)
        usages.append(usage)
        digest.extend(summarize_for_dedup(payload))
        logger.info(
            f"[{job_id}] {label}{batch.describe()} complete | "
            f"{len(digest)}/{requested_total} questions so far"
        )

    return payloads, usages


async def _reconcile_counts(
    pool: Buckets,
    blueprint: Dict[str, Any],
    *,
    question_type_counts: Dict[str, int],
    requested_total: int,
    blooms_by_type: Dict[str, List[str]],
    blooms_str: str,
    course_weightage: Optional[Any],
    aggregated_metadata: Dict[str, Any],
    shared_prompt_inputs: Dict[str, Any],
    job_id: Optional[str],
) -> Optional[Tuple[Buckets, Dict[str, Any], List[Any]]]:
    """
    Bring the generated questions to the requested per-type counts.

    Runs only when some type's count differs from the request, or a type that
    was not requested is present — which covers every case where the total is
    off. Returns None otherwise, and the caller keeps its output untouched.

      * Extras are trimmed (`trim_to_counts`): only from a type over its count,
        only as many as it is over by, choosing which go by the Bloom's and
        course distributions.
      * A shortfall is generated as NEW calls, after all the original ones,
        planned with `plan_batches` exactly as a request would be — so a large
        shortfall is still split at QUESTION_BATCH_SIZE. Each call asks for the
        missing count per type, the type's assigned Bloom's levels not yet used
        by the kept questions, and the course quantities not yet met; and is
        shown every kept question so it does not repeat them.
      * The top-up output is trimmed the same way, and the next round fills
        whatever is still short, up to TOPUP_MAX_ROUNDS.

    If no blueprint exists yet (a batched run cut short before its last batch),
    the last top-up call authors it.

    Returns (questions, blueprint, top-up usages).
    """
    wanted = requested_counts(question_type_counts)
    if count_by_type(pool) == wanted:
        return None

    course_targets = course_counts_from_weightage(course_weightage, requested_total)
    trim_inputs = dict(
        blooms_by_type=blooms_by_type,
        course_counts=course_targets,
        aggregated_metadata=aggregated_metadata,
    )
    logger.warning(
        f"[{job_id}] Generated counts {count_by_type(pool)} differ from the "
        f"requested {wanted} — reconciling"
    )

    kept, dropped = trim_to_counts(pool, wanted, **trim_inputs)
    if dropped:
        logger.info(f"[{job_id}] Trimmed extras by type: {dropped}")

    batch_prompt_inputs = _batch_prompt_inputs(shared_prompt_inputs, requested_total)
    usages: List[Any] = []

    for round_no in range(1, max(0, TOPUP_MAX_ROUNDS) + 1):
        missing = shortfall(wanted, kept)
        if not missing:
            break

        blooms = remaining_blooms(blooms_by_type, kept, missing)
        courses = remaining_course_counts(
            course_targets, kept, sum(missing.values()), aggregated_metadata,
        )
        logger.warning(
            f"[{job_id}] Top-up round {round_no}/{TOPUP_MAX_ROUNDS} | missing {missing}"
            + (f" | Bloom's {blooms}" if blooms else "")
            + (f" | courses {courses}" if courses else "")
        )

        batches = plan_batches(missing, blooms, courses or None)
        for batch in batches:
            batch.is_final = False
        if batches and not blueprint:
            batches[-1].is_final = True

        payloads, round_usages = await _run_batches(
            batches,
            digest=summarize_for_dedup({"questions": kept}),
            question_type_counts=question_type_counts,
            requested_total=requested_total,
            blooms_str=blooms_str,
            course_weightage=course_weightage,
            batch_prompt_inputs=batch_prompt_inputs,
            job_id=job_id,
            label=f"top-up {round_no} ",
        )
        usages.extend(round_usages)

        for payload in payloads:
            for bucket, questions in extract_buckets(payload).items():
                # Each top-up call numbers its questions from one, like the
                # original call did, so fresh ids keep them from colliding.
                for question in questions:
                    question["question_id"] = new_question_id()
                kept[bucket].extend(questions)
            if not blueprint and isinstance(payload, dict) and isinstance(payload.get("blueprint"), dict):
                blueprint = payload["blueprint"]

        kept, dropped = trim_to_counts(kept, wanted, **trim_inputs)
        if dropped:
            logger.info(f"[{job_id}] Top-up round {round_no} trimmed extras by type: {dropped}")

    missing = shortfall(wanted, kept)
    if missing:
        logger.error(
            f"[{job_id}] Still short of the request after top-up — missing {missing}; "
            f"returning {sum(len(v) for v in kept.values())}/{requested_total} questions"
        )
    else:
        logger.info(f"[{job_id}] Counts reconciled to the request {wanted}")

    return kept, blueprint, usages


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
    identical formatting inside section 10, which invites the model to generate
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


class PromptInputs(TypedDict, total=False):
    """
    The prompt values shared by every call for one assessment — the single
    call, every batch and every top-up. Built once in `generate_assessment`
    (`shared_prompt_inputs`); a batch gets a copy with `time_to_complete`
    restated for its slice (`_batch_prompt_inputs`).

    `competency_focus_instruction` and `case_study_levels` are optional; the
    rest are required.
    """
    course_context: str
    learning_objectives_str: str
    transcript: str
    pdf_snippets: str
    assessment_type: str
    difficulty_level: str
    time_to_complete: Optional[str]
    additional_instructions: Optional[str]
    input_language: str
    topic_names: str
    competency_focus_instruction: str
    case_study_levels: str


def build_prompt(
    inputs: PromptInputs,
    question_type_counts: Dict[str, int],
    total_questions: int,
    blooms_distribution: str,
    course_weightage_instruction: str,
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

    `inputs` carries the values shared by every call for one assessment; see
    `PromptInputs`.
    """
    course_context = inputs["course_context"]
    learning_objectives_str = inputs["learning_objectives_str"]
    transcript = inputs["transcript"]
    pdf_snippets = inputs["pdf_snippets"]
    assessment_type = inputs["assessment_type"]
    difficulty_level = inputs["difficulty_level"]
    time_to_complete = inputs["time_to_complete"]
    additional_instructions = inputs["additional_instructions"]
    input_language = inputs["input_language"]
    topic_names = inputs["topic_names"]
    competency_focus_instruction = inputs.get(
        "competency_focus_instruction", "Not applicable for this assessment type.")
    case_study_levels = inputs.get("case_study_levels", "None")

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
    prompt = prompt.replace("{case_study_levels}", case_study_levels)

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
        # NULs (e.g. from a UTF-16 file decoded as UTF-8) would otherwise be
        # copied by the LLM into questions that Postgres jsonb cannot store.
        raw = raw.replace('\x00', '')

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
            # PyMuPDF emits NUL for glyphs it cannot map; see extract_vtt_text.
            page_text = page.get_text().replace('\x00', '').strip()
            if page_text:
                text_parts.append(page_text)
        doc.close()
    except Exception as e:
        logger.exception('PDF extraction failed for %s: %s', pdf_path, e)
    return '\n\n'.join(text_parts)

async def extract_pdf_text(pdf_path: Path) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, extract_pdf_text_sync, pdf_path)

async def extract_txt_text(txt_path: Path) -> str:
    def _read():
        try:
            raw = txt_path.read_text(encoding='utf-8-sig')
        except Exception:
            raw = txt_path.read_text(encoding='latin-1')
        # NULs: see extract_vtt_text.
        return raw.replace('\x00', '').strip()

    return await asyncio.to_thread(_read)

def extract_docx_text_sync(docx_path: Path) -> str:
    text_parts = []
    try:
        doc = docx.Document(str(docx_path))
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text.strip())
        # Tables are not part of doc.paragraphs; one line per row.
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    text_parts.append(" | ".join(cells))
    except Exception as e:
        logger.exception('DOCX extraction failed for %s: %s', docx_path, e)
    return '\n'.join(text_parts).replace('\x00', '')

async def extract_docx_text(docx_path: Path) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, extract_docx_text_sync, docx_path)

_UPLOAD_EXTRACTORS = {
    '.pdf': extract_pdf_text,
    '.vtt': extract_vtt_text,
    '.txt': extract_txt_text,
    '.docx': extract_docx_text,
}

async def extract_uploaded_text(path: Path) -> str:
    """Extract text from an uploaded file by extension; '' if the type is unsupported."""
    extractor = _UPLOAD_EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        logger.warning(f"Unsupported upload type skipped: {path.name}")
        return ""
    return await extractor(path)
