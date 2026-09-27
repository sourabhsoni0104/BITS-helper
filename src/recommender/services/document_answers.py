from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from recommender.ingestion.corpus import STOP_WORDS, get_document_page, search_corpus


_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", re.IGNORECASE)


def _terms(text: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(text) if word.casefold() not in STOP_WORDS}


def _passages(text: str) -> list[str]:
    """Split extracted text conservatively, preserving complete source sentences."""
    output: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", text):
        lines = [" ".join(line.split()).strip() for line in paragraph.splitlines() if line.strip()]
        
        
        units = lines if any("|" in line for line in lines) else [" ".join(lines)]
        for unit in units:
            parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", unit)
            output.extend(part.strip() for part in parts if part.strip())
    return output


def answer_question(
    database: str | Path,
    question: str,
    limit: int = 5,
    document_type: str | None = None,
    semester: str | None = None,
) -> dict[str, Any]:
    """Answer with relevant complete source passages and numbered citations only."""
    limit = max(1, min(int(limit), 5))
    result = search_corpus(database, question, limit, document_type=document_type, semester=semester)
    query_terms = _terms(question)
    
    
    minimum_overlap = 1 if len(query_terms) <= 2 else 2
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, str | None, str]] = set()
    seen_passages: set[str] = set()
    for match in result["matches"]:
        page = get_document_page(
            database,
            match["document_id"],
            match.get("page"),
            section=match.get("section"),
            unit_key=match.get("unit_key"),
        )
        if not page:
            continue
        for passage in _passages(str(page.get("text") or "")):
            terms = _terms(passage)
            overlap = query_terms & terms
            if len(overlap) < minimum_overlap:
                continue
            normalized = " ".join(_WORD.findall(passage.casefold()))
            key = (match["document_id"], match.get("page"), match.get("section"), normalized)
            if key in seen or normalized in seen_passages:
                continue
            seen.add(key)
            seen_passages.add(normalized)
            candidates.append({
                "document_id": match["document_id"],
                "page": match.get("page"),
                "unit_key": match.get("unit_key"),
                "section": match.get("section"),
                "file_name": match["file_name"],
                "excerpt": passage,
                "_overlap": len(overlap),
                "_rank": match.get("retrieval_score", 0.0),
            })
    candidates.sort(key=lambda item: (-item["_overlap"], -item["_rank"], item["file_name"], item["page"] or 0))
    citations = [{key: value for key, value in item.items() if not key.startswith("_")} for item in candidates[:limit]]
    if not citations:
        return {
            "question": question,
            "answer_status": "not_found",
            "mode": "extractive",
            "answer": None,
            "citations": [],
            "warning": "No sufficiently relevant source passage was found; no answer was generated.",
        }
    bullets = [f"- {citation['excerpt']} [{index}]" for index, citation in enumerate(citations, start=1)]
    return {
        "question": question,
        "answer_status": "evidence_found",
        "mode": "extractive",
        "answer": "\n".join(bullets),
        "citations": citations,
        "warning": "Each bullet quotes an extracted source passage. Passages are not synthesized into a rule, and source applicability or conflicts are not resolved.",
    }


def get_source_page(database: str | Path, document_id: str, page: int | None = None) -> dict[str, Any] | None:
    """Compatibility helper for dashboard callers needing one verified corpus page."""
    from recommender.ingestion.corpus import get_document_page

    return get_document_page(database, document_id, page)
