from __future__ import annotations

from typing import Any
from functools import lru_cache

from recommender.models import (
    AttemptStatus,
    CheckResult,
    DatasetSnapshot,
    DecisionStatus,
    EligibilityResult,
    Metric,
    Offering,
    PolicyContext,
    RequirementResult,
    StudentProfile,
    VerificationStatus,
)

MAX_ALLOCATION_STATES = 100_000


class PolicyResolutionError(ValueError):
    pass


def canonical_course_id(snapshot: DatasetSnapshot, course_id: str) -> str:
    members = {course_id}
    changed = True
    while changed:
        changed = False
        for group in snapshot.equivalence_groups:
            supported = group.verification_status == VerificationStatus.VERIFIED and group.evidence_ids and any(
                eid in snapshot.evidence and snapshot.evidence[eid].verification_status == VerificationStatus.VERIFIED for eid in group.evidence_ids
            )
            if supported and members.intersection(group.course_ids) and not set(group.course_ids).issubset(members):
                members.update(group.course_ids)
                changed = True
    return min(members)


def resolve_policy_context(profile: StudentProfile, snapshot: DatasetSnapshot) -> PolicyContext:
    profile_programmes = set(profile.programme_ids)
    matches = [
        context
        for context in snapshot.policy_contexts
        if context.campus.casefold() == profile.campus.casefold()
        and context.admission_year_from <= profile.admission_year <= context.admission_year_to
        and set(context.programme_ids) == profile_programmes
        and context.minor_id is None
    ]
    if not matches:
        raise PolicyResolutionError("No supplied policy context exactly matches this campus, admission year, and programme combination; combined-programme rules are unresolved.")
    if len(matches) != 1:
        raise PolicyResolutionError("Multiple equally applicable policy contexts were found; applicability is unresolved.")
    return matches[0]


def resolve_minor_policy_context(profile: StudentProfile, snapshot: DatasetSnapshot) -> PolicyContext | None:
    if profile.minor_id is None:
        return None
    matches = [context for context in snapshot.policy_contexts
        if context.campus.casefold() == profile.campus.casefold()
        and context.admission_year_from <= profile.admission_year <= context.admission_year_to
        and context.minor_id == profile.minor_id]
    matches = [context for context in matches if not context.programme_ids or set(context.programme_ids).issubset(set(profile.programme_ids))]
    if len(matches) > 1:
        raise PolicyResolutionError("Multiple equally applicable minor policy contexts were found; applicability is unresolved.")
    return matches[0] if matches else None


def _completed_ids(profile: StudentProfile, context: PolicyContext | None = None) -> set[str]:
    by_course: dict[str, list[Any]] = {}
    for attempt in profile.attempts:
        by_course.setdefault(attempt.course_id, []).append(attempt)
    result: set[str] = set()
    for course_id, attempts in by_course.items():
        if len(attempts) == 1:
            completed = attempts[0].status == AttemptStatus.COMPLETED
        elif context is not None and context.repeat_policy == "latest" and all(item.attempt_order is not None for item in attempts):
            completed = max(attempts, key=lambda item: item.attempt_order).status == AttemptStatus.COMPLETED
        else:
            completed = all(attempt.status == AttemptStatus.COMPLETED for attempt in attempts)
        if completed:
            result.add(course_id)
    return result


def _course_unit_values(profile: StudentProfile, course_ids: set[str], snapshot: DatasetSnapshot, context: PolicyContext | None = None) -> dict[str, float]:
    attempts: dict[str, Any] = {}
    grouped: dict[str, list[Any]] = {}
    for item in profile.attempts:
        grouped.setdefault(item.course_id, []).append(item)
    for course_id, values in grouped.items():
        if len(values) == 1:
            attempts[course_id] = values[0]
        elif context is not None and context.repeat_policy == "latest" and all(item.attempt_order is not None for item in values):
            attempts[course_id] = max(values, key=lambda item: item.attempt_order)
    values: dict[str, float] = {}
    for course_id in course_ids:
        source_id = next((candidate for candidate in attempts if canonical_course_id(snapshot, candidate) == course_id), course_id)
        attempt = attempts.get(source_id)
        if attempt is None:
            raise PolicyResolutionError(f"Completed course {course_id} has no completed attempt record; unit requirement is unresolved.")
        if attempt.units_awarded is not None:
            values[course_id] = attempt.units_awarded
        elif source_id in snapshot.courses and snapshot.courses[source_id].units is not None:
            values[course_id] = snapshot.courses[source_id].units
        else:
            raise PolicyResolutionError(f"Units for completed course {course_id} are unavailable; unit requirement is unresolved.")
    return values


