from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from recommender.models import AttemptStatus, DatasetSnapshot, DecisionStatus, Fact, QueryIntent, StudentProfile, VerificationStatus, to_dict
from recommender.policies.engine import analyze_requirements, canonical_course_id, evaluate_eligibility, resolve_minor_policy_context, resolve_policy_context
from recommender.query import parse_query
from recommender.services.scheduling import assess_schedule


def _constraint_status(fact: Fact | None, expected: Any) -> DecisionStatus:
    if fact is None or fact.verification_status != VerificationStatus.VERIFIED or fact.value is None:
        return DecisionStatus.UNKNOWN
    return DecisionStatus.PASS if fact.value == expected else DecisionStatus.FAIL


def _topic_score(topics: tuple[str, ...], preferences: tuple[str, ...]) -> int:
    haystack = " ".join(topics).casefold()
    aliases = {
        "artificial intelligence": ("artificial intelligence", "machine learning", "ai"),
        "ai": ("artificial intelligence", "machine learning", "ai"),
    }
    def matches(phrase: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(phrase.casefold())}(?!\w)", haystack) is not None
    return sum(any(matches(alias) for alias in aliases.get(preference.casefold(), (preference,))) for preference in preferences)


class FinalValidationError(RuntimeError):
    """Raised when final validation detects an inconsistent recommendation.

    Only produced by explicit callers of ``_final_validate`` (e.g. tests); the
    normal ``recommend_from_intent`` path degrades gracefully instead.
    """


def _final_validate(
    recommendations: list[dict[str, Any]],
    profile: StudentProfile,
    intent: QueryIntent,
    snapshot: DatasetSnapshot,
    context: Any | None = None,
) -> None:
    """Re-evaluate academic and hard-property checks before rendering."""
    failures = _final_validation_failures(recommendations, profile, intent, snapshot, context)
    if failures:
        raise FinalValidationError(failures[0]["reason"])


def _final_validation_failures(
    recommendations: list[dict[str, Any]],
    profile: StudentProfile,
    intent: QueryIntent,
    snapshot: DatasetSnapshot,
    context: Any | None = None,
) -> list[dict[str, Any]]:
    """Return one failure record per recommendation that no longer validates."""
    offerings = {offering.offering_id: offering for offering in snapshot.offerings}
    context = context or resolve_policy_context(profile, snapshot)
    excluded_courses = _excluded_course_ids(profile, context, snapshot)
    failures: list[dict[str, Any]] = []
    for item in recommendations:
        offering = offerings.get(item["offering_id"])
        reason = None
        if offering is None or offering.course_id != item["course_id"]:
            reason = "Final validation rejected a mismatched offering reference."
        elif offering.campus.casefold() != profile.campus.casefold() or offering.semester_id != profile.target_semester_id:
            reason = "Final validation rejected an offering outside the requested campus or semester."
        elif canonical_course_id(snapshot, offering.course_id) in excluded_courses:
            reason = "Final validation rejected a completed or in-progress course."
        else:
            try:
                eligibility = evaluate_eligibility(profile, offering, intent.requested_category, context, snapshot)
            except (StopIteration, RuntimeError):
                
                eligibility = None
            if eligibility is None or eligibility.overall_status != DecisionStatus.PASS:
                reason = "Final validation rejected a non-eligible recommendation."
            else:
                for constraint in intent.hard_constraints:
                    if _constraint_status(offering.handout_facts.get(constraint.field), constraint.value) != DecisionStatus.PASS:
                        reason = "Final validation rejected an unmet or unsupported hard constraint."
                        break
        if reason is not None:
            failures.append({
                "offering_id": item.get("offering_id"),
                "course_code": item.get("course_code"),
                "reason": reason,
            })
    return failures


