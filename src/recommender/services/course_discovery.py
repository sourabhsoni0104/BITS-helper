from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from recommender.ingestion.corpus import CorpusError, search_corpus
from recommender.models import AttemptStatus, StudentProfile


TOKEN = re.compile(r"[a-z0-9]+")
QUERY_FILLER = {
    "a", "about", "all", "an", "and", "anything", "are", "course", "courses",
    "different", "elective", "electives", "find", "for", "from", "give", "good",
    "i", "in", "interesting", "is", "kind", "looking", "me", "my", "of", "or",
    "please", "recommend", "show", "some", "something", "suggest", "that", "the",
    "teach", "teaches", "teaching", "to", "want", "with",
}
TEXT_FILLER = QUERY_FILLER | {
    "also", "been", "being", "book", "class", "course", "details", "handout", "has",
    "have", "into", "its", "more", "other", "semester", "students", "study", "their",
    "these", "this", "through", "using", "will",
}


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(TOKEN.findall(value.casefold()))


def _query_terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(token for token in _tokens(value) if len(token) > 1 and token not in QUERY_FILLER))


def _matches(term: str, candidate: str) -> bool:
    if term == candidate:
        return True
    return len(term) >= 5 and len(candidate) >= 5 and term[:5] == candidate[:5]


def _term_match(term: str, title: str, source: str, title_tokens: tuple[str, ...], source_tokens: tuple[str, ...], acronyms: set[str]) -> tuple[bool, bool]:
    if term in {"ai", "ml"}:
        patterns = ("artificial intelligence", "machine learning", "deep learning", "neural network", "neural networks")
        title_match = term in title_tokens or any(phrase in title for phrase in patterns)
        source_match = any(phrase in source for phrase in patterns)
        return title_match, source_match
    return (
        term in acronyms or any(_matches(term, candidate) for candidate in title_tokens),
        any(_matches(term, candidate) for candidate in source_tokens),
    )


def _title_acronyms(value: str) -> set[str]:
    words = [token for token in _tokens(value) if token.isalpha() and token not in TEXT_FILLER]
    acronyms = {"".join(word[0] for word in words)} if len(words) > 1 else set()
    acronyms.update("".join(word[0] for word in words[start:start + size])
                    for size in range(2, min(5, len(words)) + 1)
                    for start in range(len(words) - size + 1))
    return {value for value in acronyms if len(value) > 1}


def _reference_ids(course: dict[str, Any], offerings: list[dict[str, Any]]) -> list[str]:
    values = [str(value) for value in course.get("evidence_ids", []) if value]
    for offering in offerings:
        values.extend(str(value) for value in offering.get("evidence_ids", []) if value)
        for fact in offering.get("handout_facts", {}).values():
            if isinstance(fact, dict):
                values.extend(str(value) for value in fact.get("evidence_ids", []) if value)
    return list(dict.fromkeys(values))


def _corpus_ranks(corpus_path: str | Path | None, query: str) -> tuple[dict[str, int], dict[str, str]]:
    if corpus_path is None or not Path(corpus_path).is_file():
        return {}, {}
    try:
        matches = search_corpus(corpus_path, query, limit=50, document_type="handout")["matches"]
    except (CorpusError, OSError):
        return {}, {}
    ranks: dict[str, int] = {}
    excerpts: dict[str, str] = {}
    for index, match in enumerate(matches):
        document_id = str(match["document_id"])
        ranks[document_id] = max(ranks.get(document_id, 0), 50 - index)
        excerpts.setdefault(document_id, str(match.get("excerpt") or ""))
    return ranks, excerpts


