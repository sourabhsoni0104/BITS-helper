from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from recommender.models import StudentProfile
from recommender.services.course_discovery import discover_courses


class StagedToolClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


PLAN_TOOLS = [{
    "type": "function",
    "function": {
        "name": "plan_dataset_search",
        "description": "Interpret the student's request using their academic profile and the supplied BITS dataset, then produce semantic retrieval terms.",
        "parameters": {
            "type": "object",
            "properties": {
                "interpreted_request": {"type": "string"},
                "search_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                },
                "needs_clarification": {"type": "boolean"},
                "clarification_question": {"type": "string"},
            },
            "required": ["interpreted_request", "search_terms", "needs_clarification", "clarification_question"],
            "additionalProperties": False,
        },
    },
}]
RERANK_TOOLS = [{
    "type": "function",
    "function": {
        "name": "rerank_staged_courses",
        "description": "Select only candidate course IDs that semantically match the student's request and explain each match from supplied candidate data.",
        "parameters": {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "course_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["course_id", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["selected"],
            "additionalProperties": False,
        },
    },
}]
PLAN_SYSTEM_PROMPT = """You are the intent-planning agent for BITSbuddy.
Call plan_dataset_search exactly once. Interpret the request using both the complete student profile and the supplied dataset summary.
Turn natural language, abbreviations, and interests into specific subject or course-property search terms; never copy conversational filler such as something, after, good, interesting, teach, or recommend into search terms.
Use completed and current courses to understand academic context, and use campus, batch, programmes, semester, minor, and interests when relevant.
If the request is ambiguous, refers to an unknown abbreviation, or asks for a sequence such as what comes after a course without enough context, set needs_clarification true and ask one concise question.
Do not select courses or invent rules, prerequisites, categories, availability, or document facts."""
RERANK_SYSTEM_PROMPT = """You are the semantic reranking agent for BITSbuddy.
Call rerank_staged_courses exactly once. Select only IDs from the supplied candidate list.
Use the student profile, interpreted request, and dataset-backed candidate facts together. Prefer direct subject, skill, evaluation, academic-state, or interest matches. Reject incidental keyword mentions and generic administrative text.
Return at most six selections. Keep each reason under fifteen words and base it only on the supplied title, extracted topics, and retrieval reason.
Do not claim eligibility, category, prerequisites, availability, or academic requirement satisfaction unless a supplied structured rule supports it."""


