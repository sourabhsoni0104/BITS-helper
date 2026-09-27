from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recommender.ingestion.snapshots import file_sha256


REQUIRED_SOURCE_DOCUMENT_TYPES = ("regulations", "bulletin", "timetable", "handout")
RUNTIME_INPUT_TYPES = ("profile_history",)
REQUIRED_DOCUMENT_TYPES = (*REQUIRED_SOURCE_DOCUMENT_TYPES, *RUNTIME_INPUT_TYPES)
DOCUMENT_TYPES = (*REQUIRED_DOCUMENT_TYPES, "reference")
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".tsv"}
IGNORED_SOURCE_FILENAMES = frozenset({"bit_acad_rpt.pdf"})


class ExtractionError(RuntimeError):
    pass


def is_ignored_source(path: Path) -> bool:
    """Return whether a repository fixture is explicitly outside product scope."""
    return path.name.casefold() in IGNORED_SOURCE_FILENAMES


def _candidate_type(path: Path) -> str | None:
    
    
    name = f"{path.parent.name} {path.name}".casefold()
    patterns = {
        "regulations": r"regulation|academic[_ -]?rules?",
        "bulletin": r"bulletin|catalog(?:ue)?",
        "timetable": r"time[_ -]?table|schedule",
        "handout": r"handout|course[_ -]?outline|syllabus",
        "profile_history": r"transcript|academic[_ -]?history|student[_ -]?profile|bit[_ -]?perfmnc",
    }
    matches = [document_type for document_type, pattern in patterns.items() if re.search(pattern, name)]
    return matches[0] if len(matches) == 1 else None


def inventory_sources(directory: str | Path) -> dict[str, Any]:
    root = Path(directory)
    files: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != ".gitkeep"):
            suffix = path.suffix.casefold()
            ignored = is_ignored_source(path)
            files.append({
                "path": str(path),
                "extension": suffix,
                "supported": suffix in SUPPORTED_EXTENSIONS,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
                "candidate_document_type": None if ignored else _candidate_type(path),
                "classification_status": "ignored" if ignored else "needs_review",
                "ignored": ignored,
            })
    candidate_types = {
        item["candidate_document_type"]
        for item in files
        if item["supported"] and not item["ignored"] and item["candidate_document_type"]
    }
    return {
        "input_directory": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "required_input_classes": list(REQUIRED_DOCUMENT_TYPES),
        "runtime_input_classes": list(RUNTIME_INPUT_TYPES),
        "candidate_classes_present": sorted(candidate_types),
        "classes_without_candidates": [item for item in REQUIRED_SOURCE_DOCUMENT_TYPES if item not in candidate_types],
        "ignored_files": [item["path"] for item in files if item["ignored"]],
        "note": "Filename classifications are candidates only and require confirmation during extraction.",
    }


def _pdf_page_count(path: Path) -> int:
    executable = shutil.which("pdfinfo")
    if executable is None:
        raise ExtractionError("pdfinfo is required for PDF extraction but was not found on PATH.")
    result = subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise ExtractionError(f"pdfinfo failed for {path.name}: {result.stderr.strip()}")
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise ExtractionError(f"Could not determine the page count for {path.name}.")
    return int(match.group(1))


