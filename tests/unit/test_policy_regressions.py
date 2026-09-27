import unittest
from dataclasses import replace
from pathlib import Path

from recommender.ingestion.snapshots import load_snapshot
from recommender.models import (
    AttemptStatus, CategoryMembership, Course, CourseAttempt, DecisionStatus,
    Metric, PolicyContext, Requirement, StudentProfile, VerificationStatus,
)
from recommender.policies.engine import (
    PolicyResolutionError, analyze_requirements, evaluate_eligibility,
    evaluate_expression, resolve_policy_context,
)


ROOT = Path(__file__).resolve().parents[2]


def profile(*completed, programmes=("BSC-SYNTH",)):
    return StudentProfile(
        "test", "SYNTHETIC", 2025, programmes, 3, "SYN-2026-T1",
        tuple(CourseAttempt(course, AttemptStatus.COMPLETED) for course in completed),
    )


class PolicyRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_snapshot(ROOT / "data/synthetic/demo_snapshot.json")

    def test_combined_programmes_do_not_select_single_programme_context(self):
        with self.assertRaisesRegex(PolicyResolutionError, "combined-programme rules are unresolved"):
            resolve_policy_context(profile(programmes=("BSC-SYNTH", "MINOR-SYNTH")), self.snapshot)

    def test_mandatory_course_allocation_is_exclusive_and_outstanding(self):
        reqs = (
            Requirement("A", "DEL", Metric.COURSES, 1, ("SYN-100",), ("SYN-100",), ("EV-RULES",)),
            Requirement("B", "MINOR", Metric.COURSES, 1, ("SYN-100",), ("SYN-100",), ("EV-RULES",)),
        )
        context = replace(self.snapshot.policy_contexts[0], requirements=reqs)
        results = analyze_requirements(profile("SYN-100"), context, self.snapshot)
        self.assertEqual(sum(result.credited_amount for result in results), 1)
        self.assertEqual(sum(result.status == "verified" for result in results), 1)
        incomplete = next(result for result in results if result.status == "incomplete")
        self.assertEqual(incomplete.outstanding_mandatory_courses, ("SYN-100",))

    def test_unknown_historical_course_is_ignored_by_count_only_rules(self):
        requirement = Requirement("A", "DEL", Metric.COURSES, 1, ("SYN-100",), (), ("EV-RULES",))
        context = replace(self.snapshot.policy_contexts[0], requirements=(requirement,))
        result = analyze_requirements(profile("OLD-UNKNOWN"), context, self.snapshot)[0]
        self.assertEqual(result.credited_amount, 0)
        self.assertEqual(result.credited_course_ids, ())

    def test_unit_requirement_with_missing_course_credits_is_unresolved(self):
        requirement = Requirement("A", "DEL", Metric.UNITS, 3, ("OLD-UNKNOWN",), (), ("EV-RULES",))
        context = replace(self.snapshot.policy_contexts[0], requirements=(requirement,))
        with self.assertRaisesRegex(PolicyResolutionError, "Units for completed course OLD-UNKNOWN are unavailable"):
            analyze_requirements(profile("OLD-UNKNOWN"), context, self.snapshot)

    def test_allocation_search_compresses_repeated_equivalent_states(self):
        ids = tuple(f"C{i}" for i in range(17))
        courses = dict(self.snapshot.courses)
        courses.update({course_id: Course(course_id, course_id, course_id, 1, (), ()) for course_id in ids})
        snapshot = replace(self.snapshot, courses=courses)
        reqs = (
            Requirement("A", "DEL", Metric.COURSES, 1, ids, (), ("EV-RULES",)),
            Requirement("B", "MINOR", Metric.COURSES, 1, ids, (), ("EV-RULES",)),
        )
        context = replace(snapshot.policy_contexts[0], requirements=reqs)
        results = analyze_requirements(profile(*ids), context, snapshot)
        self.assertEqual(sum(result.status == "verified" for result in results), 2)

    def test_category_pass_uses_evidence_from_applicable_membership(self):
        original = self.snapshot.offerings[0]
        categories = (
            CategoryMembership("DEL", ("OTHER",), VerificationStatus.VERIFIED, ("WRONG",)),
            CategoryMembership("DEL", ("BSC-SYNTH",), VerificationStatus.VERIFIED, ("RIGHT",)),
        )
        result = evaluate_eligibility(profile(), replace(original, categories=categories), "DEL")
        category_check = next(check for check in result.checks if check.check_id == "category")
        self.assertEqual(category_check.status, DecisionStatus.PASS)
        self.assertEqual(category_check.evidence_ids, ("RIGHT",))

    def test_malformed_prerequisites_resolve_to_unknown(self):
        malformed = (
            [], {"op": "all_of", "conditions": [None]},
            {"op": "programme_in", "programme_ids": "BSC-SYNTH"},
            {"op": ["completed"]}, {"op": {"name": "completed"}},
            {"op": "minimum_semester", "value": True},
        )
        for expression in malformed:
            with self.subTest(expression=expression):
                self.assertEqual(evaluate_expression(expression, profile()).status, DecisionStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
