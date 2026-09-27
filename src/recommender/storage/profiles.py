from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

from recommender.models import AttemptStatus, CourseAttempt, StudentProfile


class ProfileConflictError(ValueError):
    """Raised when a stale profile version would overwrite newer data."""


class ProfileValidationError(ValueError):
    pass


def validate_profile(profile: StudentProfile) -> None:
    if not isinstance(profile.profile_id, str) or not profile.profile_id.strip():
        raise ProfileValidationError("Profile ID is required.")
    if not isinstance(profile.campus, str) or not profile.campus.strip():
        raise ProfileValidationError("Campus is required.")
    if not isinstance(profile.programme_ids, (tuple, list)) or not profile.programme_ids or any(
        not isinstance(programme_id, str) or not programme_id.strip()
        for programme_id in profile.programme_ids
    ):
        raise ProfileValidationError("At least one degree/programme ID is required.")
    if len(set(profile.programme_ids)) != len(profile.programme_ids):
        raise ProfileValidationError("Programme IDs must be unique.")
    if not isinstance(profile.interests, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in profile.interests):
        raise ProfileValidationError("Interests must be a list of non-empty strings.")
    if profile.minor_id is not None and (not isinstance(profile.minor_id, str) or not profile.minor_id.strip()):
        raise ProfileValidationError("Minor ID must be a non-empty string when supplied.")
    if isinstance(profile.admission_year, bool) or not isinstance(profile.admission_year, int):
        raise ProfileValidationError("Admission year must be an integer.")
    if profile.admission_year < 1900 or profile.admission_year > 2200:
        raise ProfileValidationError("Admission year is outside the supported range.")
    if isinstance(profile.current_semester, bool) or not isinstance(profile.current_semester, int):
        raise ProfileValidationError("Current semester must be an integer.")
    if profile.current_semester < 1:
        raise ProfileValidationError("Current semester must be at least 1.")
    if not isinstance(profile.target_semester_id, str) or not profile.target_semester_id.strip():
        raise ProfileValidationError("Target semester is required.")
    seen: dict[str, list[CourseAttempt]] = {}
    attempt_ids: set[str] = set()
    if not isinstance(profile.attempts, (tuple, list)) or any(not isinstance(item, CourseAttempt) for item in profile.attempts):
        raise ProfileValidationError("Attempts must contain course-attempt records.")
    for attempt in profile.attempts:
        if not isinstance(attempt.course_id, str) or not attempt.course_id.strip():
            raise ProfileValidationError("Attempt course ID is required.")
        if not isinstance(attempt.status, AttemptStatus):
            raise ProfileValidationError(f"Course {attempt.course_id} has an invalid attempt status.")
        if attempt.grade is not None and (not isinstance(attempt.grade, str) or not attempt.grade.strip()):
            raise ProfileValidationError(f"Course {attempt.course_id} grade must be a non-empty string when supplied.")
        try:
            finite_units = attempt.units_awarded is None or math.isfinite(attempt.units_awarded)
        except (TypeError, OverflowError):
            finite_units = False
        if attempt.units_awarded is not None and (
            isinstance(attempt.units_awarded, bool)
            or not isinstance(attempt.units_awarded, (int, float))
            or not finite_units
            or attempt.units_awarded < 0
        ):
            raise ProfileValidationError(f"Course {attempt.course_id} units awarded must be a finite nonnegative number.")
        if attempt.attempt_id is not None and (not isinstance(attempt.attempt_id, str) or not attempt.attempt_id.strip()):
            raise ProfileValidationError(f"Course {attempt.course_id} attempt ID must be non-empty when supplied.")
        if attempt.term_id is not None and (not isinstance(attempt.term_id, str) or not attempt.term_id.strip()):
            raise ProfileValidationError(f"Course {attempt.course_id} term ID must be non-empty when supplied.")
        if attempt.attempt_order is not None and (type(attempt.attempt_order) is not int or attempt.attempt_order < 0):
            raise ProfileValidationError(f"Course {attempt.course_id} attempt order must be a non-negative integer when supplied.")
        if attempt.attempt_id is not None:
            if attempt.attempt_id in attempt_ids:
                raise ProfileValidationError(f"Attempt ID {attempt.attempt_id} is duplicated in the profile.")
            attempt_ids.add(attempt.attempt_id)
        previous = seen.setdefault(attempt.course_id, [])
        if previous and (attempt.attempt_id is None or attempt.term_id is None or any(old.attempt_id is None or old.term_id is None for old in previous)):
            raise ProfileValidationError(f"Repeated course {attempt.course_id} requires a unique attempt ID and term ID for every attempt.")
        if any(old.term_id == attempt.term_id for old in previous):
            raise ProfileValidationError(f"Course {attempt.course_id} has multiple records for term {attempt.term_id}.")
        previous.append(attempt)


