from __future__ import annotations

import re
from dataclasses import dataclass

from recommender.models import AttemptStatus, DatasetSnapshot, Offering, StudentProfile


@dataclass(frozen=True)
class ScheduleAssessment:
    status: str
    conflicts: tuple[dict[str, str], ...] = ()
    reason: str | None = None


_DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
         "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
         "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}


def _minutes(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None
    hour, minute = map(int, match.groups())
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _day(value: str) -> int | None:
    normalized = value.strip().casefold()
    return _DAYS.get(normalized)


def assess_schedule(profile: StudentProfile, candidate: Offering, snapshot: DatasetSnapshot) -> ScheduleAssessment:
    if not candidate.schedule:
        return ScheduleAssessment("unknown", reason="Candidate meeting schedule was not supplied.")
    candidate_rows = []
    for meeting in candidate.schedule:
        day, start, end = _day(meeting.day), _minutes(meeting.start), _minutes(meeting.end)
        if day is None or start is None or end is None or start >= end:
            return ScheduleAssessment("unknown", reason="Candidate schedule contains an unrecognized day or time.")
        candidate_rows.append((day, start, end, meeting.component or "meeting"))
    active_attempts = [
        attempt for attempt in profile.attempts
        if attempt.status == AttemptStatus.IN_PROGRESS
        and (attempt.term_id is None or attempt.term_id == candidate.semester_id)
    ]
    if any(attempt.term_id is None for attempt in active_attempts):
        return ScheduleAssessment(
            "unknown",
            reason="A current course has no term ID, so its schedule applicability cannot be resolved.",
        )
    active_courses = {attempt.course_id for attempt in active_attempts}
    if not active_courses:
        return ScheduleAssessment("clear")
    offerings = [
        item for item in snapshot.offerings
        if item.course_id in active_courses
        and item.campus.casefold() == profile.campus.casefold()
        and item.semester_id == candidate.semester_id
    ]
    supplied_courses = {item.course_id for item in offerings}
    missing = sorted(active_courses - supplied_courses)
    if missing:
        return ScheduleAssessment("unknown", reason=f"No offering schedule was supplied for current course {missing[0]}.")
    ambiguous = sorted(
        course_id for course_id in active_courses
        if sum(item.course_id == course_id for item in offerings) != 1
    )
    if ambiguous:
        return ScheduleAssessment(
            "unknown",
            reason=f"Multiple offering sections were supplied for current course {ambiguous[0]}, but the selected section is unknown.",
        )
    conflicts: list[dict[str, str]] = []
    for current in offerings:
        if not current.schedule:
            return ScheduleAssessment("unknown", reason=f"Schedule for current course {current.course_id} was not supplied.")
        for meeting in current.schedule:
            day, start, end = _day(meeting.day), _minutes(meeting.start), _minutes(meeting.end)
            if day is None or start is None or end is None or start >= end:
                return ScheduleAssessment("unknown", reason=f"Schedule for current course {current.course_id} contains an unrecognized day or time.")
            for cday, cstart, cend, ccomponent in candidate_rows:
                if day == cday and max(start, cstart) < min(end, cend):
                    conflicts.append({"course_id": current.course_id, "candidate_component": ccomponent,
                                      "current_component": meeting.component or "meeting", "day": meeting.day})
    return ScheduleAssessment("conflict" if conflicts else "clear", tuple(conflicts))


def assess_bundle(offerings: tuple[Offering, ...]) -> ScheduleAssessment:
    """Check pairwise overlap in an explicitly selected bundle of offerings."""
    rows: list[tuple[str, str, int, int, int]] = []
    for offering in offerings:
        if not offering.schedule:
            return ScheduleAssessment("unknown", reason=f"Schedule for {offering.course_id} was not supplied.")
        for meeting in offering.schedule:
            day, start, end = _day(meeting.day), _minutes(meeting.start), _minutes(meeting.end)
            if day is None or start is None or end is None or start >= end:
                return ScheduleAssessment("unknown", reason=f"Schedule for {offering.course_id} contains an unrecognized day or time.")
            rows.append((offering.course_id, meeting.component or "meeting", day, start, end))
    conflicts: list[dict[str, str]] = []
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if left[0] != right[0] and left[2] == right[2] and max(left[3], right[3]) < min(left[4], right[4]):
                conflicts.append({"course_id": left[0], "other_course_id": right[0],
                                  "component": left[1], "other_component": right[1],
                                  "day": str(left[2])})
    return ScheduleAssessment("conflict" if conflicts else "clear", tuple(conflicts))
