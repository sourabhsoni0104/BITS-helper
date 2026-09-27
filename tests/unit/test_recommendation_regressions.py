from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from recommender.ingestion.snapshots import load_snapshot
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.query import parse_query
from recommender.services.recommendation import _final_validate, _topic_score, recommend


ROOT = Path(__file__).resolve().parents[2]


def profile(*attempts: CourseAttempt) -> StudentProfile:
    return StudentProfile("test", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1", attempts=attempts)


class RecommendationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_snapshot(ROOT / "data/synthetic/demo_snapshot.json")

    def test_completed_and_in_progress_courses_are_excluded_across_repeated_offerings(self) -> None:
        original = next(item for item in self.snapshot.offerings if item.course_id == "SYN-210")
        duplicate = replace(original, offering_id="SYN-210-T1-REPEAT")
        snapshot = replace(self.snapshot, offerings=self.snapshot.offerings + (duplicate,))
        for status in (AttemptStatus.COMPLETED, AttemptStatus.IN_PROGRESS):
            result = recommend(profile(CourseAttempt("SYN-210", status), CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", snapshot)
            self.assertNotIn("SYN-210", {item["course_id"] for item in result["recommendations"]})

    def test_requirement_contribution_requires_course_pool_membership(self) -> None:
        result = recommend(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", self.snapshot)
        item = next(item for item in result["recommendations"] if item["course_id"] == "SYN-210")
        self.assertEqual(item["requirement_contribution"]["requirement_id"], "SYN-DEL")

    def test_requirement_contribution_is_omitted_when_requirement_satisfied(self) -> None:
        completed = (CourseAttempt("SYN-100", AttemptStatus.COMPLETED), CourseAttempt("SYN-310", AttemptStatus.COMPLETED))
        result = recommend(profile(*completed), "Suggest DELs related to AI.", self.snapshot)
        
        item = next(item for item in result["recommendations"] if item["course_id"] == "SYN-210")
        self.assertIsNone(item["requirement_contribution"])

    def test_requirement_contribution_is_omitted_outside_requirement_pool(self) -> None:
        context = self.snapshot.policy_contexts[0]
        altered = replace(context, requirements=tuple(
            replace(req, course_pool=("SYN-310",)) if req.requirement_id == "SYN-DEL" else req
            for req in context.requirements
        ))
        snapshot = replace(self.snapshot, policy_contexts=(altered,))
        result = recommend(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", snapshot)
        item = next(item for item in result["recommendations"] if item["course_id"] == "SYN-210")
        self.assertIsNone(item["requirement_contribution"])

    def test_zero_remaining_requirement_only_contributes_its_outstanding_mandatory_course(self) -> None:
        context = self.snapshot.policy_contexts[0]
        altered = replace(context, requirements=tuple(
            replace(req, required_value=0, mandatory_course_ids=("SYN-210",)) if req.requirement_id == "SYN-DEL" else req
            for req in context.requirements
        ))
        snapshot = replace(self.snapshot, policy_contexts=(altered,))
        result = recommend(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", snapshot)
        item = next(item for item in result["recommendations"] if item["course_id"] == "SYN-210")
        self.assertEqual(item["requirement_contribution"]["requirement_id"], "SYN-DEL")

    def test_topic_matching_respects_word_boundaries(self) -> None:
        self.assertEqual(_topic_score(("retail",), ("ai",)), 0)
        self.assertEqual(_topic_score(("artificial intelligence",), ("AI",)), 1)

    def test_equal_course_rankings_use_offering_id_tie_break(self) -> None:
        original = next(item for item in self.snapshot.offerings if item.course_id == "SYN-210")
        repeated = replace(original, offering_id="SYN-210-T1-ALT")
        snapshot = replace(self.snapshot, offerings=self.snapshot.offerings + (repeated,))
        result = recommend(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", snapshot)
        ids = [item["offering_id"] for item in result["recommendations"]]
        self.assertEqual(ids, sorted(ids))

    def test_final_validation_rechecks_campus_semester_and_exclusions(self) -> None:
        result = recommend(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), "Suggest DELs related to AI.", self.snapshot)
        item = result["recommendations"][0]
        for changed in (
            replace(self.snapshot, offerings=tuple(replace(o, campus="OTHER") if o.offering_id == item["offering_id"] else o for o in self.snapshot.offerings)),
            replace(self.snapshot, offerings=tuple(replace(o, semester_id="OTHER") if o.offering_id == item["offering_id"] else o for o in self.snapshot.offerings)),
        ):
            with self.assertRaises(RuntimeError):
                _final_validate([item], profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED)), parse_query("Suggest DELs related to AI."), changed)
        with self.assertRaises(RuntimeError):
            _final_validate([item], profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED), CourseAttempt("SYN-210", AttemptStatus.COMPLETED)), parse_query("Suggest DELs related to AI."), self.snapshot)


if __name__ == "__main__":
    unittest.main()
