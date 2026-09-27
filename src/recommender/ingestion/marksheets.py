from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from recommender.ingestion.documents import extract_document


COURSE_CODE = re.compile(r"\b([A-Z]{2,5})\s+([A-Z]\d{3}[A-Z]?)(-\d+)?\b")
GRADE = re.compile(r"(?<!\S)(A-?|B-?|C-?|D|E|NC|W|I|SA)(?!\S)", re.IGNORECASE)
COURSE_TAIL = re.compile(
    r"^(?P<title>.*?)\s+(?P<units>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<grade>A-|B-|C-|A|B|C|D|E|NC|W|I|SA))?"
    r"(?:\s+(?P<tag>[A-Za-z0-9-]+))?\s*$",
    re.IGNORECASE,
)
EXPLICIT_STATUSES = (
    (re.compile(r"\b(?:PASSED|COMPLETED)\b", re.IGNORECASE), "completed"),
    (re.compile(r"\b(?:FAILED|FAIL)\b", re.IGNORECASE), "failed"),
    (re.compile(r"\b(?:IN[ -]?PROGRESS|REGISTERED)\b", re.IGNORECASE), "in_progress"),
    (re.compile(r"\bWITHDRAWN\b", re.IGNORECASE), "withdrawn"),
)
BITS_VALID_LETTER_GRADES = {"A", "A-", "B", "B-", "C", "C-", "D", "E"}


def _performance_status(grade: str | None) -> str:
    """Interpret BITS Performance Sheet grades using Regulations 4.11–4.20."""
    if grade is None:
        return "in_progress"
    value = grade.upper()
    if value in BITS_VALID_LETTER_GRADES or value == "SA":
        return "completed"
    if value == "NC":
        return "failed"
    if value == "W":
        return "withdrawn"
    return "unresolved"


class MarksheetParseError(RuntimeError):
    pass


def _course_identity(match: re.Match[str]) -> tuple[str, str, str | None]:
    canonical = f"{match.group(1).upper()} {match.group(2).upper()}"
    variant = match.group(3)[1:] if match.group(3) else None
    display = canonical if variant is None else f"{canonical}-{variant}"
    return display, canonical, variant


def _course_segments(line: str, page_number: int) -> list[dict[str, Any]]:
    matches = list(COURSE_CODE.finditer(line))
    results: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        tail = line[match.end():end].strip()
        parsed = COURSE_TAIL.match(tail)
        if parsed is None:
            continue
        display, canonical, variant = _course_identity(match)
        results.append({
            "course_code": display,
            "canonical_course_code": canonical,
            "course_variant": variant,
            "course_title": " ".join(parsed.group("title").split()),
            "units_candidate": float(parsed.group("units")),
            "grade": parsed.group("grade").upper() if parsed.group("grade") else None,
            "tag": parsed.group("tag"),
            "page": page_number,
            "column": "right" if match.start() >= 60 else "left",
            "raw_line": line,
            "verification_status": "needs_review",
        })
    return results


