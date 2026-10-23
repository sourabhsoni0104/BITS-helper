"""Resolve course-detail questions to the corresponding teaching handout."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from recommender.ingestion.corpus import CorpusError, _read_connection


_CODE = re.compile(r"\b([A-Z]{2,5})[\s_-]*([A-Z]\d{3}[A-Z]?(?:-\d+)?)\b", re.I)
_DETAIL = re.compile(
    r"\b(?:handout|syllabus|topics?|taught|teach\w*|cover\w*|modules?|"
    r"lectures?|curriculum|instructors?|professors?|textbooks?|prerequisites?|"
    r"assessments?|exams?|weightage|grading|evaluation)\b", re.I,
)
_FILLER = {
    "a", "an", "and", "are", "as", "at", "be", "bitsbuddy", "by", "can",
    "computer", "course", "do", "does", "for", "from", "give", "handout",
    "i", "in", "include", "is", "it", "its", "me", "of", "on", "please",
    "science", "should", "show", "syllabus", "taught", "teach", "tell", "that",
    "the", "their", "this", "to", "topic", "under", "what", "which", "with",
}


def _words(text: str) -> list[str]:
    return [word[:-1] if len(word) > 4 and word.endswith("s") else word
            for word in re.findall(r"[a-z]+", text.casefold())]


def _matches(left: str, right: str) -> bool:
    return left == right or (min(len(left), len(right)) >= 4 and
                             (left.startswith(right) or right.startswith(left)))


def course_handout_sources(
    database: str | Path, question: str, previous_question: str = "", limit: int = 5,
) -> dict[str, Any] | None:
    """Return scoped sources, or None when ordinary cross-library search applies.

    Missing/ambiguous course handouts produce no passages, so a bulletin or an
    unrelated course cannot silently stand in for the requested teaching plan.
    """
    general_source = re.search(r"\b(?:bulletin|regulations|timetable)\b", question, re.I)
    rejected_source = re.search(r"\b(?:not|instead of|rather than)\s+(?:the\s+)?(?:bulletin|regulations|timetable)\b", question, re.I)
    if general_source and not (rejected_source or re.search(r"\bhandout\b", question, re.I)):
        return None
    if not (_DETAIL.search(question) or _CODE.search(question) or (rejected_source and previous_question)):
        return None
    if not Path(database).is_file():
        raise CorpusError(f"Corpus database not found: {database}")
    with _read_connection(database) as db:
        rows = db.execute(
            "SELECT d.document_id,d.primary_file_name,p.text FROM documents d "
            "JOIN pages p USING(document_id) WHERE d.document_type='handout' AND p.page_number=1"
        ).fetchall()
        courses = []
        for row in rows:
            title = re.search(r"(?im)^\s*Course\s+Title\s*:?\s*(.+)$", row["text"])
            code = _CODE.search(row["primary_file_name"]) or _CODE.search(row["text"])
            if title and code:
                courses.append({
                    "document_id": row["document_id"], "file_name": row["primary_file_name"],
                    "title": title[1].strip(), "code": f"{code[1].upper()} {code[2].upper()}",
                })

        def ranked(query: str) -> list[tuple[float, dict]]:
            codes = {f"{match[0].upper()} {match[1].upper()}" for match in _CODE.findall(query)}
            words = _words(query)
            candidates = []
            for course in courses:
                if codes:
                    if course["code"] in codes:
                        candidates.append((10.0, course))
                    continue
                title_words = set(_words(course["title"])) - {"for", "and", "of", "the", "to", "in"}
                matches = sum(any(_matches(word, term) for term in words) for word in title_words)
                if title_words and matches >= min(2, len(title_words)) and matches / len(title_words) >= 0.6:
                    candidates.append((matches / len(title_words) + matches * 0.1, course))
            return sorted(candidates, key=lambda item: (-item[0], item[1]["code"], item[1]["file_name"]))

        candidates = ranked(question)
        # A newly named course takes precedence over the previous question.
        # An explicit unknown code must never fall back to a different course.
        followup = not re.search(r"\b(?:in|for)\b", question, re.I) or re.search(r"\b(?:it|its|this course|that course)\b", question, re.I)
        if not candidates and not _CODE.search(question) and previous_question and followup:
            candidates = ranked(previous_question)
        if not candidates:
            return {"status": "not_found", "citations": []}
        best_score, course = candidates[0]
        close = [item[1] for item in candidates if best_score - item[0] < 0.15]
        if len(close) > 1:
            return {"status": "ambiguous", "courses": close, "citations": []}

        focus = set(_words(question)) - set(_words(course["title"])) - set(_words(course["code"])) - _FILLER
        focus = {word for word in focus if len(word) >= 3}
        if re.search(r"\b(?:who|instructor|professor)\b", question, re.I):
            focus.add("instructor")
        topic_question = bool(re.search(r"\b(?:topics?|syllabus|taught|cover\w*|teach\w*|learn\w*)\b", question, re.I)) and not re.search(r"\bwho\b", question, re.I)
        pages = db.execute(
            "SELECT page_number,unit_key,section,text FROM pages WHERE document_id=? ORDER BY page_number",
            (course["document_id"],),
        ).fetchall()
        passages = []
        for page in pages:
            words = _words(page["text"])
            score = sum(any(_matches(word, term) for term in focus) for word in words)
            is_plan = bool(re.search(r"\bcourse\s+plan\b", page["text"], re.I))
            if topic_question and is_plan:
                score += 4
            passages.append((score, {
                "document_id": course["document_id"], "file_name": course["file_name"],
                "document_type": "handout", "page": page["page_number"],
                "unit_key": page["unit_key"], "section": page["section"],
                "excerpt": page["text"],
            }))
        passages.sort(key=lambda item: (-item[0], item[1]["page"] or 0))
        relevant = [passage for score, passage in passages if score > 0]
        citations = (relevant or [passage for _, passage in passages[:1]])[:limit]
        for number, citation in enumerate(citations, 1):
            citation["number"] = number
        return {"status": "matched", "course": course, "citations": citations}
