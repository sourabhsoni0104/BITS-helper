from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import tempfile
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recommender.ingestion.documents import _candidate_type, extract_document, is_ignored_source
from recommender.ingestion.snapshots import file_sha256


PARSER_VERSION = "page-corpus-v4-docx-ocr"
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "in", "is", "it", "my", "of", "on", "or", "the", "this", "to", "what",
    "when", "where", "which", "who", "why", "with",
}


class CorpusError(RuntimeError):
    pass


def _discover_pdfs(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.is_dir():
        raise CorpusError(f"Corpus input directory not found: {root}")
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in {".pdf", ".docx"})


def _is_ignored_document(pages: list[dict[str, Any]]) -> bool:
    """Identify the repository's out-of-scope category-wise academic report."""
    text = "\n".join(str(page.get("text", "")) for page in pages).casefold()
    return bool(re.search(r"\bcategory[- ]wise\s+academic\s+structure\b", text))


def _is_profile_document(pages: list[dict[str, Any]]) -> bool:
    """Conservatively identify performance sheets and explicitly titled transcripts."""
    text = "\n".join(str(page.get("text", "")) for page in pages).casefold()
    if re.search(r"\bperformance\s+sheet\b", text) and all(
        re.search(rf"\b{label}\b", text)
        for label in ("completed courses", "registered courses")
    ):
        return True
    has_student_identity = bool(re.search(
        r"\b(?:student\s+(?:name|id|number)|name\s+of\s+student|student\s+no\.?|id\s+no\.?)\b",
        text,
    ))
    has_course_table = bool(re.search(r"\bcourse\s+(?:code|name|no\.?)\b", text))
    has_grade_column = bool(re.search(r"\b(?:grade|letter\s+grade)\b", text))
    first_page = next((str(page.get("text", "")) for page in pages), "")
    header_lines = "\n".join(first_page.splitlines()[:30])
    has_transcript_title = bool(re.search(
        r"^\s*(?:(?:bits|birla\s+institute\s+of\s+technology)\s*[-:|]?\s*)?"
        r"(?:academic\s+transcript|transcript\s+of\s+(?:the\s+)?academic\s+record|"
        r"transcript\s+of\s+records?|official\s+transcript|student\s+transcript)"
        r"(?:\s*[-–—:]\s*[^\n]*)?\s*$",
        header_lines,
        re.IGNORECASE | re.MULTILINE,
    ))
    return has_transcript_title and has_student_identity and has_course_table and has_grade_column


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            content_sha256 TEXT NOT NULL UNIQUE,
            primary_file_name TEXT NOT NULL,
            document_type TEXT NOT NULL,
            semester TEXT,
            page_count INTEGER NOT NULL,
            parser_version TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE sources (
            source_path TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            document_id TEXT NOT NULL REFERENCES documents(document_id)
        );
        CREATE TABLE pages (
            document_id TEXT NOT NULL REFERENCES documents(document_id),
            page_number INTEGER,
            unit_key TEXT NOT NULL,
            text TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            section TEXT,
            PRIMARY KEY (document_id, unit_key)
        );
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            document_id UNINDEXED,
            page_number UNINDEXED,
            file_name UNINDEXED,
            document_type UNINDEXED,
            semester UNINDEXED,
            section UNINDEXED,
            unit_key UNINDEXED,
            text,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def build_corpus(input_directory: str | Path, output_database: str | Path) -> dict[str, Any]:
    discovered_paths = _discover_pdfs(input_directory)
    if not discovered_paths:
        raise CorpusError(f"No PDF or DOCX files found under {input_directory}")
    all_grouped: dict[str, list[Path]] = defaultdict(list)
    for path in discovered_paths:
        all_grouped[file_sha256(path)].append(path)
    grouped: dict[str, list[Path]] = {}
    ignored_count = 0
    profile_count = 0
    for digest, aliases in all_grouped.items():
        if any(is_ignored_source(path) for path in aliases):
            ignored_count += len(aliases)
        elif any(_candidate_type(path) == "profile_history" for path in aliases):
            profile_count += len(aliases)
        else:
            grouped[digest] = aliases
    if not grouped:
        raise CorpusError(f"No non-profile PDF or DOCX files found under {input_directory}")

    destination = Path(output_database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    temporary.unlink()
    indexed_at = datetime.now(timezone.utc).isoformat()
    total_pages = 0
    empty_pages = 0
    indexed_source_count = 0
    indexed_document_count = 0
    reused_documents = 0
    previous: sqlite3.Connection | None = None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        _initialize(connection)
        if destination.is_file():
            try:
                previous = sqlite3.connect(destination)
                previous.row_factory = sqlite3.Row
                previous.execute("SELECT document_id FROM documents LIMIT 1")
            except sqlite3.Error:
                if previous:
                    previous.close()
                previous = None
        eligible_groups = sorted(grouped.items())
        for position, (digest, aliases) in enumerate(eligible_groups, start=1):
            primary = aliases[0]
            document_type = _candidate_type(primary) or "reference"
            extracted = None
            if previous:
                try:
                    old = previous.execute("SELECT * FROM documents WHERE content_sha256=? AND parser_version=?", (digest, PARSER_VERSION)).fetchone()
                    if old:
                        old_pages = previous.execute("SELECT * FROM pages WHERE document_id=? ORDER BY COALESCE(page_number, 0), CAST(REPLACE(unit_key,'logical-','') AS INTEGER)", (old["document_id"],)).fetchall()
                        retry_ocr = primary.suffix.casefold() == '.pdf' and shutil.which('tesseract') and any(not page['text'] for page in old_pages)
                        if old_pages and not retry_ocr:
                            extracted = {"pages": [dict(page) for page in old_pages], 'document': {'semester_scope': old['semester']}}
                            reused_documents += 1
                except sqlite3.Error:
                    pass
            if extracted is None:
                extracted = extract_document(primary, document_type)
            pages = extracted["pages"]
            if _is_ignored_document(pages):
                ignored_count += len(aliases)
                continue
            if _is_profile_document(pages):
                profile_count += len(aliases)
                continue
            document_id = f"DOC-{digest[:20]}"
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, digest, primary.name, document_type, extracted.get("document", {}).get("semester_scope"), len(pages), PARSER_VERSION, indexed_at),
            )
            for alias in aliases:
                connection.execute(
                    "INSERT INTO sources VALUES (?, ?, ?)",
                    (str(alias), alias.name, document_id),
                )
            indexed_source_count += len(aliases)
            indexed_document_count += 1
            for page_index, page in enumerate(pages, start=1):
                text = page["text"]
                status = page["verification_status"]
                connection.execute(
                    "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
                    (document_id, page.get("page_number"), str(page.get("page_number") if page.get("page_number") is not None else f"logical-{page_index}"), text, status, page.get("section")),
                )
                if text:
                    connection.execute(
                        "INSERT INTO pages_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (document_id, page.get("page_number"), primary.name, document_type, extracted.get("document", {}).get("semester_scope"), page.get("section"), str(page.get('page_number') if page.get('page_number') is not None else f'logical-{page_index}'), text),
                    )
                else:
                    empty_pages += 1
                total_pages += 1
            if position == 1 or position % 25 == 0 or position == len(eligible_groups):
                print(f"corpus: {position}/{len(eligible_groups)} unique PDFs", flush=True)
        if indexed_document_count == 0:
            raise CorpusError("No indexable PDF documents remained after privacy and scope exclusions.")
        connection.commit()
        connection.execute("PRAGMA optimize")
        connection.close()
        connection = None
        if previous:
            previous.close()
            previous = None
        os.replace(temporary, destination)
    except Exception:
        if connection is not None:
            connection.close()
        if previous:
            previous.close()
        temporary.unlink(missing_ok=True)
        raise
    return {
        "database": str(destination),
        "discovered_pdfs": len(discovered_paths),
        "discovered_documents": len(discovered_paths),
        "reused_documents": reused_documents,
        "source_pdfs": indexed_source_count,
        "excluded_ignored_sources": ignored_count,
        "excluded_runtime_profiles": profile_count,
        "unique_documents": indexed_document_count,
        "duplicate_sources": indexed_source_count - indexed_document_count,
        "pages": total_pages,
        "empty_pages": empty_pages,
        "parser_version": PARSER_VERSION,
    }