def discover_courses(
    profile: StudentProfile,
    query: str,
    snapshot: dict[str, Any],
    *,
    corpus_path: str | Path | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    evidence = {
        str(item.get("evidence_id")): item
        for item in snapshot.get("evidence", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    all_offerings = [item for item in snapshot.get("offerings", []) if isinstance(item, dict)]
    matching_offerings = [
        item for item in all_offerings
        if str(item.get("campus", "")).casefold() == profile.campus.casefold()
        and str(item.get("semester_id", "")) == profile.target_semester_id
        and not (
            profile.admission_year < 2026
            and str(item.get("component_code", "")).isdigit()
            and int(str(item["component_code"])) >= 5000
        )
    ]
    if all_offerings and not matching_offerings:
        return {
            "recommendations": [],
            "no_result_reason": f"The supplied documents contain no {profile.campus} offerings for {profile.target_semester_id}.",
        }
    offerings_by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offering in matching_offerings:
        offerings_by_course[str(offering.get("course_id") or "")].append(offering)
    allowed = set(offerings_by_course) if all_offerings else None
    excluded = {
        attempt.course_id.casefold()
        for attempt in profile.attempts
        if attempt.status in {AttemptStatus.COMPLETED, AttemptStatus.IN_PROGRESS}
    }
    requested_terms = _query_terms(query)
    interest_terms = tuple(dict.fromkeys(term for interest in profile.interests for term in _query_terms(interest)))
    search_terms = requested_terms or interest_terms
    corpus_ranks, corpus_excerpts = _corpus_ranks(corpus_path, " ".join(search_terms)) if search_terms else ({}, {})
    ranked: list[tuple[int, str, str, tuple[str, ...], dict[str, Any]]] = []
    for course in snapshot.get("courses", []):
        if not isinstance(course, dict):
            continue
        course_id = str(course.get("course_id") or "")
        code = str(course.get("code") or course_id)
        title = str(course.get("title") or code)
        if not course_id or (allowed is not None and course_id not in allowed):
            continue
        if course_id.casefold() in excluded or code.casefold() in excluded:
            continue
        references = _reference_ids(course, offerings_by_course.get(course_id, []))
        structured_topics = " ".join(str(item) for item in course.get("topics", []) if item)
        documents = {
            str(evidence[reference].get("document_id"))
            for reference in references
            if reference in evidence and evidence[reference].get("document_id")
        }
        source_text = " ".join([structured_topics, *(str(evidence[reference].get("excerpt") or "") for reference in references if reference in evidence)])
        source_text = " ".join([source_text, *(corpus_excerpts.get(document, "") for document in documents)])
        normalized_title = f"{code} {title}".casefold()
        normalized_source = source_text.casefold()
        title_tokens = _tokens(normalized_title)
        source_tokens = _tokens(normalized_source)
        acronyms = _title_acronyms(title)
        matched_terms: list[str] = []
        score = max((corpus_ranks.get(document, 0) for document in documents), default=0)
        for term in search_terms:
            term_source = structured_topics.casefold() if term in {"ai", "ml"} else normalized_source
            term_source_tokens = _tokens(term_source) if term in {"ai", "ml"} else source_tokens
            title_match, source_match = _term_match(term, normalized_title, term_source, title_tokens, term_source_tokens, acronyms)
            if title_match or source_match:
                matched_terms.append(term)
                score += 24 if title_match else 7
        if search_terms and not matched_terms:
            continue
        substantive = [token for token in source_tokens if len(token) > 2 and token not in TEXT_FILLER and not token.isdigit()]
        if not search_terms:
            richness = len(set(substantive))
            if richness < 2:
                continue
            score = min(richness, 40)
        frequent = [token for token, _ in Counter(substantive).most_common(3)]
        if matched_terms:
            origin = "your request" if requested_terms else "your saved interests"
            reason = f"Matched {origin} in the indexed course title and handout: {', '.join(matched_terms)}"
        else:
            focus = ", ".join(frequent) if frequent else title
            reason = f"Documented course focus: {focus}"
        card = {
            "course_id": course_id,
            "course_code": code,
            "title": title,
            "matched_topics": [reason],
            "evidence_references": references,
            "badge": "Course match",
            "source_note": "Matched from the supplied course handouts.",
        }
        identity = tuple(sorted(documents)) or (re.sub(r"[^a-z0-9]+", "", title.casefold()),)
        ranked.append((-score, code.split()[0] if code.split() else "", code, identity, card))
    ordered = sorted(ranked, key=lambda item: item[:3])
    recommendations: list[dict[str, Any]] = []
    prefix_counts: dict[str, int] = defaultdict(int)
    seen_identities: set[tuple[str, ...]] = set()
    for _, prefix, _, identity, card in ordered:
        if identity in seen_identities:
            continue
        if not search_terms and prefix_counts[prefix] >= 2:
            continue
        recommendations.append(card)
        seen_identities.add(identity)
        prefix_counts[prefix] += 1
        if len(recommendations) >= min(limit, 12 if not search_terms else limit):
            break
    if recommendations:
        reason = None
    elif search_terms:
        reason = "No current course handout matched that request. Try a subject, skill, or course code."
    else:
        reason = "The supplied handouts did not contain enough course detail for broad discovery."
    return {"recommendations": recommendations, "no_result_reason": reason}
