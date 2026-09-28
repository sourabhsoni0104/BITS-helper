from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from recommender.ingestion.corpus import CorpusError, search_corpus
from recommender.models import Constraint, DatasetSnapshot, DecisionStatus, QueryIntent, StudentProfile, to_dict
from recommender.policies.engine import analyze_requirements, evaluate_eligibility, resolve_policy_context
from recommender.services.recommendation import recommend_from_intent


class ChatToolClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


FIELDS = ("midsem_present", "attendance_required", "project_present")
CATEGORIES = ("CDC", "DEL", "HUEL", "OPEL", "ANY")
PLAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "build_recommendation_plan",
            "description": "Translate the student's request into a structured retrieval and validation plan. Do not select course IDs or invent academic facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "requested_category": {"type": "string", "enum": list(CATEGORIES)},
                    "topic_preferences": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "hard_constraints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": list(FIELDS)},
                                "value": {"type": "boolean"},
                            },
                            "required": ["field", "value"],
                            "additionalProperties": False,
                        },
                        "maxItems": 6,
                    },
                    "soft_preferences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": list(FIELDS)},
                                "value": {"type": "boolean"},
                            },
                            "required": ["field", "value"],
                            "additionalProperties": False,
                        },
                        "maxItems": 6,
                    },
                    "search_query": {"type": "string"},
                },
                "required": ["requested_category", "topic_preferences", "hard_constraints", "soft_preferences", "search_query"],
                "additionalProperties": False,
            },
        },
    },
]
SYSTEM_PROMPT = """You plan retrieval for a BITS academic course recommender.
Call build_recommendation_plan exactly once. Use both the complete student profile and supplied dataset summary. Extract only preferences stated or clearly implied by the request.
Use ANY when no CDC, DEL, HUEL, or OPEL category is requested. A required property is hard; words such as prefer make it soft.
Never choose courses or invent rules, prerequisites, availability, or handout facts. Deterministic tools handle those after your plan."""