class StagedDiscoveryAgent:
    def __init__(self, client: StagedToolClient, corpus_path: str | Path, *, max_attempts: int = 2) -> None:
        self.client = client
        self.corpus_path = Path(corpus_path)
        self.max_attempts = max_attempts

    def _candidates(self, result: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        courses = {
            str(item.get("course_id")): item
            for item in snapshot.get("courses", [])
            if isinstance(item, dict) and item.get("course_id")
        }
        candidates = []
        for card in result.get("recommendations", [])[:12]:
            course = courses.get(str(card.get("course_id")), {})
            candidates.append({
                "course_id": card.get("course_id"),
                "code": card.get("course_code"),
                "title": card.get("title"),
                "topics": list(course.get("topics") or [])[:6],
                "retrieval_reason": " ".join(str(item) for item in card.get("matched_topics", []))[:180],
            })
        return candidates

    @staticmethod
    def _profile(profile: StudentProfile) -> dict[str, Any]:
        return {
            "campus": profile.campus,
            "admission_year": profile.admission_year,
            "programme_ids": list(profile.programme_ids),
            "current_semester": profile.current_semester,
            "target_semester_id": profile.target_semester_id,
            "minor_id": profile.minor_id,
            "interests": list(profile.interests),
            "course_attempts": [
                {"course_id": item.course_id, "status": item.status.value, "term_id": item.term_id}
                for item in profile.attempts
            ],
        }

    @staticmethod
    def _dataset(snapshot: dict[str, Any]) -> dict[str, Any]:
        courses = [item for item in snapshot.get("courses", []) if isinstance(item, dict)]
        offerings = [item for item in snapshot.get("offerings", []) if isinstance(item, dict)]
        departments = sorted({
            str(item.get("code") or "").split()[0]
            for item in courses
            if str(item.get("code") or "").split()
        })
        campuses = sorted({str(item.get("campus")) for item in offerings if item.get("campus")})
        semesters = sorted({str(item.get("semester_id")) for item in offerings if item.get("semester_id")})
        return {
            "course_count": len(courses),
            "offering_count": len(offerings),
            "departments": departments,
            "campuses": campuses,
            "semesters": semesters,
            "available_fields": ["course code", "title", "topics", "handout evidence", "offerings"],
        }

    def _tool_call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], name: str) -> dict[str, Any]:
        for _ in range(self.max_attempts):
            response = self.client.complete(messages, tools)
            message = response["choices"][0]["message"]
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != name:
                    continue
                arguments = json.loads(function.get("arguments") or "{}")
                if isinstance(arguments, dict):
                    return arguments
            messages.append({"role": "assistant", "content": str(message.get("content") or "")})
            messages.append({"role": "user", "content": f"Call {name} now."})
        raise RuntimeError(f"Groq did not call {name}.")

    def run(self, profile: StudentProfile, query: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        profile_context = self._profile(profile)
        dataset_context = self._dataset(snapshot)
        plan_messages = [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "student_profile": profile_context,
                "dataset": dataset_context,
                "request": query,
            }, ensure_ascii=False)},
        ]
        plan = self._tool_call(plan_messages, PLAN_TOOLS, "plan_dataset_search")
        interpreted = str(plan.get("interpreted_request") or "").strip()
        search_terms = [str(item).strip() for item in plan.get("search_terms") or [] if str(item).strip()]
        trace = [{"tool": "interpret_profile_and_request", "status": "ok"}]
        if bool(plan.get("needs_clarification")) or not search_terms:
            question = str(plan.get("clarification_question") or "What subject or type of course do you want?").strip()
            return {
                "recommendations": [],
                "no_result_reason": question,
                "agent": {
                    "mode": "groq_profile_dataset_agent",
                    "model": self.client.model,
                    "summary": interpreted or question,
                    "trace": trace,
                },
            }
        semantic_query = " ".join(dict.fromkeys(search_terms))
        retrieved = discover_courses(profile, semantic_query, snapshot, corpus_path=self.corpus_path, limit=12)
        candidates = self._candidates(retrieved, snapshot)
        trace.append({"tool": "retrieve_dataset_courses", "status": "ok"})
        if not candidates:
            retrieved["no_result_reason"] = f"No supplied course material matched: {interpreted or semantic_query}."
            retrieved["agent"] = {
                "mode": "groq_profile_dataset_agent",
                "model": self.client.model,
                "summary": interpreted or semantic_query,
                "trace": trace,
            }
            return retrieved
        messages = [
            {"role": "system", "content": RERANK_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "student_profile": profile_context,
                "dataset": dataset_context,
                "original_request": query,
                "interpreted_request": interpreted,
                "candidates": candidates,
            }, ensure_ascii=False)},
        ]
        selection = self._tool_call(messages, RERANK_TOOLS, "rerank_staged_courses")
        selected = [item for item in selection.get("selected") or [] if isinstance(item, dict)]
        cards = {str(item.get("course_id")): item for item in retrieved["recommendations"]}
        recommendations = []
        seen: set[str] = set()
        for item in selected:
            course_id = str(item.get("course_id") or "")
            reason = str(item.get("reason") or "").strip()
            if course_id not in cards or course_id in seen or not reason:
                continue
            card = dict(cards[course_id])
            card["matched_topics"] = [reason]
            card["badge"] = "Course match"
            card["source_note"] = "Matches your preferences based on the course handout."
            recommendations.append(card)
            seen.add(course_id)
        retrieved["recommendations"] = recommendations
        retrieved["no_result_reason"] = None if recommendations else "Groq found no strong semantic match among the handout-grounded candidates."
        trace.append({"tool": "select_profile_dataset_matches", "status": "ok"})
        retrieved["agent"] = {
            "mode": "groq_profile_dataset_agent",
            "model": self.client.model,
            "summary": interpreted,
            "trace": trace,
        }
        return retrieved