def _fts_query(question: str) -> str:
    terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", question.casefold()) if term not in STOP_WORDS]
    if not terms:
        raise CorpusError("The question does not contain any searchable terms.")
    return " OR ".join(f'"{term}"' for term in terms[:20])


def _fts_conjunctive_query(question: str) -> str:
    terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", question.casefold()) if term not in STOP_WORDS]
    return " AND ".join(f'"{term}"' for term in terms[:20])


def search_corpus(database: str | Path, question: str, limit: int = 8, *, document_type: str | None = None, semester: str | None = None) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise CorpusError("Search limit must be between 1 and 50.")
    path = Path(database)
    if not path.is_file():
        raise CorpusError(f"Corpus database not found: {path}")
    query = _fts_query(question)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        sql = """
            SELECT document_id, page_number, file_name, document_type, semester, section, unit_key,
                   snippet(pages_fts, 7, '[', ']', ' … ', 36) AS excerpt,
                   bm25(pages_fts) AS rank
            FROM pages_fts
            WHERE pages_fts MATCH ? AND (? IS NULL OR document_type=?)
              AND (? IS NULL OR semester=? OR instr(lower(text), lower(?)) > 0)
            ORDER BY rank
            LIMIT ?
            """
        params = (document_type, document_type, semester, semester, semester, limit)
        strict_query = _fts_conjunctive_query(question)
        rows = connection.execute(sql, (strict_query, *params)).fetchall() if strict_query else []
        if len(rows) < limit:
            broad = connection.execute(sql, (query, *params)).fetchall()
            seen = {(row["document_id"], row["unit_key"]) for row in rows}
            rows.extend(row for row in broad if (row["document_id"], row["unit_key"]) not in seen)
            rows = rows[:limit]
        matches = []
        for row in rows:
            aliases = [
                value[0]
                for value in connection.execute(
                    "SELECT source_path FROM sources WHERE document_id = ? ORDER BY source_path",
                    (row["document_id"],),
                )
            ]
            matches.append({
                "document_id": row["document_id"],
                "file_name": row["file_name"],
                "document_type": row["document_type"],
                "page": row["page_number"],
                "section": row["section"],
                "unit_key": row["unit_key"],
                "semester": row["semester"],
                "excerpt": row["excerpt"],
                "source_paths": aliases,
                "retrieval_score": -row["rank"],
                "verification_status": "source_text_only",
            })
    except sqlite3.Error as error:
        raise CorpusError(f"Could not read corpus database: {error}") from error
    finally:
        if connection is not None:
            connection.close()
    return {
        "question": question,
        "matches": matches,
        "answer_status": "evidence_found" if matches else "not_found",
        "warning": "Retrieved text can answer general questions, but it is not a validated academic-rule decision.",
    }


