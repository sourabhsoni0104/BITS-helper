from __future__ import annotations

import json
import unittest
from pathlib import Path

from recommender.ingestion.snapshots import load_snapshot
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.services.academic_agent import AcademicAgent


ROOT = Path(__file__).resolve().parents[2]


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
    }


class FakeToolClient:
    model = "test-model"

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class AcademicAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_snapshot(ROOT / "data/synthetic/demo_snapshot.json")
        cls.profile = StudentProfile(
            "student",
            "SYNTHETIC",
            2025,
            ("BSC-SYNTH",),
            3,
            "SYN-2026-T1",
            attempts=(CourseAttempt("SYN-100", AttemptStatus.COMPLETED),),
        )

    def test_agent_uses_tools_then_returns_only_validated_shortlist(self) -> None:
        client = FakeToolClient([
            tool_call("call-1", "build_recommendation_plan", {
                "requested_category": "DEL",
                "topic_preferences": ["artificial intelligence"],
                "hard_constraints": [{"field": "midsem_present", "value": False}],
                "soft_preferences": [],
                "search_query": "artificial intelligence",
            }),
            {"choices": [{"message": {"role": "assistant", "content": "SYN 210 is eligible and matches the verified no-midsem preference."}}]},
        ])
        result = AcademicAgent(client, ROOT / "missing-corpus.sqlite").run(
            self.profile,
            "Suggest an AI-related DEL with no midsem.",
            self.snapshot,
        )
        self.assertEqual([item["course_id"] for item in result["recommendations"]], ["SYN-210"])
        self.assertEqual(result["final_validation_status"], "pass")
        self.assertEqual(result["agent"]["mode"], "groq_planner_with_deterministic_tools")
        planner_input = json.loads(client.calls[0][1]["content"])
        self.assertEqual(planner_input["student_profile"]["programme_ids"], ["BSC-SYNTH"])
        self.assertEqual(planner_input["dataset"]["course_count"], len(self.snapshot.courses))
        self.assertEqual(
            [item["tool"] for item in result["agent"]["trace"]],
            [
                "build_recommendation_plan",
                "analyze_requirements",
                "find_eligible_courses",
                "search_handouts",
                "validate_recommendations",
                "explain_validated_results",
            ],
        )

    def test_agent_refuses_to_finish_without_mandatory_validation(self) -> None:
        client = FakeToolClient([
            {"choices": [{"message": {"role": "assistant", "content": "Take SYN 310."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Take SYN 310."}}]},
        ])
        with self.assertRaisesRegex(RuntimeError, "structured recommendation plan"):
            AcademicAgent(client, ROOT / "missing.sqlite", max_iterations=2).run(
                self.profile,
                "Suggest a DEL.",
                self.snapshot,
            )
        self.assertIn("Call build_recommendation_plan", client.calls[1][-1]["content"])

    def test_missing_prerequisite_cannot_pass_validator(self) -> None:
        client = FakeToolClient([
            tool_call("call-1", "build_recommendation_plan", {
                "requested_category": "DEL",
                "topic_preferences": ["artificial intelligence"],
                "hard_constraints": [],
                "soft_preferences": [],
                "search_query": "artificial intelligence",
            }),
        ])
        no_history = StudentProfile("student", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1")
        result = AcademicAgent(client, ROOT / "missing.sqlite").run(no_history, "Suggest an AI DEL.", self.snapshot)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["final_validation_status"], "pass")


if __name__ == "__main__":
    unittest.main()