def _allocate_completed_courses(profile: StudentProfile, context: PolicyContext, snapshot: DatasetSnapshot) -> dict[str, tuple[str, ...]]:
    """Allocate each completed course at most once, maximizing satisfied requirements."""
    grouped: dict[str, list[Any]] = {}
    for attempt in profile.attempts:
        grouped.setdefault(attempt.course_id, []).append(attempt)
    for course_id, attempts in grouped.items():
        if len(attempts) > 1:
            if context.repeat_policy != "latest":
                raise PolicyResolutionError(f"Repeated course {course_id} has no source-backed repeat policy; completed status and credit are unresolved.")
            if any(item.attempt_order is None for item in attempts) or len({item.attempt_order for item in attempts}) != len(attempts):
                raise PolicyResolutionError(f"Repeated course {course_id} requires unique attempt_order values to apply the supplied latest-performance policy.")
    completed = sorted(course_id for course_id, attempts in grouped.items()
                       if (max(attempts, key=lambda item: item.attempt_order if item.attempt_order is not None else -1).status == AttemptStatus.COMPLETED))
    aliases = {course_id: canonical_course_id(snapshot, course_id) for course_id in snapshot.courses}
    completed = sorted({aliases.get(course_id, course_id) for course_id in completed})
    requirements = context.requirements
    options: dict[str, tuple[tuple[int, ...], ...]] = {}
    for course_id in completed:
        eligible = tuple(index for index, requirement in enumerate(requirements) if any(aliases.get(item, item) == course_id for item in requirement.course_pool))
        mandatory = tuple(index for index in eligible if course_id in {aliases.get(cid, cid) for cid in requirements[index].mandatory_course_ids})
        choices: list[tuple[int, ...]] = [(index,) for index in (mandatory or eligible)]
        shareable = tuple(index for index in eligible if requirements[index].allow_shared_credit)
        for mask in range(1, 1 << len(shareable)):
            shared = tuple(index for bit, index in enumerate(shareable) if mask & (1 << bit))
            if len(shared) > 1:
                choices.append(shared)
        options[course_id] = tuple(dict.fromkeys(choices))

    fixed: dict[int, list[str]] = {index: [] for index in range(len(requirements))}
    ambiguous: list[str] = []
    for course_id in completed:
        candidates = options[course_id]
        if len(candidates) == 1:
            for index in candidates[0]:
                fixed[index].append(course_id)
        elif len(candidates) > 1:
            ambiguous.append(course_id)

    unit_required_courses = {
        course_id
        for course_id, candidates in options.items()
        if candidates and any(requirements[index].metric == Metric.UNITS for choice in candidates for index in choice)
    }
    unit_values = _course_unit_values(profile, unit_required_courses, snapshot, context)

    def contribution(course_id: str, requirement_index: int) -> float:
        requirement = requirements[requirement_index]
        if requirement.metric == Metric.COURSES:
            return 1.0
        return unit_values[course_id]

    def is_mandatory(requirement_index: int, course_id: str) -> bool:
        return course_id in {aliases.get(cid, cid) for cid in requirements[requirement_index].mandatory_course_ids}

    def score(allocation: dict[int, list[str]]) -> tuple[int, float, int]:
        satisfied = 0
        credited_progress = 0.0
        mandatory_allocated = 0
        for index, requirement in enumerate(requirements):
            credited = sum(contribution(course_id, index) for course_id in allocation[index])
            if requirement.required_value is not None:
                mandatory_allocated_here = {aliases.get(cid, cid) for cid in requirement.mandatory_course_ids}.issubset(allocation[index])
                satisfied += credited >= requirement.required_value and mandatory_allocated_here
                credited_progress += min(credited, requirement.required_value)
            else:
                credited_progress += credited
            mandatory_allocated += sum(is_mandatory(index, course_id) for course_id in allocation[index])
        return satisfied, credited_progress, mandatory_allocated

    base_totals = tuple(sum(contribution(course_id, i) for course_id in fixed[i]) for i in range(len(requirements)))
    base_mandatory = tuple(tuple(sorted({aliases.get(cid, cid) for cid in requirements[i].mandatory_course_ids} & set(fixed[i]))) for i in range(len(requirements)))
    states = 0
    @lru_cache(maxsize=None)
    def solve(position: int, totals: tuple[float, ...], mandatory: tuple[tuple[str, ...], ...]) -> tuple[tuple[int, float, int], tuple[tuple[int, ...], ...]]:
        nonlocal states
        states += 1
        if states > MAX_ALLOCATION_STATES:
            raise PolicyResolutionError(f"Requirement allocation exceeds the {MAX_ALLOCATION_STATES:,} memoized-state limit; results are unresolved.")
        if position == len(ambiguous):
            satisfied = sum(requirements[i].required_value is not None and totals[i] >= requirements[i].required_value and {aliases.get(cid, cid) for cid in requirements[i].mandatory_course_ids}.issubset(mandatory[i]) for i in range(len(requirements)))
            progress = sum(min(totals[i], requirements[i].required_value) if requirements[i].required_value is not None else totals[i] for i in range(len(requirements)))
            mandatory_count = sum(sum(is_mandatory(i, cid) for cid in mandatory[i]) for i in range(len(requirements)))
            return (satisfied, progress, mandatory_count), ()
        cid = ambiguous[position]
        best: tuple[tuple[int, float, int], tuple[tuple[int, ...], ...]] | None = None
        for choice in options[cid]:
            new_totals = list(totals)
            new_mandatory = [set(value) for value in mandatory]
            for index in choice:
                new_totals[index] += contribution(cid, index)
                if is_mandatory(index, cid):
                    new_mandatory[index].add(cid)
            subscore, suffix = solve(position + 1, tuple(new_totals), tuple(tuple(sorted(value)) for value in new_mandatory))
            candidate = (subscore, (choice,) + suffix)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
                best = candidate
        return best or ((0, 0.0, 0), ())
    _, choices = solve(0, base_totals, base_mandatory)
    best_allocation = {index: list(values) for index, values in fixed.items()}
    for cid, choice in zip(ambiguous, choices):
        for index in choice:
            best_allocation[index].append(cid)
    return {requirements[index].requirement_id: tuple(sorted(course_ids)) for index, course_ids in best_allocation.items()}


