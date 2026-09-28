from __future__ import annotations

import re

from recommender.models import Constraint, QueryIntent


KNOWN_CATEGORIES = ("CDC", "DEL", "HUEL", "OPEL")


def parse_query(query: str) -> QueryIntent:
    text = query.strip()
    lowered = text.casefold()
    category_matches = {
        item for item in KNOWN_CATEGORIES
        if re.search(rf"\b{item.casefold()}s?\b", lowered)
    }
    
    
    ordered_matches = [item for item in KNOWN_CATEGORIES if item in category_matches]
    category = ordered_matches[0] if len(ordered_matches) == 1 else None
    topics: list[str] = []
    topic_match = re.search(r"\b(ai|artificial intelligence|machine learning|ml)\b", lowered)
    hard: list[Constraint] = []
    soft: list[Constraint] = []
    ambiguities: list[str] = []
    questions: list[str] = []
    if re.search(r"\b(no|without)\s+(a\s+)?mid[- ]?sem", lowered):
        hard.append(Constraint("midsem_present", "eq", False))
    if re.search(r"\b(no|without)\s+(an?\s+)?attendance (requirement|policy)", lowered):
        hard.append(Constraint("attendance_required", "eq", False))
    if len(category_matches) > 1:
        ambiguities.append("multiple requested categories")
        questions.append("Which single category should I use: " + ", ".join(sorted(category_matches)) + "?")
    elif category:
        category_pattern = rf"\b{category.casefold()}s?\b"
        if re.search(rf"\b(?:not|no|without)\s+(?:the\s+)?{category_pattern}", lowered):
            category = None
            ambiguities.append("negated category request")
            questions.append("Did you mean to exclude this category, or is it your requested category?")

    if topic_match:
        before_topic = lowered[max(0, topic_match.start() - 30):topic_match.start()]
        if re.search(r"\b(?:(?:not|no|without|excluding)\s+(?:an?\s+)?|(?:do\s+not|don't)\s+want\s+(?:an?\s+)?)$", before_topic):
            ambiguities.append("negated topic request")
            questions.append("Did you mean to exclude artificial intelligence, or are you looking for it?")
        else:
            topics.append("artificial intelligence")

    project_match = re.search(r"\bproject(?:s|[- ]based)?\b", lowered)
    if project_match:
        project_mentions = list(re.finditer(r"\bproject(?:s|[- ]based)?\b", lowered))
        has_negative_project = bool(re.search(
            r"\b(?:no|without|avoid|excluding|not)\s+(?:a\s+|any\s+)?project(?:s|[- ]based)?\b"
            r"|\b(?:do\s+not|don't)\s+want\s+(?:any\s+)?project(?:s|[- ]based)?\b",
            lowered,
        ))
        has_positive_project = bool(re.search(
            r"\b(?:prefer|like|want|need|require|requires|must\s+(?:have|include|take)|only)\s+(?:a\s+|an\s+)?project(?:s|[- ]based)?\b"
            r"|\bproject(?:s|[- ]based)?\s+(?:is\s+)?(?:preferred|required|mandatory)\b",
            lowered,
        ))
        if len(project_mentions) > 1 and has_negative_project and has_positive_project:
            ambiguities.append("conflicting project preferences")
            questions.append("Should I include or exclude courses with projects?")
            project_match = None
    if project_match:
        before_project = lowered[max(0, project_match.start() - 48):project_match.start()]
        after_project = lowered[project_match.end():project_match.end() + 36]
        if re.match(r"\s+(?:(?:is|are)\s+)?not\s+(?:required|mandatory)\b", after_project) or re.search(r"\bnot\s+necessarily\s+(?:with\s+)?$", before_project):
            ambiguities.append("optional project preference")
            questions.append("Are projects acceptable, preferred, or should they be excluded?")
            project_match = None
    if project_match:
        negative = bool(re.search(
            r"(?:\b(?:no|without|avoid|excluding|not)\s+(?:a\s+|any\s+)?|\bdo\s+not\s+want\s+(?:any\s+)?|\bdon't\s+want\s+(?:any\s+)?)$",
            before_project,
        )) or bool(re.match(r"\s+(?:is\s+)?not\b", after_project))
        if negative:
            hard.append(Constraint("project_present", "eq", False))
        else:
            locally_required = bool(re.search(
                r"\b(?:must\s+(?:have|include|take)|require|requires|need|only(?:\s+want)?)\s+(?:a\s+|an\s+)?$",
                before_project,
            )) or bool(re.match(r"\s+(?:(?:is|are)\s+)?(?:required|mandatory)\b", after_project))
            (hard if locally_required else soft).append(Constraint("project_present", "eq", True))

    if "lenient" in lowered and re.search(r"\bmake[- ]?up\b", lowered):
        ambiguities.append("lenient makeup policy")
        questions.append("What should count as a lenient makeup policy (for example, no documentation requirement or broad allowed reasons)?")
    return QueryIntent(category, tuple(topics), tuple(hard), tuple(soft), tuple(ambiguities), tuple(questions), text)
