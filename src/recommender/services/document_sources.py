"""Select evidence by the fact and level of detail the user requests."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from recommender.ingestion.corpus import _read_connection, search_corpus
from recommender.services.course_handouts import _CODE, _DETAIL, _words, course_handout_sources
from recommender.services.document_answers import answer_question


_CATALOGUE = re.compile(
    r"\b(?:course\s*(?:code|number|no\.?|id|title|name)|(?:code|number|title|name)\s+(?:of|for)|"
    r"its\s+(?:course\s+)?(?:code|number|title|name)|units?|credits?|catalogue|catalog|overview|brief\s+description)\b", re.I,
)
_POLICY = re.compile(
    r"\b(?:cgpa|gpa|graduat\w*|probation|degree|minor|dual.degree|regulations?|"
    r"eligibility|withdraw\w*|repeat\w*|registration|disciplinary)\b", re.I,
)
_PROCEDURE = re.compile(r"\b(?:how\s+(?:do|to|can)|submit|apply|application|forms?|procedure|instructions?|make.?up|thesis|joining)\b", re.I)
_SCHEDULE = re.compile(r"\b(?:timetable|schedule|timings?|venues?|rooms?|calendar|dates?|deadline|when)\b", re.I)


def source_policy(question: str, previous_question: str = "") -> dict[str, Any]:
    """An explicit source request wins; otherwise route by the requested facts."""
    cleaned = re.sub(r"\b(?:not|instead of|rather than)\s+(?:the\s+)?(?:bulletin|handout|regulations|timetable)\b", "", question, flags=re.I)
    explicit = []
    for kind, pattern in (("handout", r"handouts?"), ("bulletin", r"bulletin"),
                          ("regulations", r"(?:academic\s+)?regulations?"), ("timetable", r"timetable")):
        if re.search(rf"\b(?:{pattern})\b", cleaned, re.I):
            explicit.append(kind)
    catalogue = bool(_CATALOGUE.search(question))
    detail = bool(_DETAIL.search(question) or re.search(r"\b(?:more\s+details?|in\s+detail|detailed|comprehensive)\b", question, re.I))
    if explicit:
        groups = [explicit]
        intent = "explicit_source"
    elif re.search(r"\b(?:calendar|deadline|dates?\s+of\s+(?:semester|registration)|when.*semester)\b", question, re.I):
        groups, intent = [["reference"], ["timetable"], ["bulletin"]], "academic_calendar"
    elif _SCHEDULE.search(question):
        groups, intent = [["timetable"], ["reference", "handout"], ["bulletin"]], "schedule"
    elif catalogue and detail:
        groups, intent = [["bulletin", "handout"]], "catalogue_and_detail"
    elif _POLICY.search(question) and not _CODE.search(question) and not re.search(r"\bcourse\s*(?:code|number|title|name)\b", question, re.I) and not re.search(r"\b(?:submit|apply|application|form|procedure|instructions?|joining|thesis)\b", question, re.I):
        groups, intent = [["regulations"], ["reference"], ["bulletin"]], "academic_policy"
    elif catalogue:
        groups, intent = [["bulletin"], ["handout"]], "catalogue"
    elif _PROCEDURE.search(question) and re.search(r"\b(?:submit|apply|application|forms?|procedure|instructions?|joining|thesis|make.?up)\b", question, re.I):
        groups, intent = [["reference"], ["regulations"], ["bulletin"]], "procedure"
    elif _POLICY.search(question):
        groups, intent = [["regulations"], ["reference"], ["bulletin"]], "academic_policy"
    elif _PROCEDURE.search(question):
        groups, intent = [["reference"], ["regulations"], ["bulletin"]], "procedure"
    elif detail or re.search(r"\b(?:attendance|nc\s+criteria)\b", question, re.I):
        groups, intent = [["handout"], ["reference", "regulations"], ["bulletin"]], "course_detail"
    elif _CODE.search(question):
        groups, intent = [["bulletin"], ["handout"]], "catalogue"
    else:
        groups, intent = [["reference", "regulations", "timetable", "handout"], ["bulletin"]], "document_lookup"
    if re.search(r"\b(?:not|instead of|rather than)\s+(?:the\s+)?bulletin\b", question, re.I):
        groups = [[kind for kind in group if kind != "bulletin"] for group in groups]
        if previous_question and not any("handout" in group for group in groups):
            groups.insert(0, ["handout"])
    single_fact = (catalogue and not re.search(r"overview|description", question, re.I)) or re.search(r"\bwho\b", question, re.I)
    depth = "lookup" if single_fact else "summary"
    if re.search(r"\b(?:topics?|syllabus|curriculum|(?:lecture|course)\s+plan|detail\w*|explain|all|step.by.step|compare|comprehensive)\b", question, re.I):
        depth = "detailed"
    if re.search(r"\b(?:brief\w*|short|just|only|one.line)\b", question, re.I):
        depth = "brief"
    return {"intent": intent, "depth": depth, "source_priority": [group for group in groups if group]}


def _typed_sources(database: str | Path, query: str, kind: str, course: dict | None, limit: int) -> list[dict]:
    # Catalogue entries and timetables are tabular: preserve full pages so course
    # codes, titles, and column headers are not separated by sentence extraction.
    if kind in {"bulletin", "timetable"} and course:
        lookup = " ".join(filter(None, (course.get("code"), course.get("title"))))
        matches = search_corpus(database, lookup, limit=12, document_type=kind)["matches"]
        codes = {course["code"]} if course.get("code") else set()
        citations = []
        with _read_connection(database) as db:
            for match in matches:
                page = db.execute("SELECT text FROM pages WHERE document_id=? AND unit_key=?",
                                  (match["document_id"], match["unit_key"])).fetchone()
                if not page or (codes and not codes.intersection(f"{a.upper()} {b.upper()}" for a, b in _CODE.findall(page["text"]))):
                    continue
                citations.append({
                    "document_id": match["document_id"], "file_name": match["file_name"],
                    "page": match["page"], "unit_key": match["unit_key"], "section": match["section"],
                    "document_type": kind, "excerpt": page["text"],
                })
        title_words = set(_words(course.get("title", ""))) - {"and", "of", "the", "for", "in"}
        citations.sort(key=lambda item: -len(title_words.intersection(_words(item["excerpt"]))))
        return citations[:limit]
    citations = answer_question(database, query, limit=limit, document_type=kind)["citations"]
    if course and course.get("code"):
        # A matching keyword in a different course's reference passage is not
        # evidence about the requested course.
        with _read_connection(database) as db:
            scoped = []
            for citation in citations:
                page = db.execute("SELECT text FROM pages WHERE document_id=? AND unit_key=?",
                                  (citation["document_id"], citation["unit_key"])).fetchone()
                if page and course["code"] in {f"{a.upper()} {b.upper()}" for a, b in _CODE.findall(page["text"])}:
                    scoped.append(citation)
            citations = scoped
    for citation in citations:
        citation["document_type"] = kind
    return citations


def select_document_sources(database: str | Path, question: str, previous_question: str = "", limit: int = 5) -> dict[str, Any]:
    policy = source_policy(question, previous_question)
    query = question
    identity = course_handout_sources(database, question, previous_question, resolve_only=True)
    course = identity.get("course")
    if not course and _CODE.search(question):
        match = _CODE.search(question)
        course = {"code": f"{match[1].upper()} {match[2].upper()}", "title": ""}
    if identity["status"] == "ambiguous" and policy["intent"] in {"catalogue", "course_detail", "catalogue_and_detail"}:
        return {**policy, **identity}
    if course and policy["intent"] == "academic_policy" and _DETAIL.search(question) and not re.search(r"\b(?:cgpa|graduat\w*|degree|minor|probation)\b", question, re.I):
        policy.update(intent="course_detail", source_priority=[["handout"], ["reference"], ["bulletin"]])
    if not course and policy["intent"] == "course_detail" and re.search(r"\b(?:attendance|grading|prerequisites?|exam\w*)\b", question, re.I):
        policy.update(intent="academic_policy", source_priority=[["regulations"], ["reference"], ["bulletin"]])
    if previous_question and course and not _CODE.search(question):
        query = f"{course['code']} {course.get('title', '')} {question}"

    handout = None
    attempted = []
    for index, group in enumerate(policy["source_priority"]):
        citations = []
        for kind in group:
            attempted.append(kind)
            if kind == "handout":
                if course and identity["status"] == "matched":
                    handout_query = question if _DETAIL.search(question) or _CODE.search(question) else f"{question} {course['code']}"
                    handout = course_handout_sources(database, handout_query, previous_question, limit)
                    if handout:
                        citations.extend(handout.get("citations", []))
                elif policy["intent"] == "document_lookup":
                    citations.extend(_typed_sources(database, query, kind, None, limit))
            else:
                scoped_course = course if policy["intent"] in {"catalogue", "course_detail", "catalogue_and_detail", "schedule", "explicit_source"} else None
                citations.extend(_typed_sources(database, query, kind, scoped_course, min(limit, 2) if course else limit))
        seen = set()
        unique = []
        for citation in citations:
            key = (citation["document_id"], citation.get("unit_key"), citation.get("excerpt"))
            if key not in seen:
                seen.add(key)
                unique.append(citation)
        citations = unique[:limit]
        if citations:
            for number, citation in enumerate(citations, 1):
                citation["number"] = number
            required = group if policy["intent"] in {"catalogue_and_detail", "explicit_source"} else []
            missing = [kind for kind in required if kind not in {item["document_type"] for item in citations}]
            status = "partial" if missing else ("matched" if index == 0 else "fallback")
            return {**policy, "status": status, "missing_sources": missing, "course": course,
                    "sources_checked": attempted, "citations": citations,
                    "handout_selection": handout}
    return {**policy, "status": "not_found", "course": course, "sources_checked": attempted, "citations": []}