def _excluded_course_ids(profile: StudentProfile, context: Any, snapshot: DatasetSnapshot) -> set[str]:
    grouped: dict[str, list[Any]] = {}
    for attempt in profile.attempts:
        grouped.setdefault(attempt.course_id, []).append(attempt)
    excluded: set[str] = set()
    for course_id, attempts in grouped.items():
        if len(attempts) > 1 and context.repeat_policy == "latest" and all(item.attempt_order is not None for item in attempts):
            status = max(attempts, key=lambda item: item.attempt_order).status
        else:
            status = attempts[-1].status if len(attempts) == 1 else None
        if status in {AttemptStatus.COMPLETED, AttemptStatus.IN_PROGRESS}:
            excluded.add(canonical_course_id(snapshot, course_id))
    return excluded


def recommend(profile: StudentProfile, query: str, snapshot: DatasetSnapshot) -> dict[str, Any]:
    return recommend_from_intent(profile, parse_query(query), snapshot)


def recommend_from_intent(profile: StudentProfile, intent: QueryIntent, snapshot: DatasetSnapshot) -> dict[str, Any]:
    context = resolve_policy_context(profile, snapshot)
    minor_context = resolve_minor_policy_context(profile, snapshot)
    warnings: list[str] = []
    if profile.minor_id and minor_context is None:
        warnings.append(f"No supplied policy context matches minor {profile.minor_id}; minor requirements are unresolved.")
    elif minor_context is not None:
        minor_requirements = tuple(replace(req, requirement_id=f"minor:{profile.minor_id}:{req.requirement_id}") for req in minor_context.requirements)
        context = replace(context, requirements=context.requirements + minor_requirements,
                          repeat_policy=context.repeat_policy if context.repeat_policy == minor_context.repeat_policy else "unresolved")
    requirements = analyze_requirements(profile, context, snapshot)
    base: dict[str, Any] = {
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "dataset_version": snapshot.dataset_version,
        "synthetic": snapshot.synthetic,
        "target_semester_id": profile.target_semester_id,
        "interpreted_query": to_dict(intent),
        "requirement_summary": [to_dict(item) for item in requirements],
        "recommendations": [],
        "unverified_alternatives": [],
        "warnings": warnings,
        "no_result_reason": None,
        "clarification_questions": list(intent.clarification_questions),
        "final_validation_status": "not_run",
    }
    if intent.clarification_questions:
        base["no_result_reason"] = "The request needs clarification before recommendations can be made."
        return base

    candidates = [
        offering for offering in snapshot.offerings
        if offering.campus.casefold() == profile.campus.casefold() and offering.semester_id == profile.target_semester_id
    ]
    eligible_count = 0
    hard_failed_count = 0
    hard_unknown_count = 0
    ranked: list[tuple[int, str, str, dict[str, Any]]] = []
    excluded_courses = _excluded_course_ids(profile, context, snapshot)
    for offering in candidates:
        if canonical_course_id(snapshot, offering.course_id) in excluded_courses:
            continue
        course = snapshot.courses[offering.course_id]
        eligibility = evaluate_eligibility(profile, offering, intent.requested_category, context, snapshot)
        if eligibility.overall_status != DecisionStatus.PASS:
            continue
        eligible_count += 1
        hard_checks = []
        for constraint in intent.hard_constraints:
            status = _constraint_status(offering.handout_facts.get(constraint.field), constraint.value)
            hard_checks.append({"field": constraint.field, "status": status.value, "expected": constraint.value})
        if any(check["status"] == DecisionStatus.FAIL for check in hard_checks):
            hard_failed_count += 1
            continue
        if any(check["status"] == DecisionStatus.UNKNOWN for check in hard_checks):
            hard_unknown_count += 1
            base["unverified_alternatives"].append({
                "course_code": course.code,
                "title": course.title,
                "reason": "The supplied documents do not confirm one or more requested properties.",
                "hard_constraint_checks": hard_checks,
            })
            continue
        soft_matches = []
        for preference in intent.soft_preferences:
            status = _constraint_status(offering.handout_facts.get(preference.field), preference.value)
            soft_matches.append({"field": preference.field, "status": status.value})
        score_preferences = intent.topic_preferences or profile.interests
        score = 2 * _topic_score(course.topics, score_preferences) + sum(item["status"] == "pass" for item in soft_matches)
        relevant_properties = {
            name: {"value": fact.value, "verification_status": fact.verification_status.value, "evidence_ids": list(fact.evidence_ids)}
            for name, fact in offering.handout_facts.items()
            if name in {constraint.field for constraint in (*intent.hard_constraints, *intent.soft_preferences)}
        }
        requirement = next((
            item for item in requirements
            if item.category == intent.requested_category
            and canonical_course_id(snapshot, offering.course_id) in {canonical_course_id(snapshot, cid) for cid in (
                next((req.course_pool for req in context.requirements if req.requirement_id == item.requirement_id), ())
            )}
            and ((item.remaining_amount is not None and item.remaining_amount > 0)
                 or offering.course_id in item.outstanding_mandatory_courses)
        ), None)
        schedule = assess_schedule(profile, offering, snapshot)
        if schedule.status == "unknown" and schedule.reason:
            warning = f"Schedule unresolved for {course.code}: {schedule.reason}"
            if warning not in base["warnings"]:
                base["warnings"].append(warning)
        evidence_references = set(course.evidence_ids + offering.evidence_ids)
        if requirement is not None:
            evidence_references.update(requirement.evidence_ids)
        for check in eligibility.checks:
            evidence_references.update(check.evidence_ids)
        for value in relevant_properties.values():
            evidence_references.update(value["evidence_ids"])
        card = {
            "course_id": course.course_id,
            "course_code": course.code,
            "title": course.title,
            "offering_id": offering.offering_id,
            "category_for_this_student": intent.requested_category,
            "requirement_contribution": None if requirement is None else {
                "requirement_id": requirement.requirement_id,
                "remaining_before_selection": requirement.remaining_amount,
                "metric": requirement.metric,
                "evidence_ids": list(requirement.evidence_ids),
            },
            "eligibility_status": eligibility.overall_status.value,
            "eligibility_reasons": [to_dict(check) for check in eligibility.checks],
            "matched_topics": [topic for topic in course.topics if _topic_score((topic,), intent.topic_preferences)],
            "soft_preference_checks": soft_matches,
            "relevant_handout_properties": relevant_properties,
            "schedule_validation_status": schedule.status,
            "schedule_conflicts": list(schedule.conflicts),
            "evidence_references": sorted(evidence_references),
            "score": score,
        }
        ranked.append((-score, course.code, offering.offering_id, card))
    base["recommendations"] = [item[3] for item in sorted(ranked, key=lambda item: item[:3])]
    validation_failures = _final_validation_failures(base["recommendations"], profile, intent, snapshot, context)
    failed_offering_ids = {failure["offering_id"] for failure in validation_failures}
    if failed_offering_ids:
        
        
        base["recommendations"] = [item for item in base["recommendations"] if item["offering_id"] not in failed_offering_ids]
        for failure in validation_failures:
            warning = f"Final validation removed {failure['course_code'] or failure['offering_id']}: {failure['reason']}"
            if warning not in base["warnings"]:
                base["warnings"].append(warning)
        base["final_validation_status"] = "pass_with_removals"
    else:
        base["final_validation_status"] = "pass"
    if not base["recommendations"]:
        if not candidates:
            base["no_result_reason"] = "No offerings were supplied for this campus and target semester."
        elif eligible_count == 0:
            base["no_result_reason"] = "No supplied offerings are academically eligible for this profile and requested category."
        elif hard_unknown_count:
            base["no_result_reason"] = "Eligible offerings exist, but the supplied documents do not confirm the requested property."
        elif hard_failed_count:
            base["no_result_reason"] = "Eligible offerings exist, but none satisfies every hard preference."
        else:
            base["no_result_reason"] = "No supported match was found."
    return base
