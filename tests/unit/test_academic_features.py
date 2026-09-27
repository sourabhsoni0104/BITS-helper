from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from recommender.ingestion.snapshots import load_snapshot
from recommender.models import AttemptStatus, CourseAttempt, Evidence, EquivalenceGroup, Meeting, Metric, Requirement, StudentProfile, VerificationStatus
from recommender.policies import engine
from recommender.policies.engine import PolicyResolutionError, analyze_requirements
from recommender.services.scheduling import assess_bundle, assess_schedule
from recommender.storage.profiles import ProfileRepository, ProfileValidationError, validate_profile


def profile(attempts: tuple[CourseAttempt, ...]) -> StudentProfile:
    return StudentProfile("p", "north", 2024, ("cs",), 2, "2026-fall", attempts=attempts)


class AcademicFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_snapshot(Path(__file__).resolve().parents[2] / "data/synthetic/demo_snapshot.json")

    def test_attempt_identity_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProfileRepository(Path(directory) / "profiles.sqlite")
            saved = repository.save(profile((CourseAttempt("c1", AttemptStatus.COMPLETED, "A", 3, "a1", "2025-fall", 1),)))
            self.assertEqual(repository.get("p").attempts, saved.attempts)
            repository.close()

    def test_repeat_requires_distinct_identity_and_order_for_latest_rule(self) -> None:
        repeated_without_identity = profile((
            CourseAttempt("c1", AttemptStatus.COMPLETED),
            CourseAttempt("c1", AttemptStatus.FAILED),
        ))
        with self.assertRaises(ProfileValidationError):
            validate_profile(repeated_without_identity)

    def test_legacy_single_attempt_remains_valid(self) -> None:
        validate_profile(profile((CourseAttempt("c1", AttemptStatus.COMPLETED),)))

    def test_latest_repeat_units_use_latest_completed_attempt(self) -> None:
        req = Requirement("units", "DEL", Metric.UNITS, 4, ("SYN-100",), (), ("EV-RULES",))
        context = replace(self.snapshot.policy_contexts[0], requirements=(req,), repeat_policy="latest")
        student = StudentProfile("p", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1", (
            CourseAttempt("SYN-100", AttemptStatus.FAILED, units_awarded=2, attempt_id="old", term_id="t1", attempt_order=1),
            CourseAttempt("SYN-100", AttemptStatus.COMPLETED, units_awarded=4, attempt_id="new", term_id="t2", attempt_order=2),
        ))
        result = analyze_requirements(student, context, self.snapshot)[0]
        self.assertEqual(result.credited_amount, 4)

    def test_sharing_is_available_only_when_every_requirement_opts_in(self) -> None:
        requirements = (
            Requirement("a", "A", Metric.COURSES, 1, ("SYN-100",), (), ("EV-RULES",), True),
            Requirement("b", "B", Metric.COURSES, 1, ("SYN-100",), (), ("EV-RULES",), True),
        )
        context = replace(self.snapshot.policy_contexts[0], requirements=requirements)
        results = analyze_requirements(profile((CourseAttempt("SYN-100", AttemptStatus.COMPLETED),)), context, self.snapshot)
        self.assertEqual([item.credited_amount for item in results], [1, 1])

    def test_verified_equivalence_credits_alias_once_and_satisfies_prerequisite(self) -> None:
        evidence = dict(self.snapshot.evidence)
        evidence["EQ"] = Evidence("EQ", "DOC-RULES", 1, "Equivalence", "equivalent courses", VerificationStatus.VERIFIED)
        group = EquivalenceGroup("eq", ("SYN-100", "SYN-200"), ("EQ",), VerificationStatus.VERIFIED)
        snapshot = replace(self.snapshot, evidence=evidence, equivalence_groups=(group,))
        req = Requirement("one", "DEL", Metric.COURSES, 2, ("SYN-100", "SYN-200"), ("SYN-200",), ("EV-RULES",))
        context = replace(snapshot.policy_contexts[0], requirements=(req,))
        student = StudentProfile("p", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1", (
            CourseAttempt("SYN-100", AttemptStatus.COMPLETED), CourseAttempt("SYN-200", AttemptStatus.COMPLETED),
        ))
        result = analyze_requirements(student, context, snapshot)[0]
        self.assertEqual(result.credited_amount, 1)
        self.assertEqual(result.outstanding_mandatory_courses, ())

    def test_memoized_search_obeys_configured_state_bound(self) -> None:
        reqs = tuple(Requirement(str(i), "DEL", Metric.COURSES, 1, ("SYN-100",), (), ("EV-RULES",)) for i in range(3))
        context = replace(self.snapshot.policy_contexts[0], requirements=reqs)
        with patch.object(engine, "MAX_ALLOCATION_STATES", 1):
            with self.assertRaisesRegex(PolicyResolutionError, "memoized-state limit"):
                analyze_requirements(profile((CourseAttempt("SYN-100", AttemptStatus.COMPLETED),)), context, self.snapshot)

    def test_schedule_reports_overlap_and_missing_data_as_unknown(self) -> None:
        candidate = replace(self.snapshot.offerings[0], schedule=())
        self.assertEqual(assess_schedule(profile(()), candidate, self.snapshot).status, "unknown")
        candidate = replace(candidate, schedule=(Meeting("Monday", "10:00", "11:00", "lecture"),))
        current = replace(self.snapshot.offerings[1], schedule=(Meeting("Mon", "10:30", "11:30", "lab"),))
        snapshot = replace(self.snapshot, offerings=(candidate, current))
        student = StudentProfile("p", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1",
                                 (CourseAttempt(current.course_id, AttemptStatus.IN_PROGRESS, term_id=candidate.semester_id),))
        self.assertEqual(assess_schedule(student, candidate, snapshot).status, "conflict")
        exam = replace(current, schedule=(Meeting("Monday", "10:45", "11:45", "exam"),))
        self.assertEqual(assess_bundle((candidate, exam)).status, "conflict")

    def test_schedule_ignores_other_terms_and_rejects_ambiguous_sections(self) -> None:
        candidate = replace(self.snapshot.offerings[0], schedule=(Meeting("Monday", "10:00", "11:00"),))
        current = replace(self.snapshot.offerings[1], schedule=(Meeting("Mon", "10:30", "11:30"),))
        other_term = StudentProfile("p", "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1", (
            CourseAttempt(current.course_id, AttemptStatus.IN_PROGRESS, term_id="SYN-2025-T2"),
        ))
        self.assertEqual(assess_schedule(other_term, candidate, replace(self.snapshot, offerings=(candidate, current))).status, "clear")
        same_term = replace(other_term, attempts=(
            CourseAttempt(current.course_id, AttemptStatus.IN_PROGRESS, term_id=candidate.semester_id),
        ))
        section_two = replace(current, offering_id=current.offering_id + "-B")
        result = assess_schedule(same_term, candidate, replace(self.snapshot, offerings=(candidate, current, section_two)))
        self.assertEqual(result.status, "unknown")
        self.assertIn("Multiple offering sections", result.reason)


if __name__ == "__main__":
    unittest.main()
