"""Bounded, ephemeral marksheet review helpers for runtime profile import."""
from __future__ import annotations

import tempfile
import re
import hashlib
from pathlib import Path
from typing import Any

from recommender.ingestion.marksheets import MarksheetParseError, parse_marksheet
from recommender.models import AttemptStatus, CourseAttempt, DatasetSnapshot


MAX_MARKSHEET_BYTES = 10 * 1024 * 1024
_ALLOWED_STATUSES = {status.value for status in AttemptStatus}


class MarksheetImportError(ValueError):
    """An invalid or unsupported upload/review submission."""


def _course_lookup(snapshot: DatasetSnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    return {course.code.strip().upper(): course_id for course_id, course in snapshot.courses.items()}


def _attempt_order(term: Any) -> int | None:
    if not isinstance(term, str):
        return None
    match = re.search(r"\b(FIRST|SECOND|SUMMER)\s+SEMESTER\s+(\d{4})\s*[-/]\s*\d{4}\b", term, re.I)
    if match:
        season = {"FIRST": 1, "SECOND": 2, "SUMMER": 3}[match.group(1).upper()]
        return int(match.group(2)) * 3 + season
    match = re.fullmatch(r"\s*(\d{4})\s*[-/]\s*([123])\s*", term)
    return int(match.group(1)) * 3 + int(match.group(2)) if match else None


def preview_marksheet(
    payload: bytes, filename: str, snapshot: DatasetSnapshot | None
) -> dict[str, Any]:
    """Parse a bounded PDF into a review payload; the temporary source is removed."""
    if not isinstance(payload, bytes) or not payload:
        raise MarksheetImportError("Choose a non-empty PDF file.")
    if len(payload) > MAX_MARKSHEET_BYTES:
        raise MarksheetImportError("PDF uploads must be 10 MiB or smaller.")
    if not isinstance(filename, str) or Path(filename).suffix.casefold() != ".pdf":
        raise MarksheetImportError("Upload a PDF file.")
    if not payload.startswith(b"%PDF-"):
        raise MarksheetImportError("The uploaded file does not have a valid PDF signature.")

    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="marksheet-review-", suffix=".pdf", delete=False) as tmp:
            tmp.write(payload)
            tmp_name = tmp.name
        parsed = parse_marksheet(tmp_name)
    except (OSError, MarksheetParseError, ValueError, RuntimeError) as exc:
        raise MarksheetImportError(f"Could not read this marksheet: {exc}") from exc
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)

    lookup = _course_lookup(snapshot)
    upload_id = hashlib.sha256(payload).hexdigest()[:16]
    candidates = []
    for index, source in enumerate(parsed.get("attempt_candidates", [])):
        source_code = str(source.get("course_code") or "").strip().upper()
        code = str(source.get("canonical_course_code") or source_code).strip().upper()
        
        course_id = lookup.get(source_code) or lookup.get(code)
        status = source.get("attempt_status")
        if status not in _ALLOWED_STATUSES:
            status = "unresolved"
        candidates.append({
            "index": index,
            "course_code": source.get("course_code") or code,
            "canonical_course_code": code,
            "course_id": course_id,
            "course_mapping_status": "mapped" if course_id else "unknown",
            "course_title": source.get("course_title"),
            "grade": source.get("grade"),
            "units": source.get("units_candidate"),
            "term": source.get("term"),
            "academic_year": source.get("academic_year"),
            "attempt_id": f"marksheet-{upload_id}-{index}",
            "attempt_order": _attempt_order(source.get("term")),
            "status": status,
            "selected": bool(course_id) or status != "unresolved",
            "verification_status": "needs_review",
        })
    return {
        "schema_version": "runtime-marksheet-review-v1",
        "report_type": parsed.get("report_type"),
        "filename": Path(filename).name,
        "profile_candidates": parsed.get("profile_candidates", {}),
        "attempt_candidates": candidates,
        "pending_course_candidates": parsed.get("pending_course_candidates", []),
        "issues": parsed.get("issues", []),
        "status": "review_needed",
    }


