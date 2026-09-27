"""Source-linked extraction drafts and explicit, validated publication."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recommender.ingestion.documents import write_json_atomic
from recommender.ingestion.handouts import parse_handout_index
from recommender.ingestion.snapshots import SnapshotError, load_snapshot, publish_snapshot

CODE = re.compile(r"\b([A-Z]{2,5})\s+([A-Z]\d{3}[A-Z]?(?:-\d+)?)\b")


def course_codes(text: str) -> list[str]:
    return list(dict.fromkeys(f"{m[0]} {m[1]}" for m in CODE.findall(text)))


def build_review_bundle(corpus: str | Path, index: str | Path, output: str | Path,
                        *, campus: str = "Pilani", semester: str = "2026-T1",
                        admission_year: int = 2025) -> dict[str, Any]:
    """Extract candidates, never silently promote an extracted claim to verified."""
    db = sqlite3.connect(Path(corpus).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        documents = {row['document_id']: dict(row) for row in db.execute('SELECT * FROM documents')}
        sources = {row['file_name']: row['document_id'] for row in db.execute('SELECT * FROM sources')}
        pages = {(row['document_id'], row['page_number']): row['text'] for row in db.execute('SELECT * FROM pages')}
    finally:
        db.close()
    evidence: dict[str, dict] = {}
    courses: dict[str, dict] = {}
    offerings: list[dict] = []
    notes: list[str] = []

    def cite(document_id: str, page: int, excerpt: str, purpose: str) -> str:
        digest = hashlib.sha256(f'{document_id}:{page}:{purpose}:{excerpt}'.encode()).hexdigest()[:18]
        key = f'EV-{digest}'
        evidence[key] = dict(evidence_id=key, document_id=document_id, page=page,
                             section=purpose, excerpt=excerpt.strip(), verification_status='needs_review')
        return key

    def add_course(code: str, title: str, refs: list[str]) -> None:
        if code not in courses:
            courses[code] = dict(course_id=code, code=code, title=title, units=None, topics=[], evidence_ids=refs)
        else:
            courses[code]['evidence_ids'] = sorted(set(courses[code]['evidence_ids'] + refs))

    for entry in parse_handout_index(index):
        codes = course_codes(entry.course_code)
        if not codes:
            continue
        code = codes[0]
        doc = sources.get(entry.filename)
        if doc is None:
            notes.append(f'Missing indexed handout: {entry.filename}')
            continue
        page_text = pages.get((doc, 1), '')
        if not page_text:
            continue
        ref = cite(doc, 1, page_text[:2500], 'Course identity and offering scope — confirm campus and term')
        add_course(code, entry.course_title, [ref])
        facts = {}
        for (doc_id, page), text in pages.items():
            if doc_id != doc:
                continue
            for field, pattern in {
                'midsem_present': r'(?im)^.*\bmid[ -]?(?:sem(?:ester)?)(?:\s+(?:exam|test))?.*$',
                'project_present': r'(?im)^.*\bproject(?:s)?\b.*$',
                'attendance_required': r'(?im)^.*\battendance\b.*$',
            }.items():
                match = re.search(pattern, text)
                if match:
                    fact_ref = cite(doc, page, match.group(0), f'Review {field}; mention alone does not establish the value')
                    facts.setdefault(field, dict(value=None, verification_status='needs_review', evidence_ids=[fact_ref]))
        offerings.append(dict(offering_id=f'{code.replace(" ", "-")}-{entry.component_code}-{semester}',
            course_id=code, campus=campus, semester_id=semester,
            availability=dict(value=None, verification_status='needs_review', evidence_ids=[ref]),
            categories=[], prerequisite=None, handout_facts=facts, evidence_ids=[ref]))

    
    
    prerequisites: dict[str, tuple[dict, str]] = {}
    for name, doc in sources.items():
        if 'pre-requisite' not in name.casefold():
            continue
        for (doc_id, page), text in pages.items():
            if doc_id != doc:
                continue
            for line in text.splitlines():
                codes = course_codes(line)
                if len(codes) != 2 or not re.search(r'\bPRE\b', line) or re.search(r'\b(?:OR|CO)\b', line):
                    continue
                ref = cite(doc, page, line, 'Candidate prerequisite')
                add_course(codes[1], courses.get(codes[1], {}).get('title', codes[1]), [ref])
                prerequisites[codes[0]] = ({'op': 'completed', 'course_id': codes[1]}, ref)
    for offering in offerings:
        if offering['course_id'] in prerequisites:
            expression, ref = prerequisites[offering['course_id']]
            offering['prerequisite'] = expression
            offering['evidence_ids'].append(ref)

    contexts = []
    
    
    for name, doc in sources.items():
        if 'bulletin' not in name.casefold():
            continue
        for (doc_id, page), text in pages.items():
            if doc_id != doc or not re.search(r'^Semester-wise Pattern for Students Admitted to B\.?\s*E\.? Computer Science Programme', text):
                continue
            total = re.search(r'Discipline Core\s*[-–]?\s*(\d+)\s*Units\s*\((\d+)\s*Courses\)', text)
            core = [code for code in course_codes(text) if re.match(r'CS F[234]\d\d$', code)]
            if not total or len(core) != int(total[2]):
                notes.append(f'Core table needs manual extraction: {name}, page {page}')
                continue
            ref = cite(doc, page, text, 'CS curriculum — confirm admission batch, campus and programme scope')
            for code in core:
                add_course(code, courses.get(code, {}).get('title', code), [ref])
            contexts.append(dict(context_id=f'{campus}-BE-CS-{admission_year}', campus=campus,
                admission_year_from=admission_year, admission_year_to=admission_year,
                programme_ids=['BE-CS'], evidence_ids=[ref],
                requirements=[dict(requirement_id='BE-CS-CDC', category='CDC', metric='courses',
                    required_value=int(total[2]), course_pool=core, mandatory_course_ids=core, evidence_ids=[ref])]))
            for offering in offerings:
                if offering['course_id'] in core:
                    offering['categories'].append(dict(category='CDC', programme_ids=['BE-CS'],
                        verification_status='needs_review', evidence_ids=[ref]))
            notes.append('Seeded BE-CS core only. Add verified DEL/HUEL/OPEL, unit totals, minor and dual-degree rules before claiming complete graduation progress.')
            break
        if contexts:
            break
    used_docs = {item['document_id'] for item in evidence.values()}
    snapshot = dict(dataset_version='draft-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        synthetic=False, documents=[dict(document_id=doc, file_name=documents[doc]['primary_file_name'],
        content_hash=documents[doc]['content_sha256'], corpus_document_id=doc) for doc in sorted(used_docs)],
        evidence=list(evidence.values()), courses=list(courses.values()), offerings=offerings, policy_contexts=contexts)
    bundle = dict(schema_version='review-bundle-v1', status='needs_review',
        generated_at=datetime.now(timezone.utc).isoformat(),
        scope=dict(campus=campus, semester=semester, admission_year=admission_year),
        notes=['All extracted facts and proposed scope require review. Empty prerequisites mean unknown, not unrestricted.', *notes], snapshot=snapshot)
    write_json_atomic(bundle, output)
    return bundle


def save_review(bundle: dict, snapshot: dict, output: str | Path, reviewer: str) -> dict:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('courses'), list):
        raise SnapshotError('Review must contain a snapshot object with a courses list.')
    updated = {**bundle, 'snapshot': snapshot, 'reviewer': reviewer,
               'updated_at': datetime.now(timezone.utc).isoformat()}
    write_json_atomic(updated, output)
    return updated


def reviewed_subset(snapshot: dict) -> dict:
    """Select reviewed records without promoting any unreviewed evidence or fact."""
    result = copy.deepcopy(snapshot)
    verified = {item['evidence_id'] for item in result.get('evidence', []) if item.get('verification_status') == 'verified'}

    def supported(record: dict) -> bool:
        refs = set(record.get('evidence_ids', []))
        return bool(refs) and refs.issubset(verified)

    result['courses'] = [item for item in result.get('courses', []) if supported(item)]
    known = {item['course_id'] for item in result['courses']}

    def prerequisites_known(expression: Any) -> bool:
        if expression is None:
            return True
        if not isinstance(expression, dict):
            return False
        if expression.get('op') == 'completed':
            return expression.get('course_id') in known
        if expression.get('op') in ('all_of','any_of'):
            return isinstance(expression.get('conditions'),list) and all(prerequisites_known(child) for child in expression['conditions'])
        return True

    result['offerings'] = [item for item in result.get('offerings', [])
        if item.get('course_id') in known and supported(item) and prerequisites_known(item.get('prerequisite'))]
    contexts = []
    for context in result.get('policy_contexts', []):
        if not supported(context):
            continue
        if all(supported(req) and set(req.get('course_pool',[])+req.get('mandatory_course_ids',[])).issubset(known)
               for req in context.get('requirements', [])):
            contexts.append(context)
    result['policy_contexts'] = contexts
    result['equivalence_groups'] = [group for group in result.get('equivalence_groups', [])
        if supported(group) and set(group.get('course_ids', [])).issubset(known)]
    return result


def publish_review(bundle_path: str | Path, destination: str | Path, reviewer: str, *, reviewed_only: bool = True) -> dict:
    bundle = json.loads(Path(bundle_path).read_text(encoding='utf-8'))
    snapshot = reviewed_subset(bundle['snapshot']) if reviewed_only else bundle['snapshot']
    
    
    if snapshot.get('synthetic') or not snapshot.get('policy_contexts') or not snapshot.get('offerings'):
        raise SnapshotError('A real publication needs reviewed policy contexts, their required courses, and reviewed offerings. Confirm the cited evidence and course facts first.')
    path = Path(bundle_path).with_suffix('.candidate.json')
    write_json_atomic(snapshot, path)
    try:
        load_snapshot(path)
        destination = Path(destination)
        if destination.exists():
            history = destination.parent / 'history'
            old = json.loads(destination.read_text(encoding='utf-8'))
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()[:16]
            write_json_atomic(old, history / f'snapshot-{digest}.json')
        published = publish_snapshot(path, destination)
    finally:
        path.unlink(missing_ok=True)
    bundle.update(status='published', reviewer=reviewer, published_at=datetime.now(timezone.utc).isoformat())
    write_json_atomic(bundle, bundle_path)
    return {'dataset_version': published.dataset_version, 'courses': len(published.courses),
            'offerings': len(published.offerings), 'policy_contexts': len(published.policy_contexts)}