def _generic_candidate_from_line(line: str, page_number: int) -> dict[str, Any] | None:
    match = COURSE_CODE.search(line)
    if match is None:
        return None
    display, canonical, variant = _course_identity(match)
    tail = line[match.end():].strip()
    grades = GRADE.findall(tail)
    grade = grades[-1].upper() if grades else None
    status = "unresolved"
    for pattern, value in EXPLICIT_STATUSES:
        if pattern.search(tail):
            status = value
            break
    numeric_values = [float(value) for value in re.findall(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)(?![A-Za-z0-9.])", tail)]
    plausible_units = [value for value in numeric_values if 0 < value <= 25]
    return {
        "course_code": display,
        "canonical_course_code": canonical,
        "course_variant": variant,
        "grade": grade,
        "units_candidate": plausible_units[-1] if len(plausible_units) == 1 else None,
        "attempt_status": status,
        "page": page_number,
        "raw_line": line,
        "verification_status": "needs_review",
    }


def _metadata(text: str) -> dict[str, Any]:
    def value(pattern: str) -> str | None:
        match = re.search(pattern, text, re.IGNORECASE)
        return " ".join(match.group(1).split()) if match else None

    return {
        "student_id": value(r"Student ID(?: No\.)?:?\s+([A-Z0-9]+)"),
        "student_name": value(r"(?:Student\s+)?Name:?\s+([A-Z][A-Za-z ]+?)(?=\s{3,}|\n)"),
        "erp_id": value(r"ERP ID:\s*([0-9]+)"),
        "academic_status": value(r"Status:\s*([A-Za-z ]+?)(?=\s{3,}|\n)"),
        "cgpa": value(r"CGPA:\s*([0-9.]+)"),
        "verification_status": "needs_review",
    }


def _parse_performance_sheet(pages: list[dict[str, Any]]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    mode = "attempts"
    academic_year: str | None = None
    left_term: str | None = None
    right_term: str | None = None
    left_pending_group: str | None = None
    right_pending_group: str | None = None

    for page in pages:
        for raw_line in page["text"].splitlines():
            line = raw_line.rstrip()
            compact = " ".join(line.split())
            if not compact:
                continue
            if "Pending Courses (To be eligible for graduation)" in compact:
                mode = "pending"
                left_pending_group = None
                right_pending_group = None
                continue
            year_match = re.search(r"Academic Year\s+(\d{4}\s*-\s*\d{4})", compact, re.IGNORECASE)
            if year_match:
                academic_year = re.sub(r"\s+", "", year_match.group(1))
                left_term = None
                right_term = None
                continue
            if mode == "attempts" and "SEMESTER" in compact:
                first = re.search(r"FIRST SEMESTER\s+(\d{4}-\d{4})", compact, re.IGNORECASE)
                second = re.search(r"SECOND SEMESTER\s+(\d{4}-\d{4})", compact, re.IGNORECASE)
                if first:
                    left_term = f"FIRST SEMESTER {first.group(1)}"
                if second:
                    right_term = f"SECOND SEMESTER {second.group(1)}"
                continue
            if mode == "pending" and not COURSE_CODE.search(line):
                if compact.startswith("Course No."):
                    continue
                groups = [item.strip() for item in re.split(r"\s{8,}", line.strip()) if item.strip()]
                groups = [item for item in groups if item.casefold() not in {"course no.", "course title", "units"}]
                if groups and not compact.startswith(("Count of", "For any Queries")):
                    left_pending_group = groups[0]
                    right_pending_group = groups[1] if len(groups) > 1 else None
                continue

            for candidate in _course_segments(line, page["page_number"]):
                column = candidate.pop("column")
                if mode == "attempts":
                    candidate["academic_year"] = academic_year
                    candidate["term"] = right_term if column == "right" else left_term
                    candidate["record_state"] = "graded" if candidate["grade"] else "registered"
                    candidate["attempt_status"] = _performance_status(candidate["grade"])
                    attempts.append(candidate)
                else:
                    candidate["planned_group"] = right_pending_group if column == "right" else left_pending_group
                    candidate["record_state"] = "pending_requirement"
                    candidate.pop("grade", None)
                    candidate.pop("tag", None)
                    pending.append(candidate)
    return {
        "report_type": "performance_sheet",
        "profile_import_status": "review_needed",
        "attempt_candidates": attempts,
        "pending_course_candidates": pending,
        "requirement_progress_candidates": [],
    }


def parse_marksheet(source: str | Path) -> dict[str, Any]:
    """Extract reviewable runtime-profile candidates without guessing policy semantics."""
    extracted = extract_document(source, "profile_history")
    pages = extracted["pages"]
    full_text = "\n".join(page["text"] for page in pages)
    if "Category-Wise Academic Structure" in full_text:
        raise MarksheetParseError("Category-wise academic reports are not supported profile inputs; use a performance sheet.")
    if "Performance Sheet" in full_text and "Completed Courses/Registered Courses" in full_text:
        parsed = _parse_performance_sheet(pages)
    else:
        attempts = []
        for page in pages:
            for raw_line in page["text"].splitlines():
                line = " ".join(raw_line.split())
                candidate = _generic_candidate_from_line(line, page["page_number"])
                if candidate is not None:
                    attempts.append(candidate)
        parsed = {
            "report_type": "generic_marksheet",
            "profile_import_status": "review_needed",
            "attempt_candidates": attempts,
            "pending_course_candidates": [],
            "requirement_progress_candidates": [],
        }

    issues = [issue for issue in extracted["issues"] if issue.get("code") != "UNKNOWN_SCOPE"]
    attempts = parsed["attempt_candidates"]
    if not attempts and not parsed["requirement_progress_candidates"]:
        issues.append({
            "severity": "warning",
            "code": "NO_PROFILE_FACTS_FOUND",
            "message": "No course-attempt or requirement-progress rows were recognized; OCR or a format-specific adapter may be required.",
        })
    if any(item.get("grade") and item["attempt_status"] == "unresolved" for item in attempts):
        issues.append({
            "severity": "warning",
            "code": "GRADE_SEMANTICS_UNRESOLVED",
            "message": "Grades were extracted but not converted into pass/fail status because the applicable grading policy must be supplied.",
        })
    return {
        "schema_version": "marksheet-review-v2",
        "source_document": extracted["document"],
        "profile_candidates": _metadata(full_text),
        **parsed,
        "issues": issues,
        "status": parsed["profile_import_status"],
    }
