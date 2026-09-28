from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recommender.ingestion.snapshots import SnapshotError, load_snapshot, validate_snapshot
from recommender.models import AttemptStatus, CourseAttempt, DecisionStatus, Metric, PolicyContext, Requirement, StudentProfile
from recommender.policies.engine import analyze_requirements, evaluate_expression, resolve_policy_context
from recommender.services.recommendation import recommend


ROOT = Path(__file__).resolve().parents[2]


def profile(*completed: str) -> StudentProfile:
    return StudentProfile(
        profile_id="test-profile",
        campus="SYNTHETIC",
        admission_year=2025,
        programme_ids=("BSC-SYNTH",),
        current_semester=3,
        target_semester_id="SYN-2026-T1",
        attempts=tuple(CourseAttempt(course_id, AttemptStatus.COMPLETED) for course_id in completed),
    )


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_snapshot(ROOT / "data/synthetic/demo_snapshot.json")

    def test_synthetic_snapshot_validates(self) -> None:
        self.assertFalse([issue for issue in validate_snapshot(self.snapshot) if issue.severity == "error"])

    def _load_modified_snapshot(self, modify) -> None:
        raw = json.loads((ROOT / "data/synthetic/demo_snapshot.json").read_text(encoding="utf-8"))
        modify(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            load_snapshot(path)

    def test_evidence_must_reference_a_registered_document(self) -> None:
        with self.assertRaisesRegex(SnapshotError, "unknown document"):
            self._load_modified_snapshot(lambda raw: raw["evidence"][0].update(document_id="MISSING-DOCUMENT"))

    def test_verified_fact_requires_verified_evidence(self) -> None:
        def remove_evidence(raw) -> None:
            raw["offerings"][0]["handout_facts"]["midsem_present"]["evidence_ids"] = []

        with self.assertRaisesRegex(SnapshotError, "verified but has no evidence"):
            self._load_modified_snapshot(remove_evidence)

    def test_policy_context_is_scoped(self) -> None:
        context = resolve_policy_context(profile(), self.snapshot)
        self.assertEqual(context.context_id, "SYNTHETIC-BSC-2024-2026")
        wrong = StudentProfile("x", "OTHER", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1")
        with self.assertRaises(ValueError):
            resolve_policy_context(wrong, self.snapshot)

    def test_in_progress_course_is_not_completed_prerequisite(self) -> None:
        value = profile()
        value = StudentProfile(**{**value.__dict__, "attempts": (CourseAttempt("SYN-100", AttemptStatus.IN_PROGRESS),)})
        result = evaluate_expression({"op": "completed", "course_id": "SYN-100"}, value)
        self.assertEqual(result.status, DecisionStatus.FAIL)

    def test_all_of_fail_beats_unknown(self) -> None:
        expression = {"op": "all_of", "conditions": [{"op": "completed", "course_id": "MISSING"}, {"op": "mystery"}]}
        self.assertEqual(evaluate_expression(expression, profile()).status, DecisionStatus.FAIL)

    def test_any_of_pass_beats_unknown(self) -> None:
        expression = {"op": "any_of", "conditions": [{"op": "completed", "course_id": "SYN-100"}, {"op": "mystery"}]}
        self.assertEqual(evaluate_expression(expression, profile("SYN-100")).status, DecisionStatus.PASS)

    def test_any_of_unknown_when_no_pass_and_one_unknown(self) -> None:
        expression = {"op": "any_of", "conditions": [{"op": "completed", "course_id": "MISSING"}, {"op": "mystery"}]}
        self.assertEqual(evaluate_expression(expression, profile()).status, DecisionStatus.UNKNOWN)

    def test_ai_del_requires_foundation(self) -> None:
        blocked = recommend(profile(), "Suggest an AI-related DEL with no midsem.", self.snapshot)
        self.assertEqual(blocked["recommendations"], [])
        allowed = recommend(profile("SYN-100"), "Suggest an AI-related DEL with no midsem.", self.snapshot)
        self.assertEqual([item["course_code"] for item in allowed["recommendations"]], ["SYN 210"])
        self.assertEqual(allowed["final_validation_status"], "pass")
        self.assertEqual(allowed["recommendations"][0]["requirement_contribution"]["requirement_id"], "SYN-DEL")
        self.assertIn("EV-RULES", allowed["recommendations"][0]["evidence_references"])

    def test_missing_fact_does_not_pass_hard_constraint(self) -> None:
        result = recommend(profile("SYN-100"), "I need a HUEL with no midsem.", self.snapshot)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["unverified_alternatives"][0]["course_code"], "HUM 150")
        self.assertIn("do not confirm", result["no_result_reason"])

    def test_no_attendance_query_uses_verified_false(self) -> None:
        result = recommend(profile("SYN-100"), "I want an OPEL with no attendance requirement.", self.snapshot)
        self.assertEqual([item["course_code"] for item in result["recommendations"]], ["OPEN 120"])

    def test_subjective_leniency_requires_clarification(self) -> None:
        result = recommend(profile("SYN-100"), "Suggest courses with no midsem and a lenient makeup policy.", self.snapshot)
        self.assertTrue(result["clarification_questions"])
        self.assertEqual(result["recommendations"], [])

    def test_requirement_progress_changes_with_profile(self) -> None:
        before = recommend(profile(), "Suggest DELs related to AI.", self.snapshot)
        after = recommend(profile("SYN-100"), "Suggest DELs related to AI.", self.snapshot)
        before_cdc = next(item for item in before["requirement_summary"] if item["category"] == "CDC")
        after_cdc = next(item for item in after["requirement_summary"] if item["category"] == "CDC")
        self.assertEqual(before_cdc["remaining_amount"], 1)
        self.assertEqual(after_cdc["remaining_amount"], 0)

    def test_overlapping_courses_are_allocated_without_greedy_false_failure(self) -> None:
        context = PolicyContext(
            context_id="overlap-test",
            campus="SYNTHETIC",
            admission_year_from=2024,
            admission_year_to=2026,
            programme_ids=("BSC-SYNTH",),
            requirements=(
                Requirement("FLEXIBLE", "DEL", Metric.COURSES, 1, ("SYN-100", "SYN-200"), (), ("EV-RULES",)),
                Requirement("CONSTRAINED", "MINOR", Metric.COURSES, 1, ("SYN-100",), (), ("EV-RULES",)),
            ),
            evidence_ids=("EV-RULES",),
        )
        results = analyze_requirements(profile("SYN-100", "SYN-200"), context, self.snapshot)
        allocations = {item.requirement_id: item.credited_course_ids for item in results}
        self.assertEqual(allocations["FLEXIBLE"], ("SYN-200",))
        self.assertEqual(allocations["CONSTRAINED"], ("SYN-100",))
        self.assertTrue(all(item.remaining_amount == 0 for item in results))


if __name__ == "__main__":
    unittest.main()
