from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recommender.models import (
    CategoryMembership,
    Course,
    DatasetSnapshot,
    Evidence,
    EquivalenceGroup,
    Fact,
    Metric,
    Meeting,
    Offering,
    PolicyContext,
    Requirement,
    VerificationStatus,
)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


class SnapshotError(ValueError):
    pass


def _fact(raw: dict[str, Any]) -> Fact:
    return Fact(
        value=raw.get("value"),
        verification_status=VerificationStatus(raw["verification_status"]),
        evidence_ids=tuple(raw.get("evidence_ids", [])),
    )


def _require_unique(items: list[dict[str, Any]], key: str, label: str) -> None:
    values = [item[key] for item in items]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise SnapshotError(f"Duplicate {label}: {', '.join(duplicates)}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{label} must be an object")
    return value


def _list(value: Any, label: str, *, default: Any = None) -> list[Any]:
    if value is None and default is not None:
        value = default
    if not isinstance(value, list):
        raise SnapshotError(f"{label} must be an array")
    return value


def _strings(value: Any, label: str, *, default: Any = None) -> list[str]:
    values = _list(value, label, default=default)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise SnapshotError(f"{label} must contain non-empty strings")
    return values


def _finite_number(value: Any, label: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    try:
        finite = math.isfinite(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise SnapshotError(f"{label} must be a finite non-negative number" + (" or null" if optional else ""))
    return value


def _validate_raw(raw: Any) -> dict[str, Any]:
    root = _object(raw, "Snapshot root")
    if not isinstance(root.get("dataset_version"), str):
        raise SnapshotError("dataset_version must be a string")
    if "synthetic" in root and not isinstance(root["synthetic"], bool):
        raise SnapshotError("synthetic must be a boolean")
    for key in ("documents", "evidence", "courses", "offerings", "policy_contexts", "equivalence_groups"):
        values = _list(root.get(key, []), key)
        for index, value in enumerate(values):
            _object(value, f"{key}[{index}]")
    required = {
        "documents": ("document_id",),
        "evidence": ("evidence_id", "document_id", "excerpt", "verification_status"),
        "courses": ("course_id", "code", "title"),
        "offerings": ("offering_id", "course_id", "campus", "semester_id"),
        "policy_contexts": ("context_id", "campus", "admission_year_from", "admission_year_to"),
    }
    for collection, fields in required.items():
        for index, item in enumerate(root.get(collection, [])):
            for field in fields:
                if field not in item:
                    raise SnapshotError(f"{collection}[{index}].{field} is required")
                if field in {"admission_year_from", "admission_year_to"}:
                    continue
                if not isinstance(item[field], str) or not item[field].strip():
                    raise SnapshotError(f"{collection}[{index}].{field} must be a non-empty string")
    for index, evidence in enumerate(root.get("evidence", [])):
        if "verification_status" not in evidence:
            raise SnapshotError(f"evidence[{index}].verification_status is required")
        if evidence.get("section") is not None and not isinstance(evidence["section"], str):
            raise SnapshotError(f"evidence[{index}].section must be a string or null")
    for index, course in enumerate(root.get("courses", [])):
        _finite_number(course.get("units"), f"courses[{index}].units", optional=True)
        _strings(course.get("topics", []), f"courses[{index}].topics")
        _strings(course.get("evidence_ids", []), f"courses[{index}].evidence_ids")
    for index, evidence in enumerate(root.get("evidence", [])):
        if evidence.get("page") is not None and (type(evidence["page"]) is not int or evidence["page"] < 0):
            raise SnapshotError(f"evidence[{index}].page must be a non-negative integer or null")
    for index, offering in enumerate(root.get("offerings", [])):
        if "availability" not in offering:
            raise SnapshotError(f"offerings[{index}].availability is required")
        for key in ("availability",):
            fact = _object(offering.get(key), f"offerings[{index}].{key}")
            if "verification_status" not in fact:
                raise SnapshotError(f"offerings[{index}].{key}.verification_status is required")
            if not isinstance(fact.get("value"), bool) and fact.get("value") is not None:
                raise SnapshotError(f"offerings[{index}].availability.value must be a boolean or null")
            _strings(fact.get("evidence_ids", []), f"offerings[{index}].{key}.evidence_ids")
        for key in ("evidence_ids",):
            _strings(offering.get(key, []), f"offerings[{index}].{key}")
        for mi, meeting in enumerate(_list(offering.get("schedule", []), f"offerings[{index}].schedule")):
            meeting = _object(meeting, f"offerings[{index}].schedule[{mi}]")
            for field in ("day", "start", "end"):
                if not isinstance(meeting.get(field), str) or not meeting[field].strip():
                    raise SnapshotError(f"offerings[{index}].schedule[{mi}].{field} must be a non-empty string")
            if meeting.get("component") is not None and not isinstance(meeting["component"], str):
                raise SnapshotError(f"offerings[{index}].schedule[{mi}].component must be a string or null")
        categories = _list(offering.get("categories", []), f"offerings[{index}].categories")
        for ci, membership in enumerate(categories):
            membership = _object(membership, f"offerings[{index}].categories[{ci}]")
            for field in ("category", "verification_status"):
                if not isinstance(membership.get(field), str):
                    raise SnapshotError(f"offerings[{index}].categories[{ci}].{field} must be a string")
            _strings(membership.get("programme_ids", []), f"offerings[{index}].categories[{ci}].programme_ids")
            _strings(membership.get("evidence_ids", []), f"offerings[{index}].categories[{ci}].evidence_ids")
        facts = _object(offering.get("handout_facts", {}), f"offerings[{index}].handout_facts")
        for name, value in facts.items():
            fact = _object(value, f"offerings[{index}].handout_facts.{name}")
            if "verification_status" not in fact:
                raise SnapshotError(f"offerings[{index}].handout_facts.{name}.verification_status is required")
            if name in {"midsem_present", "attendance_required", "project_present"} and not isinstance(fact.get("value"), bool) and fact.get("value") is not None:
                raise SnapshotError(f"offerings[{index}].handout_facts.{name}.value must be a boolean or null")
            _strings(fact.get("evidence_ids", []), f"offerings[{index}].handout_facts.{name}.evidence_ids")
        _validate_prerequisite(offering.get("prerequisite"), f"offerings[{index}].prerequisite")
    for index, context in enumerate(root.get("policy_contexts", [])):
        _strings(context.get("programme_ids", []), f"policy_contexts[{index}].programme_ids")
        _strings(context.get("evidence_ids", []), f"policy_contexts[{index}].evidence_ids")
        for key in ("admission_year_from", "admission_year_to"):
            if type(context.get(key)) is not int:
                raise SnapshotError(f"policy_contexts[{index}].{key} must be an integer")
        if context["admission_year_from"] > context["admission_year_to"]:
            raise SnapshotError(f"policy_contexts[{index}] admission year range is reversed")
        requirements = _list(context.get("requirements", []), f"policy_contexts[{index}].requirements")
        for ri, req in enumerate(requirements):
            req = _object(req, f"policy_contexts[{index}].requirements[{ri}]")
            for field in ("requirement_id", "category", "metric"):
                if not isinstance(req.get(field), str):
                    raise SnapshotError(f"requirement {ri}.{field} must be a string")
            _finite_number(req.get("required_value"), f"requirement {req.get('requirement_id', ri)}.required_value", optional=True)
            pool = _strings(req.get("course_pool", []), f"requirement {ri}.course_pool")
            mandatory = _strings(req.get("mandatory_course_ids", []), f"requirement {ri}.mandatory_course_ids")
            if not set(mandatory).issubset(pool):
                raise SnapshotError(f"requirement {ri}.mandatory_course_ids must be included in course_pool")
            _strings(req.get("evidence_ids", []), f"requirement {ri}.evidence_ids")
            if "allow_shared_credit" in req and not isinstance(req["allow_shared_credit"], bool):
                raise SnapshotError(f"requirement {ri}.allow_shared_credit must be a boolean")
        if context.get("repeat_policy", "unresolved") not in {"unresolved", "latest"}:
            raise SnapshotError(f"policy_contexts[{index}].repeat_policy is unsupported")
    for gi, group in enumerate(root.get("equivalence_groups", [])):
        _strings(group.get("course_ids"), f"equivalence_groups[{gi}].course_ids")
        _strings(group.get("evidence_ids"), f"equivalence_groups[{gi}].evidence_ids")
        if len(set(group.get("course_ids", []))) < 2:
            raise SnapshotError(f"equivalence_groups[{gi}].course_ids must contain at least two unique courses")
        if not isinstance(group.get("verification_status"), str):
            raise SnapshotError(f"equivalence_groups[{gi}].verification_status is required")
    return root


def _validate_prerequisite(expression: Any, label: str) -> None:
    if expression is None:
        return
    expression = _object(expression, label)
    op = expression.get("op")
    if op == "completed":
        if not isinstance(expression.get("course_id"), str):
            raise SnapshotError(f"{label}.course_id must be a string")
    elif op in {"all_of", "any_of"}:
        for index, condition in enumerate(_list(expression.get("conditions"), f"{label}.conditions")):
            _validate_prerequisite(condition, f"{label}.conditions[{index}]")
    elif op == "minimum_semester":
        if type(expression.get("value")) is not int or expression["value"] < 0:
            raise SnapshotError(f"{label}.value must be a non-negative integer")
    elif op == "programme_in":
        _strings(expression.get("programme_ids"), f"{label}.programme_ids")


def load_snapshot(path: str | Path) -> DatasetSnapshot:
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"Unable to read dataset snapshot {source}: {exc}") from exc
    return _load_snapshot_payload(payload, source)


def _load_snapshot_payload(payload: bytes, source: Path) -> DatasetSnapshot:
    try:
        raw = _validate_raw(json.loads(payload.decode("utf-8")))
        _require_unique(raw.get("documents", []), "document_id", "document IDs")
        _require_unique(raw.get("evidence", []), "evidence_id", "evidence IDs")
        _require_unique(raw.get("courses", []), "course_id", "course IDs")
        _require_unique(raw.get("offerings", []), "offering_id", "offering IDs")
        _require_unique(raw.get("policy_contexts", []), "context_id", "policy context IDs")
        evidence = {
            item["evidence_id"]: Evidence(
                evidence_id=item["evidence_id"],
                document_id=item["document_id"],
                page=item.get("page"),
                section=item.get("section"),
                excerpt=item["excerpt"],
                verification_status=VerificationStatus(item["verification_status"]),
            )
            for item in raw.get("evidence", [])
        }
        courses = {
            item["course_id"]: Course(
                course_id=item["course_id"],
                code=item["code"],
                title=item["title"],
                units=item.get("units"),
                topics=tuple(item.get("topics", [])),
                evidence_ids=tuple(item.get("evidence_ids", [])),
            )
            for item in raw.get("courses", [])
        }
        offerings = tuple(
            Offering(
                offering_id=item["offering_id"],
                course_id=item["course_id"],
                campus=item["campus"],
                semester_id=item["semester_id"],
                availability=_fact(item["availability"]),
                categories=tuple(
                    CategoryMembership(
                        category=membership["category"],
                        programme_ids=tuple(membership.get("programme_ids", [])),
                        verification_status=VerificationStatus(membership["verification_status"]),
                        evidence_ids=tuple(membership.get("evidence_ids", [])),
                    )
                    for membership in item.get("categories", [])
                ),
                prerequisite=item.get("prerequisite"),
                handout_facts={key: _fact(value) for key, value in item.get("handout_facts", {}).items()},
                evidence_ids=tuple(item.get("evidence_ids", [])),
                schedule=tuple(Meeting(**meeting) for meeting in item.get("schedule", [])),
            )
            for item in raw.get("offerings", [])
        )
        contexts = tuple(
            PolicyContext(
                context_id=item["context_id"],
                campus=item["campus"],
                admission_year_from=item["admission_year_from"],
                admission_year_to=item["admission_year_to"],
                programme_ids=tuple(item.get("programme_ids", [])),
                requirements=tuple(
                    Requirement(
                        requirement_id=req["requirement_id"],
                        category=req["category"],
                        metric=Metric(req["metric"]),
                        required_value=req.get("required_value"),
                        course_pool=tuple(req.get("course_pool", [])),
                        mandatory_course_ids=tuple(req.get("mandatory_course_ids", [])),
                        evidence_ids=tuple(req.get("evidence_ids", [])),
                        allow_shared_credit=req.get("allow_shared_credit", False),
                    )
                    for req in item.get("requirements", [])
                ),
                evidence_ids=tuple(item.get("evidence_ids", [])),
                minor_id=item.get("minor_id"),
                repeat_policy=item.get("repeat_policy", "unresolved"),
            )
            for item in raw.get("policy_contexts", [])
        )
        snapshot = DatasetSnapshot(
            dataset_version=raw["dataset_version"],
            synthetic=raw.get("synthetic", False),
            documents=tuple(raw.get("documents", [])),
            evidence=evidence,
            courses=courses,
            offerings=offerings,
            policy_contexts=contexts,
            equivalence_groups=tuple(
                EquivalenceGroup(
                    group_id=item["group_id"],
                    course_ids=tuple(item["course_ids"]),
                    evidence_ids=tuple(item["evidence_ids"]),
                    verification_status=VerificationStatus(item["verification_status"]),
                ) for item in raw.get("equivalence_groups", [])
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnapshotError(f"Invalid snapshot schema in {source}: {exc}") from exc

    errors = [issue for issue in validate_snapshot(snapshot) if issue.severity == "error"]
    if errors:
        raise SnapshotError("; ".join(issue.message for issue in errors))
    return snapshot


def validate_snapshot(snapshot: DatasetSnapshot) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    evidence_ids = set(snapshot.evidence)
    course_ids = set(snapshot.courses)
    document_ids = {
        document.get("document_id")
        for document in snapshot.documents
        if isinstance(document, dict) and document.get("document_id")
    }

    def evidence_check(owner: str, ids: tuple[str, ...]) -> None:
        for evidence_id in ids:
            if evidence_id not in evidence_ids:
                issues.append(ValidationIssue("error", "UNKNOWN_EVIDENCE", f"{owner} references unknown evidence {evidence_id}"))

    def verified_claim_check(owner: str, ids: tuple[str, ...]) -> None:
        """Require every verified claim to have at least one verified source record."""
        evidence_check(owner, ids)
        if not ids:
            issues.append(ValidationIssue("error", "MISSING_EVIDENCE", f"{owner} is verified but has no evidence references"))
            return
        known = [snapshot.evidence[evidence_id] for evidence_id in ids if evidence_id in snapshot.evidence]
        if known and not any(item.verification_status == VerificationStatus.VERIFIED for item in known):
            issues.append(ValidationIssue("error", "UNVERIFIED_EVIDENCE", f"{owner} is verified but none of its evidence records is verified"))

    def prerequisite_check(owner: str, expression: dict[str, Any] | None) -> None:
        if expression is None:
            issues.append(ValidationIssue("warning", "PREREQUISITE_NOT_EXTRACTED", f"{owner} has unresolved prerequisite information"))
            return
        op = expression.get("op")
        if op == "completed":
            if expression.get("course_id") not in course_ids:
                issues.append(ValidationIssue("error", "UNKNOWN_PREREQUISITE_COURSE", f"{owner} references unknown prerequisite course {expression.get('course_id')}"))
        elif op in {"all_of", "any_of"}:
            conditions = expression.get("conditions")
            if not isinstance(conditions, list):
                issues.append(ValidationIssue("error", "MALFORMED_PREREQUISITE", f"{owner} has a malformed {op} expression"))
            else:
                for index, condition in enumerate(conditions):
                    if not isinstance(condition, dict):
                        issues.append(ValidationIssue("error", "MALFORMED_PREREQUISITE", f"{owner} condition {index} is not an object"))
                    else:
                        prerequisite_check(f"{owner} condition {index}", condition)
        elif op == "minimum_semester":
            if not isinstance(expression.get("value"), int):
                issues.append(ValidationIssue("error", "MALFORMED_PREREQUISITE", f"{owner} has an invalid minimum semester"))
        elif op == "programme_in":
            if not expression.get("programme_ids"):
                issues.append(ValidationIssue("error", "MALFORMED_PREREQUISITE", f"{owner} has an empty programme restriction"))
        else:
            issues.append(ValidationIssue("warning", "UNSUPPORTED_PREREQUISITE", f"{owner} uses unsupported operator {op!r}; eligibility will remain unknown"))

    if not snapshot.dataset_version.strip():
        issues.append(ValidationIssue("error", "MISSING_VERSION", "dataset_version must not be empty"))
    if not snapshot.documents:
        issues.append(ValidationIssue("warning", "NO_DOCUMENTS", "Snapshot has no registered source documents"))
    for evidence in snapshot.evidence.values():
        if evidence.document_id not in document_ids:
            issues.append(
                ValidationIssue(
                    "error",
                    "UNKNOWN_EVIDENCE_DOCUMENT",
                    f"Evidence {evidence.evidence_id} references unknown document {evidence.document_id}",
                )
            )
        if not evidence.excerpt.strip():
            issues.append(ValidationIssue("error", "EMPTY_EVIDENCE_EXCERPT", f"Evidence {evidence.evidence_id} has no supporting excerpt"))
    for course in snapshot.courses.values():
        verified_claim_check(f"course {course.course_id}", course.evidence_ids)
    offering_ids: set[str] = set()
    for offering in snapshot.offerings:
        if offering.offering_id in offering_ids:
            issues.append(ValidationIssue("error", "DUPLICATE_OFFERING", f"Duplicate offering {offering.offering_id}"))
        offering_ids.add(offering.offering_id)
        if offering.course_id not in course_ids:
            issues.append(ValidationIssue("error", "UNKNOWN_COURSE", f"Offering {offering.offering_id} references unknown course {offering.course_id}"))
        verified_claim_check(f"offering {offering.offering_id}", offering.evidence_ids)
        if offering.availability.verification_status == VerificationStatus.VERIFIED:
            verified_claim_check(f"availability {offering.offering_id}", offering.availability.evidence_ids)
        else:
            evidence_check(f"availability {offering.offering_id}", offering.availability.evidence_ids)
        prerequisite_check(f"offering {offering.offering_id}", offering.prerequisite)
        for membership in offering.categories:
            if membership.verification_status == VerificationStatus.VERIFIED:
                verified_claim_check(f"category {offering.offering_id}/{membership.category}", membership.evidence_ids)
            else:
                evidence_check(f"category {offering.offering_id}/{membership.category}", membership.evidence_ids)
        for name, fact in offering.handout_facts.items():
            if fact.verification_status == VerificationStatus.VERIFIED:
                verified_claim_check(f"fact {offering.offering_id}/{name}", fact.evidence_ids)
            else:
                evidence_check(f"fact {offering.offering_id}/{name}", fact.evidence_ids)
    for context in snapshot.policy_contexts:
        verified_claim_check(f"policy context {context.context_id}", context.evidence_ids)
        requirement_ids: set[str] = set()
        for requirement in context.requirements:
            if requirement.requirement_id in requirement_ids:
                issues.append(ValidationIssue("error", "DUPLICATE_REQUIREMENT", f"Policy context {context.context_id} repeats requirement {requirement.requirement_id}"))
            requirement_ids.add(requirement.requirement_id)
            verified_claim_check(f"requirement {requirement.requirement_id}", requirement.evidence_ids)
            for course_id in (*requirement.course_pool, *requirement.mandatory_course_ids):
                if course_id not in course_ids:
                    issues.append(ValidationIssue("error", "UNKNOWN_REQUIREMENT_COURSE", f"Requirement {requirement.requirement_id} references unknown course {course_id}"))
            if not set(requirement.mandatory_course_ids).issubset(requirement.course_pool):
                issues.append(ValidationIssue("error", "MANDATORY_COURSE_OUTSIDE_POOL", f"Requirement {requirement.requirement_id} has a mandatory course outside its course pool"))
            if requirement.required_value is None:
                issues.append(ValidationIssue("warning", "UNKNOWN_REQUIREMENT_TOTAL", f"Requirement {requirement.requirement_id} has an unknown total"))
    group_ids: set[str] = set()
    for group in snapshot.equivalence_groups:
        if group.group_id in group_ids:
            issues.append(ValidationIssue("error", "DUPLICATE_EQUIVALENCE_GROUP", f"Duplicate equivalence group {group.group_id}"))
        group_ids.add(group.group_id)
        for course_id in group.course_ids:
            if course_id not in course_ids:
                issues.append(ValidationIssue("error", "UNKNOWN_EQUIVALENCE_COURSE", f"Equivalence group {group.group_id} references unknown course {course_id}"))
        if group.verification_status == VerificationStatus.VERIFIED:
            verified_claim_check(f"equivalence group {group.group_id}", group.evidence_ids)
        else:
            evidence_check(f"equivalence group {group.group_id}", group.evidence_ids)
    return issues


def publish_snapshot(candidate: str | Path, output: str | Path) -> DatasetSnapshot:
    """Validate, then atomically replace the active normalized snapshot."""
    source = Path(candidate)
    destination = Path(output)
    try:
        payload = source.read_bytes()
        snapshot = _load_snapshot_payload(payload, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError(f"Unable to publish snapshot to {destination}: {exc}") from exc
    return snapshot


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
