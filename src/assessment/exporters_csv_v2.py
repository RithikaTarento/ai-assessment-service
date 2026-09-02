
import csv
import re
from typing import Dict, List, Any
from pathlib import Path

from .questions import iter_questions_in_order

# Internal bucket name -> the type code used in the iGot import schema.
CSV_TYPE_BY_BUCKET = {
    "Multiple Choice Question": "MCQ-SCA",
    "Multi-Choice Question": "MCQ-MCA",
    "True/False Question": "T/F",
    "MTF Question": "MTF",
    "FTB Question": "FTB",
}


def generate_csv_v2(assessment_data: Dict[str, Any], output_path: Path):
    """
    Generates a CSV export with the specific V2 schema required by the user.
    Schema:
    QuestionNo, QuestionType, Question, QuestionTagging, Option1, isOption1Correct, ... Option7, isOption7Correct
    """
    
    # Define Header
    headers = ["QuestionNo", "QuestionType", "Question", "QuestionTagging"]
    for i in range(1, 8):
        headers.extend([f"Option{i}", f"isOption{i}Correct"])
        
    rows = []
    q_counter = 1

    # Flatten in the assessment's authoritative sequence, so the row order here
    # matches the PDF, DOCX and JSON exports.
    all_questions = [
        {
            "raw": q,
            "type": CSV_TYPE_BY_BUCKET.get(bucket, bucket),
            "complexity": q.get('reasoning', {}).get('complexity_level', 'Easy'),
        }
        for bucket, q in iter_questions_in_order(assessment_data)
    ]

    for item in all_questions:
        q = item["raw"]
        q_type = item["type"]
        difficulty_map = {"easy": "Easy", "medium": "Medium", "intermediate": "Medium", "hard": "Difficult", "difficult": "Difficult", "advanced": "Difficult"}
        raw_diff = str(q.get("difficulty_level", "")).lower()
        tagging = difficulty_map.get(raw_diff, "Medium")
        
        default_q_txt = "" 
        if q_type == "MTF":
            # Extract Matching Context and Prepend it
            context = q.get("matching_context", "Match the following items appropriately:")
            default_q_txt = f"{context}\n\n" if context else "Match the following items appropriately:\n\n"
        
        q_text = q.get("question_text", default_q_txt)
        if q_type == "FTB":
            q_text = re.sub(r'_{2,}', '<blank>', q_text)

        row = {
            "QuestionNo": q_counter,
            "QuestionType": q_type,
            "Question": q_text,
            "QuestionTagging": tagging,
        }
        
        # Override the MTF string to only contain the context since it lacks a QuestionText itself
        if q_type == "MTF":
           row["Question"] = q.get("matching_context", "Match the following items appropriately:")
        
        # Populate Options columns (Default empty)
        for i in range(1, 8):
            row[f"Option{i}"] = ""
            row[f"isOption{i}Correct"] = ""
            
        # Logic per Type
        if q_type in ["MCQ-SCA", "MCQ-MCA"]:
            options = q.get("options", [])
            correct_idx = q.get("correct_option_index")
            # correct_idx could be int (SCA) or list of ints (MCA) return 1-based index usually? 
            # Internal schema is 0-indexed or 1-indexed? Usually 0-indexed lists.
            # Example provided: Option1..Option7.
            
            # Normalize correct_idx to set
            correct_set = set()
            if isinstance(correct_idx, list):
                correct_set = {int(x) for x in correct_idx}
            elif correct_idx is not None:
                correct_set = {int(correct_idx)}
                
            for i, opt in enumerate(options[:7]):
                col_idx = i + 1
                row[f"Option{col_idx}"] = opt.get("text", "")
                # Match using the option's own index field. `normalize_assessment`
                # guarantees it is present; the fallback is zero-based to match
                # the documented convention and the PDF/DOCX exporters — it used
                # to be one-based here, which could mark a different option
                # correct in CSV than in the other formats.
                opt_index = int(opt["index"]) if opt.get("index") is not None else i
                is_correct = "Yes" if opt_index in correct_set else "No"
                row[f"isOption{col_idx}Correct"] = is_correct

        elif q_type == "T/F":
            # Fixed Options: TRUE / FALSE
            row["Option1"] = "TRUE"
            row["Option2"] = "FALSE"
            
            correct_ans = str(q.get("correct_answer", "")).lower()
            row["isOption1Correct"] = "Yes" if correct_ans == "true" else "No"
            row["isOption2Correct"] = "Yes" if correct_ans == "false" else "No"

        elif q_type == "MTF":
            # Map Pairs: Left -> OptionN, Right -> isOptionNCorrect
            pairs = q.get("pairs", [])
            for i, pair in enumerate(pairs[:7]):
                col_idx = i + 1
                row[f"Option{col_idx}"] = pair.get("left", "")
                row[f"isOption{col_idx}Correct"] = pair.get("right", "")

        elif q_type == "FTB":
            # Map blanks based on user example:
            # Option1: <text>, isOption1Correct: Blank1
            # But wait, internal FTB structure is usually a list of answers?
            # Or is it a question text with _____?
            # Let's check internal schema for FTB Question.
            # Standard FTB usually has 'correct_answer' list or dict.
            # We'll adapt: if 'blanks' list exists? 
            # Or if 'correct_answer' is dict {"blank1": "val"}.
            
            # Simple assumption:
            # Option{i}: Answer Text
            # isOption{i}Correct: "Blank{i}"
            
            correct_ans = q.get("correct_answer") # Could be string or list/dict
            if isinstance(correct_ans, dict):
                 # {"blank1": "val", "blank2": "val"}
                 for i, (k, v) in enumerate(correct_ans.items()):
                     if i >= 7: break
                     col_idx = i + 1
                     row[f"Option{col_idx}"] = v
                     row[f"isOption{col_idx}Correct"] = f"Blank{col_idx}"
            elif isinstance(correct_ans, list):
                for i, v in enumerate(correct_ans):
                    if i >= 7: break
                    col_idx = i + 1
                    row[f"Option{col_idx}"] = v
                    row[f"isOption{col_idx}Correct"] = f"Blank{col_idx}"
            else:
                 # Single string?
                 row["Option1"] = str(correct_ans)
                 row["isOption1Correct"] = "Blank1"

        rows.append(row)
        q_counter += 1

    # Write CSV
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def generate_csv_basic(assessment_data: Dict[str, Any], output_path: Path):
    """
    Basic CSV export — MCQ only (SCA + MCA), no QuestionType/QuestionTagging columns.
    Columns: SR, Question, Option1..Option6, IsOption1Correct..IsOption6Correct
    IsOptionNCorrect values: TRUE / FALSE
    """
    headers = ["SR", "Question"]
    for i in range(1, 7):
        headers.extend([f"Option{i}", f"IsOption{i}Correct"])

    rows = []
    q_counter = 1

    # Ordered by the assessment's authoritative sequence, filtered to the MCQ
    # types this schema supports.
    all_questions = [
        {"raw": q, "type": CSV_TYPE_BY_BUCKET[bucket]}
        for bucket, q in iter_questions_in_order(assessment_data)
        if bucket in ("Multiple Choice Question", "Multi-Choice Question")
    ]

    for item in all_questions:
        q = item["raw"]
        q_type = item["type"]

        q_text = q.get("question_text", "")

        row = {"SR": q_counter, "Question": q_text}

        for i in range(1, 7):
            row[f"Option{i}"] = ""
            row[f"IsOption{i}Correct"] = ""

        options = q.get("options", [])
        correct_idx = q.get("correct_option_index")
        correct_set = set()
        if isinstance(correct_idx, list):
            correct_set = {int(x) for x in correct_idx}
        elif correct_idx is not None:
            correct_set = {int(correct_idx)}

        for i, opt in enumerate(options[:6]):
            col_idx = i + 1
            row[f"Option{col_idx}"] = opt.get("text", "")
            opt_index = int(opt["index"]) if opt.get("index") is not None else i
            row[f"IsOption{col_idx}Correct"] = "TRUE" if opt_index in correct_set else "FALSE"

        rows.append(row)
        q_counter += 1

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