def analyze_requirements(profile: StudentProfile, context: PolicyContext, snapshot: DatasetSnapshot) -> tuple[RequirementResult, ...]:
    allocations = _allocate_completed_courses(profile, context, snapshot)
    unit_course_ids = {
        course_id
        for requirement in context.requirements
        if requirement.metric == Metric.UNITS
        for course_id in allocations[requirement.requirement_id]
    }
    unit_values = _course_unit_values(profile, unit_course_ids, snapshot, context)
    results: list[RequirementResult] = []
    for requirement in context.requirements:
        eligible_completed = list(allocations[requirement.requirement_id])
        if requirement.metric == Metric.COURSES:
            credited = float(len(eligible_completed))
        else:
            credited = sum(unit_values[course_id] for course_id in eligible_completed)
        remaining = None if requirement.required_value is None else max(requirement.required_value - credited, 0)
        outstanding = tuple(course_id for course_id in requirement.mandatory_course_ids if canonical_course_id(snapshot, course_id) not in eligible_completed)
        status = "incomplete" if remaining is None or remaining > 0 or outstanding else "verified"
        results.append(
            RequirementResult(
                requirement_id=requirement.requirement_id,
                category=requirement.category,
                metric=requirement.metric,
                required_amount=requirement.required_value,
                credited_amount=credited,
                remaining_amount=remaining,
                outstanding_mandatory_courses=outstanding,
                credited_course_ids=tuple(eligible_completed),
                status=status,
                evidence_ids=requirement.evidence_ids,
            )
        )
    return tuple(results)


