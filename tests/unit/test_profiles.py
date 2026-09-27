from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.storage.profiles import ProfileConflictError, ProfileRepository, ProfileValidationError


def sample_profile() -> StudentProfile:
    return StudentProfile(
        profile_id="student-1",
        campus="SYNTHETIC",
        admission_year=2025,
        programme_ids=("BSC-SYNTH", "MSC-SYNTH"),
        current_semester=3,
        target_semester_id="SYN-2026-T1",
        attempts=(
            CourseAttempt("SYN-100", AttemptStatus.COMPLETED, grade="A"),
            CourseAttempt("SYN-200", AttemptStatus.IN_PROGRESS),
        ),
        minor_id="MINOR-SYNTH",
        interests=("AI", "design"),
    )


class ProfileRepositoryTests(unittest.TestCase):
    def test_round_trip_and_version_increment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProfileRepository(Path(directory) / "profiles.sqlite")
            created = repository.save(sample_profile(), expected_version=0)
            self.assertEqual(created.profile_version, 1)
            loaded = repository.get("student-1")
            self.assertEqual(loaded, created)
            updated = repository.save(created, expected_version=1)
            self.assertEqual(updated.profile_version, 2)
            repository.close()

    def test_exact_profile_can_be_deleted_for_ephemeral_sessions(self) -> None:
        repository = ProfileRepository(":memory:")
        repository.save(sample_profile(), expected_version=0)
        repository.delete("student-1")
        self.assertIsNone(repository.get("student-1"))
        repository.close()

    def test_stale_update_is_rejected(self) -> None:
        repository = ProfileRepository(":memory:")
        created = repository.save(sample_profile(), expected_version=0)
        repository.save(created, expected_version=1)
        with self.assertRaises(ProfileConflictError):
            repository.save(created, expected_version=1)
        repository.close()

    def test_contradictory_attempts_are_rejected(self) -> None:
        repository = ProfileRepository(":memory:")
        value = sample_profile()
        invalid = StudentProfile(**{
            **value.__dict__,
            "attempts": (
                CourseAttempt("SYN-100", AttemptStatus.COMPLETED),
                CourseAttempt("SYN-100", AttemptStatus.IN_PROGRESS),
            ),
        })
        with self.assertRaises(ProfileValidationError):
            repository.save(invalid)
        repository.close()

    def test_two_repositories_serialize_versioned_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "profiles.sqlite"
            first = ProfileRepository(database)
            created = first.save(sample_profile(), expected_version=0)
            second = ProfileRepository(database)
            barrier = threading.Barrier(2)
            outcomes: list[object] = []

            def update(repository: ProfileRepository, campus: str) -> None:
                barrier.wait()
                try:
                    outcomes.append(repository.save(replace(created, campus=campus), expected_version=1))
                except Exception as error:  
                    outcomes.append(error)

            threads = [
                threading.Thread(target=update, args=(first, "CAMPUS-A")),
                threading.Thread(target=update, args=(second, "CAMPUS-B")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sum(isinstance(outcome, StudentProfile) for outcome in outcomes), 1)
            self.assertEqual(sum(isinstance(outcome, ProfileConflictError) for outcome in outcomes), 1)
            final = first.get("student-1")
            self.assertEqual(final.profile_version, 2)
            recovered = first.save(final, expected_version=2)
            self.assertEqual(recovered.profile_version, 3)
            first.close()
            second.close()

    def test_rejects_blank_ids_and_invalid_awarded_units(self) -> None:
        repository = ProfileRepository(":memory:")
        profile = sample_profile()
        invalid_profiles = (
            replace(profile, programme_ids="BSC-SYNTH"),
            replace(profile, programme_ids=("BSC-SYNTH", "  ")),
            replace(profile, interests="AI"),
            replace(profile, attempts=("SYN-100",)),
            replace(profile, attempts=(CourseAttempt("  ", AttemptStatus.COMPLETED),)),
            replace(profile, attempts=(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, units_awarded=float("nan")),)),
            replace(profile, attempts=(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, units_awarded=-0.5),)),
            replace(profile, attempts=(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, units_awarded=10 ** 1000),)),
        )
        for invalid in invalid_profiles:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProfileValidationError):
                    repository.save(invalid)
        repository.close()

    def test_rejects_invalid_versions_and_boolean_years(self) -> None:
        repository = ProfileRepository(":memory:")
        with self.assertRaises(ProfileValidationError):
            repository.save(sample_profile(), expected_version=True)
        with self.assertRaises(ProfileValidationError):
            repository.save(replace(sample_profile(), admission_year=True))
        repository.close()


if __name__ == "__main__":
    unittest.main()