def _extract_pdf(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ExtractionError("pdftotext is required for PDF extraction but was not found on PATH.")
    page_count = _pdf_page_count(path)
    result = subprocess.run(
        [executable, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=max(60, page_count * 2),
        check=False,
    )
    if result.returncode != 0:
        raise ExtractionError(f"pdftotext failed for {path.name}: {result.stderr.strip()}")
    chunks = result.stdout.split("\f")
    if len(chunks) == page_count + 1 and not chunks[-1].strip():
        chunks.pop()
    if len(chunks) != page_count:
        raise ExtractionError(
            f"Page-aware extraction mismatch for {path.name}: pdfinfo reported {page_count} pages but pdftotext returned {len(chunks)}."
        )
    pages: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for page_number, chunk in enumerate(chunks, start=1):
        text = chunk.strip()
        status = "needs_review" if text else "not_found"
        pages.append({
            "page_number": page_number,
            "text": text,
            "extraction_method": "pdftotext-layout",
            "verification_status": status,
        })
        if not text:
            issues.append({
                "severity": "warning",
                "code": "PAGE_TEXT_NOT_FOUND",
                "page_number": page_number,
                "message": "No usable text was extracted; this page may require OCR.",
            })
    return pages, issues


def _extract_text(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"{path.name} is not valid UTF-8 text: {exc}") from exc
    chunks = raw.split("\f")
    pages = [
        {
            "page_number": index,
            "text": text.strip(),
            "extraction_method": "utf8-text",
            "verification_status": "needs_review" if text.strip() else "not_found",
        }
        for index, text in enumerate(chunks, start=1)
    ]
    issues = [
        {
            "severity": "warning",
            "code": "PAGE_TEXT_NOT_FOUND",
            "page_number": page["page_number"],
            "message": "This logical page is empty.",
        }
        for page in pages
        if not page["text"]
    ]
    return pages, issues


def _extract_docx(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract Word body order into one logical unit; DOCX has no reliable physical pages."""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ExtractionError(f"Could not read DOCX content from {path.name}: {exc}") from exc
    body = root.find("w:body", ns)
    if body is None:
        raise ExtractionError(f"DOCX body is missing from {path.name}.")
    blocks: list[str] = []
    section = None
    sections: list[str] = []
    for node in body:
        if node.tag.endswith("}p"):
            value = "".join(item.text or "" for item in node.findall(".//w:t", ns)).strip()
            if not value:
                continue
            style = node.find("w:pPr/w:pStyle", ns)
            style_name = style.get(f"{{{ns['w']}}}val", "") if style is not None else ""
            if re.match(r"(?:Title|Heading)\d*", style_name, re.I):
                section = value
            blocks.append(value)
            sections.append(section or "")
        elif node.tag.endswith("}tbl"):
            for row in node.findall(".//w:tr", ns):
                cells = [" ".join((t.text or "") for t in cell.findall(".//w:t", ns)).strip() for cell in row.findall("w:tc", ns)]
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    blocks.append(line)
                    sections.append(section or "")
    grouped: list[tuple[str, list[str]]] = []
    for label, block in zip(sections, blocks):
        if not grouped or grouped[-1][0] != label:
            grouped.append((label, []))
        grouped[-1][1].append(block)
    pages = [{"page_number": None, "text": "\n".join(items), "section": label or None, "extraction_method": "docx-xml-logical-section", "verification_status": "needs_review"} for label, items in grouped]
    issue = [] if pages else [{"severity": "warning", "code": "DOCUMENT_TEXT_NOT_FOUND", "message": "No usable body text was extracted."}]
    return pages, issue


def _ocr_empty_pdf_pages(path: Path, pages: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    renderer, ocr = shutil.which("pdftoppm"), shutil.which("tesseract")
    empty = [page for page in pages if not page["text"]]
    if not empty:
        return
    if not renderer or not ocr:
        issues.append({"severity": "warning", "code": "OCR_UNAVAILABLE", "message": "Empty PDF pages remain unsearched because pdftoppm and/or tesseract are unavailable."})
        return
    with tempfile.TemporaryDirectory(prefix="reference-ocr-") as directory:
        for page in empty:
            base = Path(directory) / f"page-{page['page_number']}"
            try:
                subprocess.run([renderer, "-f", str(page["page_number"]), "-l", str(page["page_number"]), "-singlefile", "-png", str(path), str(base)], capture_output=True, timeout=60, check=True)
                result = subprocess.run([ocr, str(base) + ".png", "stdout"], capture_output=True, text=True, timeout=60, check=True)
                page["text"] = result.stdout.strip()
                if page["text"]:
                    page["extraction_method"] = "pdftotext-layout+ocr"
                    page["verification_status"] = "needs_review"
                    issues[:] = [issue for issue in issues if not (issue.get("code") == "PAGE_TEXT_NOT_FOUND" and issue.get("page_number") == page["page_number"])]
            except (OSError, subprocess.SubprocessError) as exc:
                issues.append({"severity": "warning", "code": "OCR_PAGE_FAILED", "page_number": page["page_number"], "message": f"OCR failed: {exc}"})


def extract_document(
    source: str | Path,
    document_type: str,
    *,
    campus_scope: str | None = None,
    programme_scope: str | None = None,
    batch_scope: str | None = None,
    semester_scope: str | None = None,
) -> dict[str, Any]:
    path = Path(source)
    if not path.is_file():
        raise ExtractionError(f"Source document not found: {path}")
    if document_type not in DOCUMENT_TYPES:
        raise ExtractionError(f"Unsupported document type {document_type!r}; expected one of {', '.join(DOCUMENT_TYPES)}.")
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"Unsupported source extension: {suffix or '(none)'}")
    content_hash = file_sha256(path)
    if suffix == ".pdf":
        pages, issues = _extract_pdf(path)
        _ocr_empty_pdf_pages(path, pages, issues)
    elif suffix == ".docx":
        pages, issues = _extract_docx(path)
    else:
        pages, issues = _extract_text(path)
    unknown_scopes = [
        name for name, value in {
            "campus_scope": campus_scope,
            "programme_scope": programme_scope,
            "batch_scope": batch_scope,
            "semester_scope": semester_scope,
        }.items() if not value
    ]
    if unknown_scopes:
        issues.append({
            "severity": "warning",
            "code": "UNKNOWN_SCOPE",
            "message": f"Applicability is unknown for: {', '.join(unknown_scopes)}.",
        })
    return {
        "schema_version": "extracted-document-v1",
        "document": {
            "document_id": f"{document_type.upper()}-{content_hash[:12]}",
            "document_type": document_type,
            "file_name": path.name,
            "content_hash": content_hash,
            "campus_scope": campus_scope,
            "programme_scope": programme_scope,
            "batch_scope": batch_scope,
            "semester_scope": semester_scope,
            "parser_version": "stdlib-poppler-docx-v2",
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "status": "review_needed",
        },
        "pages": pages,
        "issues": issues,
    }


def write_json_atomic(payload: dict[str, Any], output: str | Path) -> None:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        handle.write(encoded)
        temp_name = handle.name
    os.replace(temp_name, destination)