def evaluate_expression(expression: Any, profile: StudentProfile, context: PolicyContext | None = None, snapshot: DatasetSnapshot | None = None) -> CheckResult:
    if expression is None:
        return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "PREREQUISITE_UNRESOLVED", "Prerequisite information was not supplied or could not be parsed.")
    if not isinstance(expression, dict):
        return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Prerequisite expression is malformed.")
    op = expression.get("op")
    if not isinstance(op, str):
        return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Prerequisite operator is malformed.")
    completed = _completed_ids(profile, context)
    if snapshot is not None:
        completed = {canonical_course_id(snapshot, item) for item in completed}
    if op == "completed":
        course_id = expression.get("course_id")
        if not isinstance(course_id, str) or not course_id:
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Completed-course rule is malformed.")
        matching_attempts = [item for item in profile.attempts if item.course_id == course_id]
        if len(matching_attempts) > 1 and (context is None or context.repeat_policy != "latest" or any(item.attempt_order is None for item in matching_attempts)):
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "REPEAT_POLICY_UNRESOLVED", f"Repeated course {course_id} has no applicable source-backed repeat policy and chronology.")
        passed = (canonical_course_id(snapshot, course_id) if snapshot is not None else course_id) in completed
        return CheckResult("prerequisite", DecisionStatus.PASS if passed else DecisionStatus.FAIL, "PREREQUISITE_MET" if passed else "PREREQUISITE_UNMET", f"Required course {course_id} is {'completed' if passed else 'not completed' }.")
    if op == "minimum_semester":
        value = expression.get("value")
        if type(value) is not int or value < 0:
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Minimum-semester rule is malformed.")
        passed = profile.current_semester >= value
        return CheckResult("prerequisite", DecisionStatus.PASS if passed else DecisionStatus.FAIL, "SEMESTER_RULE_MET" if passed else "SEMESTER_RULE_UNMET", f"Current semester {profile.current_semester}; minimum is {value}.")
    if op == "programme_in":
        values = expression.get("programme_ids")
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Programme restriction is malformed.")
        allowed = set(values)
        if not allowed:
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", "Programme restriction has no supplied programme values.")
        passed = bool(set(profile.programme_ids) & allowed)
        return CheckResult("prerequisite", DecisionStatus.PASS if passed else DecisionStatus.FAIL, "PROGRAMME_RULE_MET" if passed else "PROGRAMME_RESTRICTED", "Programme restriction evaluated against the supplied profile.")
    if op in {"all_of", "any_of"}:
        children = expression.get("conditions")
        if not isinstance(children, list):
            return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", f"{op} rule has no valid conditions list.")
        child_results = [evaluate_expression(child, profile, context, snapshot) for child in children]
        statuses = [child.status for child in child_results]
        if op == "all_of":
            status = DecisionStatus.FAIL if DecisionStatus.FAIL in statuses else DecisionStatus.PASS if all(item == DecisionStatus.PASS for item in statuses) else DecisionStatus.UNKNOWN
        else:
            status = DecisionStatus.PASS if DecisionStatus.PASS in statuses else DecisionStatus.FAIL if all(item == DecisionStatus.FAIL for item in statuses) else DecisionStatus.UNKNOWN
        return CheckResult("prerequisite", status, f"{op.upper()}_RESULT", f"{op} prerequisite evaluated to {status.value}.")
    return CheckResult("prerequisite", DecisionStatus.UNKNOWN, "UNSUPPORTED_RULE", f"Unsupported prerequisite operator: {op!r}.")


def evaluate_eligibility(profile: StudentProfile, offering: Offering, requested_category: str | None, context: PolicyContext | None = None, snapshot: DatasetSnapshot | None = None) -> EligibilityResult:
    checks: list[CheckResult] = []
    availability = offering.availability
    if availability.verification_status != VerificationStatus.VERIFIED or availability.value is None:
        checks.append(CheckResult("availability", DecisionStatus.UNKNOWN, "OFFERING_UNVERIFIED", "The supplied timetable does not confirm this offering.", availability.evidence_ids))
    elif availability.value is True:
        checks.append(CheckResult("availability", DecisionStatus.PASS, "OFFERING_AVAILABLE", "The supplied timetable marks this offering as available.", availability.evidence_ids))
    else:
        checks.append(CheckResult("availability", DecisionStatus.FAIL, "OFFERING_UNAVAILABLE", "The supplied data marks this offering as unavailable.", availability.evidence_ids))

    applicable_categories = tuple(
        membership.category
        for membership in offering.categories
        if membership.verification_status == VerificationStatus.VERIFIED
        and (not membership.programme_ids or bool(set(membership.programme_ids) & set(profile.programme_ids)))
    )
    if requested_category:
        memberships = [membership for membership in offering.categories if membership.category == requested_category]
        if requested_category in applicable_categories:
            evidence = next(
                membership.evidence_ids for membership in memberships
                if membership.verification_status == VerificationStatus.VERIFIED
                and (not membership.programme_ids or bool(set(membership.programme_ids) & set(profile.programme_ids)))
            )
            checks.append(CheckResult("category", DecisionStatus.PASS, "CATEGORY_APPLICABLE", f"Listed as {requested_category} for this programme context.", evidence))
        elif memberships:
            checks.append(CheckResult("category", DecisionStatus.UNKNOWN, "CATEGORY_UNVERIFIED", f"The supplied rules do not confirm {requested_category} membership for this programme context."))
        else:
            checks.append(CheckResult("category", DecisionStatus.FAIL, "CATEGORY_NOT_APPLICABLE", f"The course is not supplied as a {requested_category} for this programme context."))

    checks.append(evaluate_expression(offering.prerequisite, profile, context, snapshot))
    statuses = [check.status for check in checks]
    overall = DecisionStatus.FAIL if DecisionStatus.FAIL in statuses else DecisionStatus.PASS if all(status == DecisionStatus.PASS for status in statuses) else DecisionStatus.UNKNOWN
    return EligibilityResult(offering.course_id, offering.offering_id, overall, tuple(checks), applicable_categories)