def confirmed_attempts(
    preview: dict[str, Any], submitted_rows: list[dict[str, Any]]
) -> tuple[CourseAttempt, ...]:
    """Validate user-confirmed rows against preview indexes and course mappings."""
    candidates = preview.get("attempt_candidates")
    if not isinstance(candidates, list) or not isinstance(submitted_rows, list):
        raise MarksheetImportError("Invalid review submission.")
    by_index = {row.get("index"): row for row in candidates if isinstance(row, dict)}
    seen: set[int] = set()
    confirmed: list[CourseAttempt] = []
    selected_count: dict[str, int] = {}
    for item in submitted_rows:
        if not isinstance(item, dict):
            raise MarksheetImportError("Each reviewed row must be an object.")
        try:
            source = by_index.get(item.get("index"))
        except TypeError:
            source = None
        if source and item.get("selected", source.get("selected", False)):
            mapped_id = source.get("course_id")
            target_id = mapped_id or (source.get("course_code") or source.get("canonical_course_code") if item.get("allow_unknown") is True else None)
            if target_id:
                selected_count[target_id] = selected_count.get(target_id, 0) + 1
    for submitted in submitted_rows:
        if not isinstance(submitted, dict):
            raise MarksheetImportError("Each reviewed row must be an object.")
        index = submitted.get("index")
        if type(index) is not int or index not in by_index or index in seen:
            raise MarksheetImportError("A reviewed row has an invalid or duplicate index.")
        seen.add(index)
        candidate = by_index[index]
        selected = submitted.get("selected", candidate.get("selected", False))
        if not isinstance(selected, bool):
            raise MarksheetImportError("Selection must be explicit for every reviewed row.")
        if not selected:
            continue
        course_id = candidate.get("course_id")
        if candidate.get("course_mapping_status") == "mapped" and course_id:
            requested_course_id = submitted.get("course_id", course_id)
            if requested_course_id != course_id:
                raise MarksheetImportError("Course mapping changed; refresh the review and try again.")
        elif submitted.get("allow_unknown") is True:
            requested_course_id = candidate.get("course_code") or candidate.get("canonical_course_code")
            if not isinstance(requested_course_id, str) or not re.fullmatch(r"[A-Z]{2,5} [A-Z]\d{3}[A-Z]?(?:-\d+)?", requested_course_id):
                raise MarksheetImportError("The unmapped course code is invalid.")
            if submitted.get("course_id") not in (None, requested_course_id):
                raise MarksheetImportError("An unmapped row can only be kept under its extracted course code.")
        else:
            raise MarksheetImportError("Confirm an unmapped course explicitly to keep it as a code-only history entry.")
        status = submitted.get("status")
        if status not in _ALLOWED_STATUSES:
            raise MarksheetImportError("Choose a valid attempt status for every selected course.")
        grade = submitted.get("grade", candidate.get("grade"))
        if grade is not None:
            if not isinstance(grade, str) or len(grade.strip()) > 12 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+\-]*", grade.strip()):
                raise MarksheetImportError("Enter a valid grade, or leave it blank.")
            grade = grade.strip().upper()
        units = submitted.get("units", candidate.get("units"))
        if units is not None:
            if isinstance(units, bool):
                raise MarksheetImportError("Units must be a number between 0 and 25.")
            try:
                units = float(units)
            except (TypeError, ValueError) as exc:
                raise MarksheetImportError("Units must be a number between 0 and 25.") from exc
            if not 0 < units <= 25:
                raise MarksheetImportError("Units must be a number between 0 and 25.")
        term = submitted.get("term_id", candidate.get("term"))
        if term is not None and (not isinstance(term, str) or len(term.strip()) > 120):
            raise MarksheetImportError("Term must be at most 120 characters.")
        term = term.strip() if isinstance(term, str) else None
        order = submitted.get("attempt_order", candidate.get("attempt_order"))
        if order is not None and (type(order) is not int or order < 0):
            raise MarksheetImportError("Attempt order must be a non-negative integer.")
        if order is None:
            order = _attempt_order(term)
        if order is None:
            raise MarksheetImportError("The attempt order is ambiguous; enter a term or explicit chronological order.")
        if selected_count.get(requested_course_id, 0) > 1 and not term:
            raise MarksheetImportError("Add the term for each repeated course attempt before confirming.")
        confirmed.append(CourseAttempt(
            course_id=requested_course_id,
            status=AttemptStatus(status),
            grade=grade,
            units_awarded=units,
            term_id=term,
            attempt_id=candidate.get("attempt_id") or f"marksheet-row-{index}",
            attempt_order=order,
        ))
    return tuple(confirmed)