class AcademicAgent:
    def __init__(self, client: ChatToolClient, corpus_path: str | Path, *, max_iterations: int = 2) -> None:
        self.client = client
        self.corpus_path = Path(corpus_path)
        self.max_iterations = max_iterations

    def _requirements(self, profile: StudentProfile, snapshot: DatasetSnapshot) -> dict[str, Any]:
        context = resolve_policy_context(profile, snapshot)
        return {
            "context_id": context.context_id,
            "requirements": [to_dict(item) for item in analyze_requirements(profile, context, snapshot)],
            "evidence_ids": list(context.evidence_ids),
        }

    def _eligible(self, profile: StudentProfile, snapshot: DatasetSnapshot, category: str) -> dict[str, Any]:
        requested_category = None if category == "ANY" else category
        context = resolve_policy_context(profile, snapshot)
        courses: list[dict[str, Any]] = []
        seen: set[str] = set()
        for offering in snapshot.offerings:
            if offering.campus.casefold() != profile.campus.casefold() or offering.semester_id != profile.target_semester_id:
                continue
            eligibility = evaluate_eligibility(profile, offering, requested_category, context, snapshot)
            if eligibility.overall_status != DecisionStatus.PASS or offering.course_id in seen:
                continue
            seen.add(offering.course_id)
            course = snapshot.courses[offering.course_id]
            courses.append({
                "course_id": course.course_id,
                "code": course.code,
                "title": course.title,
                "topics": list(course.topics[:6]),
                "handout_facts": {
                    name: {"value": fact.value, "verification_status": fact.verification_status.value}
                    for name, fact in offering.handout_facts.items()
                },
                "evidence_ids": sorted(set(course.evidence_ids + offering.evidence_ids)),
            })
        return {"category": requested_category, "eligible_courses": courses[:40], "count": len(courses)}

    def _search(self, snapshot: DatasetSnapshot, query: str, course_ids: list[str]) -> dict[str, Any]:
        allowed = set(course_ids)
        evidence_to_courses: dict[str, set[str]] = {}
        for course_id in allowed:
            course = snapshot.courses.get(course_id)
            if course is None:
                continue
            for evidence_id in course.evidence_ids:
                document_id = snapshot.evidence[evidence_id].document_id if evidence_id in snapshot.evidence else None
                if document_id:
                    evidence_to_courses.setdefault(document_id, set()).add(course_id)
        matches: list[dict[str, Any]] = []
        if self.corpus_path.is_file():
            try:
                result = search_corpus(self.corpus_path, query, limit=30, document_type="handout")
            except CorpusError:
                result = {"matches": []}
            for match in result["matches"]:
                for course_id in sorted(evidence_to_courses.get(match["document_id"], set())):
                    course = snapshot.courses[course_id]
                    matches.append({
                        "course_id": course_id,
                        "code": course.code,
                        "title": course.title,
                        "page": match.get("page"),
                        "excerpt": match.get("excerpt"),
                    })
        if not matches:
            terms = [term for term in query.casefold().split() if len(term) > 2]
            for course_id in sorted(allowed):
                course = snapshot.courses.get(course_id)
                if course is None:
                    continue
                text = " ".join((course.code, course.title, *course.topics)).casefold()
                if any(term in text for term in terms):
                    matches.append({"course_id": course_id, "code": course.code, "title": course.title, "topics": list(course.topics[:6])})
        unique: dict[str, dict[str, Any]] = {}
        for match in matches:
            unique.setdefault(match["course_id"], match)
        return {"matches": list(unique.values())[:30], "count": len(unique)}

    def _validate(
        self,
        profile: StudentProfile,
        snapshot: DatasetSnapshot,
        original_query: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        category = arguments.get("requested_category")
        requested_category = None if category == "ANY" else category
        model_hard = tuple(Constraint(str(item["field"]), "eq", bool(item["value"])) for item in arguments.get("hard_constraints", []))
        model_soft = tuple(Constraint(str(item["field"]), "eq", bool(item["value"])) for item in arguments.get("soft_preferences", []))
        hard = tuple(dict.fromkeys(model_hard))
        soft = tuple(item for item in dict.fromkeys(model_soft) if item not in hard)
        topics = tuple(dict.fromkeys(str(item).strip() for item in arguments.get("topic_preferences", []) if str(item).strip()))
        intent = QueryIntent(
            requested_category=requested_category,
            topic_preferences=topics,
            hard_constraints=hard,
            soft_preferences=soft,
            ambiguities=(),
            clarification_questions=(),
            original_query=original_query,
        )
        result = recommend_from_intent(profile, intent, snapshot)
        requested = {str(item) for item in arguments.get("course_ids", [])}
        result["recommendations"] = [item for item in result["recommendations"] if item["course_id"] in requested]
        if requested and not result["recommendations"] and result.get("no_result_reason") is None:
            result["no_result_reason"] = "The proposed shortlist did not pass deterministic academic and preference validation."
        return result

    def _plan(self, profile: StudentProfile, query: str, snapshot: DatasetSnapshot) -> dict[str, Any]:
        profile_summary = {
            "campus": profile.campus,
            "admission_year": profile.admission_year,
            "programme_ids": list(profile.programme_ids),
            "current_semester": profile.current_semester,
            "target_semester_id": profile.target_semester_id,
            "minor_id": profile.minor_id,
            "interests": list(profile.interests),
            "course_attempts": [{"course_id": item.course_id, "status": item.status.value} for item in profile.attempts],
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "student_profile": profile_summary,
                "dataset": {
                    "dataset_version": snapshot.dataset_version,
                    "course_count": len(snapshot.courses),
                    "offering_count": len(snapshot.offerings),
                    "policy_context_count": len(snapshot.policy_contexts),
                    "campuses": sorted({item.campus for item in snapshot.offerings}),
                    "semesters": sorted({item.semester_id for item in snapshot.offerings}),
                },
                "request": query,
            })},
        ]
        for _ in range(self.max_iterations):
            response = self.client.complete(messages, PLAN_TOOLS)
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            for call in tool_calls:
                function = call.get("function") or {}
                if function.get("name") == "build_recommendation_plan":
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be a JSON object.")
                    return arguments
            messages.append({"role": "assistant", "content": str(message.get("content") or "")})
            messages.append({"role": "user", "content": "Call build_recommendation_plan now."})
        raise RuntimeError("The academic agent did not produce a structured recommendation plan.")

    def _explain(self, query: str, validated: dict[str, Any]) -> tuple[str, str]:
        courses = [{
            "code": item["course_code"],
            "title": item["title"],
            "matched_topics": item["matched_topics"],
            "requirement": item["requirement_contribution"],
            "properties": item["relevant_handout_properties"],
        } for item in validated["recommendations"][:5]]
        if not courses:
            return validated.get("no_result_reason") or "No validated recommendation is available.", "deterministic_fallback"
        messages = [
            {"role": "system", "content": "Explain only the validated course records supplied. Do not add rules or properties. Use at most three short sentences."},
            {"role": "user", "content": json.dumps({"request": query, "validated_courses": courses})},
        ]
        try:
            response = self.client.complete(messages, [])
            summary = str(response["choices"][0]["message"].get("content") or "").strip()
            if summary:
                return summary, "ok"
        except Exception:
            pass
        codes = ", ".join(item["course_code"] for item in validated["recommendations"][:5])
        return f"The deterministic academic validator approved: {codes}.", "deterministic_fallback"

    def run(self, profile: StudentProfile, query: str, snapshot: DatasetSnapshot) -> dict[str, Any]:
        plan = self._plan(profile, query, snapshot)
        category = str(plan.get("requested_category") or "ANY")
        if category not in CATEGORIES:
            category = "ANY"
        trace = [{"tool": "build_recommendation_plan", "status": "ok"}]
        self._requirements(profile, snapshot)
        trace.append({"tool": "analyze_requirements", "status": "ok"})
        eligible = self._eligible(profile, snapshot, category)
        trace.append({"tool": "find_eligible_courses", "status": "ok"})
        eligible_ids = [item["course_id"] for item in eligible["eligible_courses"]]
        topic_preferences = tuple(dict.fromkeys(str(item) for item in plan.get("topic_preferences") or []))
        has_preferences = bool(topic_preferences or plan.get("hard_constraints") or plan.get("soft_preferences"))
        matches: list[dict[str, Any]] = []
        if has_preferences:
            search_terms = [str(plan.get("search_query") or ""), *topic_preferences]
            search = self._search(snapshot, " ".join(dict.fromkeys(item.strip() for item in search_terms if item.strip())), eligible_ids)
            matches = search["matches"]
            trace.append({"tool": "search_handouts", "status": "ok"})
        if topic_preferences:
            shortlist = [item["course_id"] for item in matches]
        else:
            shortlist = eligible_ids
        arguments = {
            "course_ids": shortlist[:12],
            "requested_category": category,
            "topic_preferences": list(topic_preferences),
            "hard_constraints": plan.get("hard_constraints") or [],
            "soft_preferences": plan.get("soft_preferences") or [],
        }
        validated = self._validate(profile, snapshot, query, arguments)
        trace.append({"tool": "validate_recommendations", "status": "ok"})
        final_text, explanation_status = self._explain(query, validated)
        trace.append({"tool": "explain_validated_results", "status": explanation_status})
        validated["agent"] = {
            "mode": "groq_planner_with_deterministic_tools",
            "model": self.client.model,
            "summary": final_text,
            "trace": trace,
        }
        return validated