@contextmanager
def _read_connection(database: str | Path):
    connection = None
    try:
        connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        yield connection
    except sqlite3.Error as exc:
        raise CorpusError(f'Could not read corpus database: {exc}') from exc
    finally:
        if connection is not None:
            connection.close()


def get_document(database: str | Path, document_id: str) -> dict[str, Any] | None:
    """Resolve metadata only by an indexed opaque document ID."""
    path = Path(database)
    if not path.is_file():
        raise CorpusError(f"Corpus database not found: {path}")
    with _read_connection(path) as db:
        row = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["sources"] = [item[0] for item in db.execute("SELECT source_path FROM sources WHERE document_id=? ORDER BY source_path", (document_id,))]
        value['units'] = [dict(item) for item in db.execute('SELECT page_number,unit_key,section FROM pages WHERE document_id=? ORDER BY COALESCE(page_number,0),CAST(REPLACE(unit_key,\'logical-\',\'\') AS INTEGER)', (document_id,))]
        return value


def get_document_page(database: str | Path, document_id: str, page: int | None = None, *, section: str | None = None, unit_key: str | None = None) -> dict[str, Any] | None:
    """Retrieve indexed text by document ID and page number; never opens a caller path."""
    with _read_connection(database) as db:
        if unit_key is not None:
            row = db.execute('SELECT * FROM pages WHERE document_id=? AND unit_key=?',(document_id,unit_key)).fetchone()
        elif page is None and section is not None:
            row = db.execute('SELECT * FROM pages WHERE document_id=? AND section=?',(document_id,section)).fetchone()
        elif page is None:
            row = db.execute("SELECT * FROM pages WHERE document_id=? ORDER BY COALESCE(page_number,0),CAST(REPLACE(unit_key,'logical-','') AS INTEGER) LIMIT 1", (document_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM pages WHERE document_id=? AND page_number=?", (document_id, page)).fetchone()
        return dict(row) if row else None


def library_stats(database: str | Path) -> dict[str, Any]:
    with _read_connection(database) as db:
        docs = db.execute("SELECT document_type, COUNT(*) count FROM documents GROUP BY document_type ORDER BY document_type").fetchall()
        pages = db.execute("SELECT COUNT(*) total, SUM(CASE WHEN trim(text)='' THEN 1 ELSE 0 END) empty FROM pages").fetchone()
        sem = db.execute("SELECT COUNT(*) known FROM documents WHERE semester IS NOT NULL AND semester != ''").fetchone()["known"]
        return {"documents": sum(row["count"] for row in docs), "by_document_type": {row["document_type"]: row["count"] for row in docs}, "pages_or_logical_units": pages["total"] or 0, "empty_pages": pages["empty"] or 0, "documents_with_semester": sem, "semester_metadata_status": "partial" if sem else "unknown"}