class ProfileRepository:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database, check_same_thread=False)
        self.lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.database != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS student_profiles (
                profile_id TEXT PRIMARY KEY,
                campus TEXT NOT NULL,
                admission_year INTEGER NOT NULL,
                programme_ids_json TEXT NOT NULL,
                current_semester INTEGER NOT NULL,
                target_semester_id TEXT NOT NULL,
                minor_id TEXT,
                interests_json TEXT NOT NULL,
                attempts_json TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get(self, profile_id: str) -> StudentProfile | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM student_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
        if row is None:
            return None
        attempts = tuple(
            CourseAttempt(
                course_id=item["course_id"],
                status=AttemptStatus(item["status"]),
                grade=item.get("grade"),
                units_awarded=item.get("units_awarded"),
                attempt_id=item.get("attempt_id"),
                term_id=item.get("term_id"),
                attempt_order=item.get("attempt_order"),
            )
            for item in json.loads(row["attempts_json"])
        )
        return StudentProfile(
            profile_id=row["profile_id"],
            campus=row["campus"],
            admission_year=row["admission_year"],
            programme_ids=tuple(json.loads(row["programme_ids_json"])),
            current_semester=row["current_semester"],
            target_semester_id=row["target_semester_id"],
            attempts=attempts,
            minor_id=row["minor_id"],
            interests=tuple(json.loads(row["interests_json"])),
            profile_version=row["profile_version"],
        )

    def delete(self, profile_id: str) -> None:
        """Delete one exact profile, used when an ephemeral guest session ends."""
        if not isinstance(profile_id, str) or not profile_id:
            return
        with self.lock:
            self.connection.execute("DELETE FROM student_profiles WHERE profile_id = ?", (profile_id,))
            self.connection.commit()

    def save(self, profile: StudentProfile, expected_version: int | None = None) -> StudentProfile:
        validate_profile(profile)
        if expected_version is not None and (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ProfileValidationError("Expected version must be a nonnegative integer.")
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                current = self.get(profile.profile_id)
                if current is None:
                    if expected_version not in (None, 0):
                        raise ProfileConflictError("Profile no longer exists or was not created by this session.")
                    version = 1
                else:
                    if expected_version is not None and expected_version != current.profile_version:
                        raise ProfileConflictError(
                            f"Profile {profile.profile_id} changed from version {expected_version} to {current.profile_version}; reload before saving."
                        )
                    version = current.profile_version + 1
                saved = replace(profile, profile_version=version)
                attempts_json = json.dumps([
                    {
                        "course_id": attempt.course_id,
                        "status": attempt.status.value,
                        "grade": attempt.grade,
                        "units_awarded": attempt.units_awarded,
                        "attempt_id": attempt.attempt_id,
                        "term_id": attempt.term_id,
                        "attempt_order": attempt.attempt_order,
                    }
                    for attempt in saved.attempts
                ])
                self.connection.execute(
                    """
                    INSERT INTO student_profiles (
                        profile_id, campus, admission_year, programme_ids_json,
                        current_semester, target_semester_id, minor_id, interests_json,
                        attempts_json, profile_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        campus=excluded.campus,
                        admission_year=excluded.admission_year,
                        programme_ids_json=excluded.programme_ids_json,
                        current_semester=excluded.current_semester,
                        target_semester_id=excluded.target_semester_id,
                        minor_id=excluded.minor_id,
                        interests_json=excluded.interests_json,
                        attempts_json=excluded.attempts_json,
                        profile_version=excluded.profile_version,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        saved.profile_id,
                        saved.campus,
                        saved.admission_year,
                        json.dumps(saved.programme_ids),
                        saved.current_semester,
                        saved.target_semester_id,
                        saved.minor_id,
                        json.dumps(saved.interests),
                        attempts_json,
                        saved.profile_version,
                    ),
                )
                self.connection.commit()
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
        return saved
