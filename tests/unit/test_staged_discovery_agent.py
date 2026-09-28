from __future__ import annotations

import json
import unittest
from pathlib import Path

from recommender.models import StudentProfile
from recommender.services.staged_discovery_agent import StagedDiscoveryAgent


class FakeClient:
    model = "test-model"

    def __init__(self, selected: list[dict], plan: dict | None = None) -> None:
        self.selected = selected
        self.plan = plan or {
            "interpreted_request": "Courses about artificial intelligence",
            "search_terms": ["artificial intelligence", "machine learning", "deep learning"],
            "needs_clarification": False,
            "clarification_question": "",
        }
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        name = tools[0]["function"]["name"]
        arguments = self.plan if name == "plan_dataset_search" else {"selected": self.selected}
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }],
                },
            }],
        }


class StagedDiscoveryAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "courses": [
                {
                    "course_id": "AI-1",
                    "code": "CS F425",
                    "title": "Deep Learning",
                    "topics": ["neural networks", "machine learning"],
                    "evidence_ids": ["EV-AI"],
                },
                {
                    "course_id": "ECON-1",
                    "code": "ECON F211",
                    "title": "Principles of Economics",
                    "topics": ["economics"],
                    "evidence_ids": ["EV-ECON"],
                },
            ],
            "evidence": [
                {"evidence_id": "EV-AI", "document_id": "DOC-AI", "excerpt": "Deep learning and neural networks."},
                {"evidence_id": "EV-ECON", "document_id": "DOC-ECON", "excerpt": "Generic AI use policy."},
            ],
        }
        self.profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")

    def test_groq_reranks_only_retrieved_course_ids(self) -> None:
        client = FakeClient([
            {"course_id": "MADE-UP", "reason": "Not allowed"},
            {"course_id": "AI-1", "reason": "Directly covers deep learning and neural networks."},
        ])
        result = StagedDiscoveryAgent(client, Path("missing.sqlite")).run(
            self.profile,
            "Teach me something about AI",
            self.snapshot,
        )
        self.assertEqual([item["course_id"] for item in result["recommendations"]], ["AI-1"])
        self.assertEqual(result["recommendations"][0]["badge"], "Course match")
        self.assertEqual(result["agent"]["mode"], "groq_profile_dataset_agent")
        self.assertEqual(
            [item["tool"] for item in result["agent"]["trace"]],
            ["interpret_profile_and_request", "retrieve_dataset_courses", "select_profile_dataset_matches"],
        )
        plan_input = json.loads(client.messages[0][1]["content"])
        self.assertEqual(plan_input["student_profile"]["programme_ids"], ["BE-CS"])
        self.assertEqual(plan_input["dataset"]["course_count"], 2)
        supplied = json.loads(client.messages[1][1]["content"])["candidates"]
        self.assertEqual([item["course_id"] for item in supplied], ["AI-1"])

    def test_ambiguous_request_returns_ai_clarification_without_raw_matching(self) -> None:
        client = FakeClient([], {
            "interpreted_request": "The student may mean a course sequence involving DRM.",
            "search_terms": [],
            "needs_clarification": True,
            "clarification_question": "What does DRM refer to in your course plan?",
        })
        result = StagedDiscoveryAgent(client, Path("missing.sqlite")).run(
            self.profile,
            "something after DRM",
            self.snapshot,
        )
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["no_result_reason"], "What does DRM refer to in your course plan?")
        self.assertEqual(len(client.messages), 1)


if __name__ == "__main__":
    unittest.main()
