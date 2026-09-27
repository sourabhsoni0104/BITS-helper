from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class DecisionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    NOT_FOUND = "not_found"
    CONFLICTING = "conflicting"


class AttemptStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class Metric(StrEnum):
    COURSES = "courses"
    UNITS = "units"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    document_id: str
    page: int | None
    section: str | None
    excerpt: str
    verification_status: VerificationStatus


@dataclass(frozen=True)
class Course:
    course_id: str
    code: str
    title: str
    units: float | None
    topics: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Fact:
    value: Any
    verification_status: VerificationStatus
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategoryMembership:
    category: str
    programme_ids: tuple[str, ...]
    verification_status: VerificationStatus
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Offering:
    offering_id: str
    course_id: str
    campus: str
    semester_id: str
    availability: Fact
    categories: tuple[CategoryMembership, ...]
    prerequisite: dict[str, Any] | None
    handout_facts: dict[str, Fact]
    evidence_ids: tuple[str, ...]
    schedule: tuple[Meeting, ...] = ()


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    category: str
    metric: Metric
    required_value: float | None
    course_pool: tuple[str, ...]
    mandatory_course_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    allow_shared_credit: bool = False


@dataclass(frozen=True)
class PolicyContext:
    context_id: str
    campus: str
    admission_year_from: int
    admission_year_to: int
    programme_ids: tuple[str, ...]
    requirements: tuple[Requirement, ...]
    evidence_ids: tuple[str, ...]
    minor_id: str | None = None
    repeat_policy: str = "unresolved"


@dataclass(frozen=True)
class CourseAttempt:
    course_id: str
    status: AttemptStatus
    grade: str | None = None
    units_awarded: float | None = None
    attempt_id: str | None = None
    term_id: str | None = None
    attempt_order: int | None = None


@dataclass(frozen=True)
class Meeting:
    day: str
    start: str
    end: str
    component: str | None = None


@dataclass(frozen=True)
class EquivalenceGroup:
    group_id: str
    course_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    verification_status: VerificationStatus


@dataclass(frozen=True)
class StudentProfile:
    profile_id: str
    campus: str
    admission_year: int
    programme_ids: tuple[str, ...]
    current_semester: int
    target_semester_id: str
    attempts: tuple[CourseAttempt, ...] = ()
    minor_id: str | None = None
    interests: tuple[str, ...] = ()
    profile_version: int = 1


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_version: str
    synthetic: bool
    documents: tuple[dict[str, Any], ...]
    evidence: dict[str, Evidence]
    courses: dict[str, Course]
    offerings: tuple[Offering, ...]
    policy_contexts: tuple[PolicyContext, ...]
    equivalence_groups: tuple[EquivalenceGroup, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: DecisionStatus
    reason_code: str
    explanation: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EligibilityResult:
    course_id: str
    offering_id: str
    overall_status: DecisionStatus
    checks: tuple[CheckResult, ...]
    applicable_categories: tuple[str, ...]


@dataclass(frozen=True)
class RequirementResult:
    requirement_id: str
    category: str
    metric: Metric
    required_amount: float | None
    credited_amount: float
    remaining_amount: float | None
    outstanding_mandatory_courses: tuple[str, ...]
    credited_course_ids: tuple[str, ...]
    status: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Constraint:
    field: str
    operator: str
    value: Any


@dataclass(frozen=True)
class QueryIntent:
    requested_category: str | None
    topic_preferences: tuple[str, ...]
    hard_constraints: tuple[Constraint, ...]
    soft_preferences: tuple[Constraint, ...]
    ambiguities: tuple[str, ...]
    clarification_questions: tuple[str, ...]
    original_query: str


def to_dict(value: Any) -> Any:
    """Convert nested dataclasses/enums to JSON-safe primitives."""
    return asdict(value)
