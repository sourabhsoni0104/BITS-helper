"""Authenticated local-first web workbench for profiles and academic data."""
from __future__ import annotations

import csv
import email.policy
import hashlib
import html
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from recommender.ingestion.corpus import CorpusError, get_document, get_document_page
from recommender.ingestion.snapshots import SnapshotError, load_snapshot
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.services.academic_agent import AcademicAgent
from recommender.services.document_chat_agent import DocumentChatAgent
from recommender.services.groq_client import GroqClient
from recommender.services.marksheet_import import (
    MarksheetImportError,
    confirmed_attempts,
    preview_marksheet,
)
from recommender.services.staged_discovery_agent import StagedDiscoveryAgent
from recommender.storage.accounts import AccountError, AccountExistsError, AccountRepository
from recommender.storage.profiles import (
    ProfileConflictError,
    ProfileRepository,
    ProfileValidationError,
)


MAX_FORM_BYTES = 64 * 1024
MAX_MULTIPART_BYTES = 10 * 1024 * 1024 + 64 * 1024
PREVIEW_TTL_SECONDS = 20 * 60
GUEST_SESSION_SECONDS = 2 * 60 * 60
STATUSES = tuple(status.value for status in AttemptStatus)
CAMPUS_OPTIONS = (
    ("Pilani", "Pilani campus"),
    ("Goa", "K. K. Birla Goa campus"),
    ("Hyderabad", "Hyderabad campus"),
    ("Dubai", "Dubai campus"),
)
PROGRAMME_OPTIONS = (
    ("BE-CHEMICAL", "B.E. Chemical"),
    ("BE-CIVIL", "B.E. Civil"),
    ("BE-EEE", "B.E. Electrical and Electronics"),
    ("BE-MECHANICAL", "B.E. Mechanical"),
    ("BPHARM", "B. Pharm."),
    ("BE-CS", "B.E. Computer Science"),
    ("BE-ENI", "B.E. Electronics and Instrumentation"),
    ("BE-ECE", "B.E. Electronics and Communication"),
    ("BE-MANUFACTURING", "B.E. Manufacturing"),
    ("BE-ELECTRONICS-COMPUTER", "B.E. Electronics and Computer"),
    ("BE-MNC", "B.E. Mathematics and Computing"),
    ("BE-ENV", "B.E. Environmental and Sustainability Engineering"),
    ("MSC-BIO", "M.Sc. Biological Sciences"),
    ("MSC-CHEMISTRY", "M.Sc. Chemistry"),
    ("MSC-ECONOMICS", "M.Sc. Economics"),
    ("MSC-MATHEMATICS", "M.Sc. Mathematics"),
    ("MSC-PHYSICS", "M.Sc. Physics"),
    ("MSC-SEMICONDUCTOR", "M.Sc. Semiconductors and Nanoscience"),
)
MINOR_NAMES = (
    "Aeronautics", "Biomedical Engineering", "Computational Economics",
    "Computational Mechanics", "Computing and Intelligence", "Data Science",
    "Data Science in Climate & Health", "English Studies", "Entrepreneurship",
    "Film and Media", "Finance", "Management", "Materials Science and Engineering",
    "Nanoscience and Nanobiotechnology", "Philosophy, Economics and Politics (PEP)",
    "Physics", "Public Policy", "Quantum Information and Technologies",
    "Robotics and Automation", "Semiconductor Devices and Technology",
    "Supply Chain Analytics", "Water and Sanitation", "Tissue Engineering",
)
MINOR_OPTIONS = tuple(
    ("MINOR-" + re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-"), name)
    for name in MINOR_NAMES
)
STATUS_LABELS = {
    "completed": "Completed / passed",
    "in_progress": "Currently taking",
    "failed": "Not cleared / failed",
    "withdrawn": "Withdrawn",
    "unresolved": "Not sure yet",
}
BRANCH_CODE_TO_PROGRAMME = {
    "A1": "BE-CHEMICAL",
    "A2": "BE-CIVIL",
    "A3": "BE-EEE",
    "A4": "BE-MECHANICAL",
    "A5": "BPHARM",
    "A7": "BE-CS",
    "A8": "BE-ENI",
    "AA": "BE-ECE",
    "AB": "BE-MANUFACTURING",
    "AC": "BE-ELECTRONICS-COMPUTER",
    "AD": "BE-MNC",
    "AJ": "BE-ENV",
    "B1": "MSC-BIO",
    "B2": "MSC-CHEMISTRY",
    "B3": "MSC-ECONOMICS",
    "B4": "MSC-MATHEMATICS",
    "B5": "MSC-PHYSICS",
    "B7": "MSC-SEMICONDUCTOR",
}
CAMPUS_CODE_TO_CAMPUS = {"P": "Pilani", "G": "Goa", "H": "Hyderabad", "D": "Dubai"}
CSS = """
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#17212b;background:#f3f6f7}
*{box-sizing:border-box}body{margin:0}.shell{max-width:1100px;margin:auto;padding:28px 18px 60px}
nav{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:30px}nav a,.link{color:#086b58;text-decoration:none;font-weight:700}nav form{margin-left:auto}
h1{font-size:2.1rem;letter-spacing:-.04em;margin:.25rem 0 1rem}.lede,.muted{color:#52616d;line-height:1.55}
.panel,.card{background:white;border:1px solid #dce4e7;border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 6px 20px #142c3a0a}
label{display:block;font-weight:700;font-size:.86rem;margin:12px 0 5px}input,select,textarea{font:inherit;border:1px solid #b9c6cb;border-radius:8px;padding:9px;width:100%;background:#fff}textarea{min-height:110px}
button{font:inherit;font-weight:750;background:#08705a;color:white;border:0;border-radius:8px;padding:10px 15px;margin-top:14px;cursor:pointer}.secondary{background:#e7eff0;color:#17483f}
.error{background:#fff0ed;color:#8a261d;padding:12px;border-radius:8px}.ok{background:#e4f6ef;color:#175a43;padding:12px;border-radius:8px}.empty{border:1px dashed #b9c6cb;border-radius:10px;padding:18px;color:#52616d}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #e4eaec;min-width:95px}th{font-size:.8rem;color:#52616d}td input,td select{min-width:90px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f8;padding:14px;border-radius:8px}.pill{display:inline-block;border-radius:30px;background:#e8f3f1;color:#145748;padding:4px 9px;font-size:.8rem}
@media(max-width:650px){.shell{padding:18px 12px}.panel{padding:14px}}
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, body: str, session: dict | None = None) -> bytes:
    if session:
        links = '<a href="/">Profile</a><a href="/recommend">Recommendations</a><a href="/history">History</a><a href="/import">Upload transcript</a><a href="/documents">Chat</a>'
        if session.get("is_guest"):
            links += '<a href="/login">Switch to saved account</a>'
        signout_label = "Forget this session" if session.get("is_guest") else "Sign out"
        nav = f"""<nav><a href="/" style="font-size:1.15rem">BITSbuddy</a>{links}
        <form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><button class="secondary">{signout_label}</button></form></nav>"""
    else:
        nav = '<nav><a href="/" style="font-size:1.15rem">BITSbuddy</a></nav>'
    text = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(title)}</title><style>{CSS}</style></head><body><main class="shell">{nav}{body}</main></body></html>'
    return text.encode("utf-8")


def _field(label: str, name: str, value: object = "", kind: str = "text", required: bool = False) -> str:
    req = " required" if required else ""
    return f'<label for="{_esc(name)}">{_esc(label)}</label><input id="{_esc(name)}" name="{_esc(name)}" type="{_esc(kind)}" value="{_esc(value)}"{req}>'


def _select(label: str, name: str, options: tuple[tuple[str, str], ...], selected: str | None = None,
            *, required: bool = False, blank: str | None = "Choose…") -> str:
    rows = [] if blank is None else [f'<option value="">{_esc(blank)}</option>']
    known = {value for value, _ in options}
    if selected and selected not in known:
        rows.append(f'<option value="{_esc(selected)}" selected>{_esc(selected.replace("-", " ").title())}</option>')
    for value, text in options:
        rows.append(f'<option value="{_esc(value)}"{" selected" if value == selected else ""}>{_esc(text)}</option>')
    req = " required" if required else ""
    return f'<label for="{_esc(name)}">{_esc(label)}</label><select id="{_esc(name)}" name="{_esc(name)}"{req}>{"".join(rows)}</select>'


def _term_options(*, history: bool = False, admission_year: int | None = None) -> tuple[tuple[str, str], ...]:
    year = time.gmtime().tm_year
    values = []
    start_year = max(year - 10, admission_year) if history and admission_year else (year - 10 if history else year - 1)
    for start in range(start_year, year + 2):
        academic = f"{start}–{str(start + 1)[-2:]}"
        values.extend(((f"{start}-T1", f"{academic}, Semester 1"),
                       (f"{start}-T2", f"{academic}, Semester 2"),
                       (f"{start}-SUMMER", f"{academic}, Summer term")))
    return tuple(values)


def _planning_term_options(*, current_year: int | None = None,
                           current_month: int | None = None) -> tuple[tuple[str, str], ...]:
    """Return only the current and upcoming terms, never completed terms."""
    now = time.localtime()
    year = current_year if current_year is not None else now.tm_year
    month = current_month if current_month is not None else now.tm_mon
    if not 1 <= month <= 12:
        raise ValueError("Current month must be between 1 and 12.")
    if month <= 5:
        first_year, first_part = year - 1, "T2"
    elif month <= 7:
        first_year, first_part = year - 1, "SUMMER"
    else:
        first_year, first_part = year, "T1"
    order = {"T1": 0, "T2": 1, "SUMMER": 2}
    values = []
    for start in range(first_year, first_year + 3):
        academic = f"{start}–{str(start + 1)[-2:]}"
        for part, label in (("T1", "Semester 1"), ("T2", "Semester 2"), ("SUMMER", "Summer term")):
            if start == first_year and order[part] < order[first_part]:
                continue
            values.append((f"{start}-{part}", f"{academic}, {label}"))
    return tuple(values)


def _friendly_term(value: str) -> str:
    for option, label in _term_options(history=True):
        if option == value:
            return label
    match = re.fullmatch(r"(FIRST|SECOND|SUMMER) SEMESTER (\d{4})[-/](\d{4})", value, re.IGNORECASE)
    if match:
        part = {"FIRST": "Semester 1", "SECOND": "Semester 2", "SUMMER": "Summer term"}[match.group(1).upper()]
        return f"{match.group(2)}–{match.group(3)[-2:]}, {part}"
    return value


def _transcript_profile_hints(student_id: str | None) -> dict[str, object]:
    """Decode YYYY + one/two branch codes + PS + four digits + campus code."""
    normalized = re.sub(r"\s+", "", student_id or "").upper()
    match = re.fullmatch(r"(?P<year>20\d{2})(?P<branches>[A-Z0-9]{2}(?:[A-Z0-9]{2})?)PS\d{4}(?P<campus>[PGHD])", normalized)
    if not match:
        return {}
    branch_text = match.group("branches")
    branch_codes = tuple(branch_text[index:index + 2] for index in range(0, len(branch_text), 2))
    programmes = tuple(BRANCH_CODE_TO_PROGRAMME[code] for code in branch_codes if code in BRANCH_CODE_TO_PROGRAMME)
    return {
        "admission_year": int(match.group("year")),
        "programme_ids": programmes,
        "campus": CAMPUS_CODE_TO_CAMPUS[match.group("campus")],
        "branch_codes": branch_codes,
    }


def _textarea(label: str, name: str, value: object = "", rows: int = 6) -> str:
    return f'<label for="{_esc(name)}">{_esc(label)}</label><textarea id="{_esc(name)}" name="{_esc(name)}" rows="{rows}">{_esc(value)}</textarea>'


def _status_select(name: str, selected: str | None) -> str:
    options = ['<option value="">Choose status…</option>']
    options += [f'<option value="{s}"{" selected" if s == selected else ""}>{_esc(STATUS_LABELS.get(s, s.replace("_", " ")))}</option>' for s in STATUSES]
    return f'<select name="{_esc(name)}">{"".join(options)}</select>'


def _profile_attempt_text(profile: StudentProfile | None) -> str:
    if profile is None:
        return ""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    for attempt in profile.attempts:
        writer.writerow([
            attempt.course_id,
            attempt.status.value,
            attempt.grade or "",
            "" if attempt.units_awarded is None else str(attempt.units_awarded),
            attempt.term_id or "",
            attempt.attempt_id or "",
            "" if attempt.attempt_order is None else str(attempt.attempt_order),
        ])
    return buffer.getvalue().rstrip("\n")


def _parse_attempt_text(text: str) -> tuple[CourseAttempt, ...]:
    attempts: list[CourseAttempt] = []
    try:
        rows = csv.reader(text.splitlines())
        for index, row in enumerate(rows):
            if not row or not any(value.strip() for value in row):
                continue
            if len(row) > 7 or len(row) < 2:
                raise ValueError(f"Attempt row {index + 1} needs 2–7 CSV columns.")
            values = row + [""] * (7 - len(row))
            course, status, grade, units, term, attempt_id, order = (value.strip() for value in values)
            if not course:
                raise ValueError(f"Attempt row {index + 1} needs a course ID.")
            if status not in STATUSES:
                raise ValueError(f"Attempt row {index + 1} needs one of: {', '.join(STATUSES)}.")
            try:
                units_value = float(units) if units else None
            except ValueError as exc:
                raise ValueError(f"Attempt row {index + 1} has invalid units.") from exc
            try:
                order_value = int(order) if order else None
            except ValueError as exc:
                raise ValueError(f"Attempt row {index + 1} has an invalid chronological order.") from exc
            if order_value is not None and order_value < 0:
                raise ValueError(f"Attempt row {index + 1} has an invalid chronological order.")
            if len(attempt_id) > 160:
                raise ValueError(f"Attempt row {index + 1} has an attempt ID that is too long.")
            attempts.append(CourseAttempt(
                course_id=course,
                status=AttemptStatus(status),
                grade=grade or None,
                units_awarded=units_value,
                attempt_id=attempt_id or None,
                term_id=term or None,
                attempt_order=order_value,
            ))
    except csv.Error as exc:
        raise ValueError("Could not parse the attempt rows.") from exc
    return tuple(attempts)


def _term_order(term: str | None) -> int | None:
    if not term:
        return None
    match = re.fullmatch(r"\s*(\d{4})-(?:T)?([12])\s*", term, re.IGNORECASE)
    if match:
        return int(match.group(1)) * 3 + int(match.group(2))
    match = re.fullmatch(r"\s*(\d{4})-(?:SUMMER|S)\s*", term, re.IGNORECASE)
    return int(match.group(1)) * 3 + 3 if match else None


def _attempt_row(row_id: str, attempt: CourseAttempt | None = None,
                 catalog: tuple[tuple[str, str, str], ...] = (), admission_year: int | None = None) -> str:
    codes_by_id = {course_id: code for course_id, code, _ in catalog}
    course = codes_by_id.get(attempt.course_id, attempt.course_id) if attempt else ""
    status = attempt.status.value if attempt else ""
    grade = attempt.grade if attempt and attempt.grade else ""
    units = "" if not attempt or attempt.units_awarded is None else str(attempt.units_awarded)
    term = attempt.term_id if attempt and attempt.term_id else ""
    attempt_id = attempt.attempt_id if attempt and attempt.attempt_id else ""
    order = "" if not attempt or attempt.attempt_order is None else str(attempt.attempt_order)
    term_options = ['<option value="">Choose term (optional)</option>']
    history_terms = _term_options(history=True, admission_year=admission_year)
    known_terms = {value for value, _ in history_terms}
    if term and term not in known_terms:
        term_options.append(f'<option value="{_esc(term)}" selected>{_esc(_friendly_term(term))}</option>')
    term_options.extend(
        f'<option value="{_esc(value)}" data-start-year="{_esc(value[:4])}"{" selected" if value == term else ""}>{_esc(label)}</option>'
        for value, label in history_terms
    )
    return f'''<tr data-attempt-row><td><input type="hidden" name="attempt_row" value="{_esc(row_id)}"><input type="hidden" name="attempt_id_{_esc(row_id)}" value="{_esc(attempt_id)}"><input type="hidden" name="attempt_order_{_esc(row_id)}" value="{_esc(order)}"><input name="attempt_course_{_esc(row_id)}" value="{_esc(course)}" list="course-catalog" placeholder="Search code or course name" aria-label="Course code or name"></td>
    <td>{_status_select(f'attempt_status_{row_id}', status)}</td>
    <td><input name="attempt_grade_{_esc(row_id)}" value="{_esc(grade)}" placeholder="Optional" aria-label="Grade"></td>
    <td><input name="attempt_units_{_esc(row_id)}" value="{_esc(units)}" type="number" min="0" max="25" step="0.5" placeholder="Optional" aria-label="Units"></td>
    <td><select class="history-term" name="attempt_term_{_esc(row_id)}" aria-label="Academic term">{"".join(term_options)}</select></td>
    <td><button class="secondary delete-course" type="button">Delete course</button></td></tr>'''


def _attempts_from_preview(review: dict | None) -> tuple[CourseAttempt, ...]:
    attempts = []
    for item in (review or {}).get("attempt_candidates", []):
        status = item.get("status")
        course_id = item.get("course_id") or item.get("course_code") or item.get("canonical_course_code")
        if not item.get("selected") or status not in STATUSES or not isinstance(course_id, str):
            continue
        attempts.append(CourseAttempt(
            course_id=course_id,
            status=AttemptStatus(status),
            grade=item.get("grade") or None,
            units_awarded=item.get("units"),
            attempt_id=item.get("attempt_id"),
            term_id=item.get("term") or None,
            attempt_order=item.get("attempt_order"),
        ))
    return tuple(attempts)


def _attempt_editor(profile: StudentProfile | None, transcript_attempts: tuple[CourseAttempt, ...] = (),
                    catalog: tuple[tuple[str, str, str], ...] = (), admission_year: int | None = None) -> str:
    attempts = list(profile.attempts if profile else ())
    existing_ids = {item.attempt_id for item in attempts if item.attempt_id}
    existing_values = {
        (item.course_id, item.status, item.grade, item.units_awarded, item.term_id, item.attempt_order)
        for item in attempts
    }
    attempts.extend(
        item for item in transcript_attempts
        if (not item.attempt_id or item.attempt_id not in existing_ids)
        and (item.course_id, item.status, item.grade, item.units_awarded, item.term_id, item.attempt_order) not in existing_values
    )
    rows = [_attempt_row(str(index), attempt, catalog, admission_year) for index, attempt in enumerate(attempts)]
    if not rows:
        rows.append(_attempt_row("0", catalog=catalog, admission_year=admission_year))
    template = _attempt_row("__ROW__", catalog=catalog, admission_year=admission_year)
    options = "".join(
        f'<option value="{_esc(code)}">{_esc(title)}</option><option value="{_esc(title)}">{_esc(code)}</option>'
        for _, code, title in catalog
    )
    return f'''<label>Course history</label><p class="muted">Add courses you completed, are taking, withdrew from, or did not clear. Leave optional details blank if you do not know them.</p>
    <datalist id="course-catalog">{options}</datalist>
    <table><thead><tr><th>Course</th><th>Result</th><th>Grade</th><th>Units</th><th>Academic term</th><th></th></tr></thead><tbody id="attempt-rows">{"".join(rows)}</tbody></table>
    <button class="secondary" type="button" id="add-attempt">Add another course</button>
    <template id="attempt-template">{template}</template>
    <script>(function(){{const button=document.getElementById('add-attempt'),body=document.getElementById('attempt-rows'),template=document.getElementById('attempt-template'),admission=document.getElementById('admission_year');let next={len(rows)};function filterTerms(root=document){{const year=parseInt(admission&&admission.value,10);root.querySelectorAll('.history-term option[data-start-year]').forEach(function(option){{const old=Number.isFinite(year)&&parseInt(option.dataset.startYear,10)<year;option.hidden=old;option.disabled=old;if(old&&option.selected)option.parentElement.value='';}});}}function bind(row){{const del=row.querySelector('.delete-course');if(del)del.addEventListener('click',function(){{row.remove();}});filterTerms(row);}}body.querySelectorAll('[data-attempt-row]').forEach(bind);button.addEventListener('click',function(){{const id='new-'+(next++),wrap=document.createElement('tbody');wrap.innerHTML=template.innerHTML.replaceAll('__ROW__',id);const row=wrap.firstElementChild;body.appendChild(row);bind(row);}});if(admission)admission.addEventListener('input',function(){{filterTerms();}});filterTerms();}})();</script>'''


def _parse_attempt_form(data: dict[str, list[str]], catalog: tuple[tuple[str, str, str], ...] = ()) -> tuple[CourseAttempt, ...]:
    row_ids = data.get("attempt_row", [])
    if len(row_ids) > 60 or len(set(row_ids)) != len(row_ids):
        raise ValueError("Course-history rows are invalid; reload and try again.")
    by_code = {code.casefold(): course_id for course_id, code, _ in catalog}
    title_groups: dict[str, list[str]] = {}
    for course_id, _, title in catalog:
        title_groups.setdefault(title.casefold(), []).append(course_id)
    by_title = {title: ids[0] for title, ids in title_groups.items() if len(ids) == 1}
    attempts: list[CourseAttempt] = []
    for row_id in row_ids:
        if not re.fullmatch(r"(?:\d+|new-\d+)", row_id):
            raise ValueError("Course-history rows are invalid; reload and try again.")
        value = lambda field: data.get(f"attempt_{field}_{row_id}", [""])[0].strip()
        entered_course, status = value("course"), value("status")
        if entered_course.casefold() in title_groups and entered_course.casefold() not in by_title:
            raise ValueError(f'More than one course is named "{entered_course}". Choose it by course code.')
        course = by_code.get(entered_course.casefold()) or by_title.get(entered_course.casefold()) or entered_course.upper()
        grade, units, term = value("grade"), value("units"), value("term")
        if not any((course, status, grade, units, term)):
            continue
        if not course:
            raise ValueError("Every course-history row needs a course code.")
        if status not in STATUSES:
            raise ValueError(f"Choose what happened in {course}.")
        try:
            units_value = float(units) if units else None
        except ValueError as exc:
            raise ValueError(f"Enter valid units for {course}, or leave them blank.") from exc
        if units_value is not None and not 0 <= units_value <= 25:
            raise ValueError(f"Units for {course} must be between 0 and 25.")
        order_text = value("order")
        try:
            order = int(order_text) if order_text else _term_order(term)
        except ValueError as exc:
            raise ValueError(f"The saved attempt order for {course} is invalid.") from exc
        attempts.append(CourseAttempt(
            course_id=course,
            status=AttemptStatus(status),
            grade=grade.upper() or None,
            units_awarded=units_value,
            attempt_id=value("id") or "manual-" + secrets.token_urlsafe(10),
            term_id=term or None,
            attempt_order=order,
        ))
    return tuple(attempts)


def make_workbench_handler(
    *, snapshot_path: str | Path, corpus_path: str | Path, profile_db: str | Path,
    account_db: str | Path, review_path: str | Path, index_path: str | Path,
):
    """Build a BaseHTTPRequestHandler class; call its close_resources on shutdown."""
    snapshot_path, corpus_path = Path(snapshot_path), Path(corpus_path)
    review_path, index_path = Path(review_path), Path(index_path)
    profiles = ProfileRepository(profile_db)
    guest_profiles = ProfileRepository(":memory:")
    accounts = AccountRepository(account_db)
    previews: dict[str, tuple[float, dict]] = {}
    previews_lock = threading.RLock()
    guest_sessions: dict[str, dict] = {}
    guest_lock = threading.RLock()
    chat_threads: dict[str, list[dict]] = {}
    chat_lock = threading.RLock()
    guest_recommendations: dict[str, list[dict]] = {}
    recommendation_lock = threading.RLock()
    source_root = (corpus_path.parent.parent / "raw").resolve()
    groq_client = GroqClient.from_environment()
    academic_agent = AcademicAgent(groq_client, corpus_path) if groq_client is not None else None
    staged_agent = StagedDiscoveryAgent(groq_client, corpus_path) if groq_client is not None else None
    document_chat_agent = DocumentChatAgent(groq_client, corpus_path) if groq_client is not None else None

    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def discard_guest(token: str | None) -> None:
        if not isinstance(token, str) or not token:
            return
        with guest_lock:
            session = guest_sessions.pop(token_hash(token), None)
        if session:
            guest_profiles.delete(session["profile_id"])
        with previews_lock:
            previews.pop(token_hash(token), None)
        with chat_lock:
            chat_threads.pop(token_hash(token), None)
        with recommendation_lock:
            guest_recommendations.pop(token_hash(token), None)

    def guest_session(token: str | None) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        now = time.time()
        expired: list[tuple[str, dict]] = []
        with guest_lock:
            for key, value in tuple(guest_sessions.items()):
                if value["expires_at"] <= now:
                    expired.append((key, guest_sessions.pop(key)))
            session = guest_sessions.get(token_hash(token))
        for key, value in expired:
            guest_profiles.delete(value["profile_id"])
            with previews_lock:
                previews.pop(key, None)
            with chat_lock:
                chat_threads.pop(key, None)
            with recommendation_lock:
                guest_recommendations.pop(key, None)
        if not session or session["expires_at"] <= now:
            return None
        return dict(session)

    def profile_repository(session: dict) -> ProfileRepository:
        return guest_profiles if session.get("is_guest") else profiles

    def owns_profile(session: dict, profile_id: str) -> bool:
        if session.get("is_guest"):
            return hmac.compare_digest(session["profile_id"], profile_id)
        return accounts.owns_profile(session["user_id"], profile_id)

    def active_snapshot():
        try:
            return load_snapshot(snapshot_path)
        except (OSError, SnapshotError):
            return None

    def staged_snapshot() -> dict | None:
        if not review_path.is_file():
            return None
        try:
            bundle = json.loads(review_path.read_text(encoding="utf-8"))
            snapshot = bundle.get("snapshot")
            return snapshot if isinstance(snapshot, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def course_catalog() -> tuple[tuple[str, str, str], ...]:
        """Return (course_id, visible code, title) from active or staged data."""
        snapshot = active_snapshot()
        if snapshot:
            return tuple(sorted(
                ((course.course_id, course.code, course.title) for course in snapshot.courses.values()),
                key=lambda item: (item[1], item[2]),
            ))
        try:
            rows = (staged_snapshot() or {}).get("courses", [])
            values = {
                (str(item["course_id"]), str(item.get("code") or item["course_id"]), str(item.get("title") or item.get("code") or item["course_id"]))
                for item in rows if isinstance(item, dict) and item.get("course_id")
            }
            return tuple(sorted(values, key=lambda item: (item[1], item[2])))
        except (ValueError, TypeError, KeyError):
            return ()

    class Handler(BaseHTTPRequestHandler):
        server_version = "BITSbuddy/1.0"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _render(self, title: str, body: str, session: dict | None = None, status: int = 200) -> None:
            self._send(_page(title, body, session), status)

        def _cookie_token(self) -> str | None:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                return cookie["workbench_session"].value if "workbench_session" in cookie else None
            except Exception:
                return None

        def _session(self) -> dict | None:
            token = self._cookie_token()
            account = accounts.get_session(token)
            if account:
                return {**account, "is_guest": False}
            return guest_session(token)

        def _current_preview(self) -> dict | None:
            token = self._cookie_token()
            key = token_hash(token) if token else ""
            with previews_lock:
                now = time.time()
                for expired_key, value in tuple(previews.items()):
                    if value[0] < now:
                        previews.pop(expired_key, None)
                stored = previews.get(key)
            return stored[1] if stored else None

        def _read_body(self, limit: int) -> bytes:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise ValueError("Invalid request length.")
            length = int(raw_length)
            if length <= 0:
                raise ValueError("Request body is empty.")
            if length > limit:
                raise OverflowError("Request body is too large.")
            return self.rfile.read(length)

        def _form(self, *, limit: int = MAX_FORM_BYTES) -> dict[str, list[str]]:
            if self.headers.get("Content-Type", "").split(";", 1)[0].casefold() != "application/x-www-form-urlencoded":
                raise TypeError("Expected an HTML form submission.")
            return parse_qs(self._read_body(limit).decode("utf-8", "strict"), keep_blank_values=True, max_num_fields=5000)

        def _json_body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";", 1)[0].casefold() != "application/json":
                raise TypeError("Expected JSON content.")
            value = json.loads(self._read_body(MAX_FORM_BYTES))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            return value

        @staticmethod
        def _value(data: dict[str, list[str]], key: str, default: str = "") -> str:
            return data.get(key, [default])[0]

        def _require_auth(self) -> dict | None:
            session = self._session()
            if not session:
                self._render("Sign in", self._login_form(), None, 401)
            return session

        def _check_csrf(self, session: dict, data: dict[str, list[str]]) -> bool:
            supplied = self.headers.get("X-CSRF-Token") or self._value(data, "csrf_token")
            if session.get("is_guest"):
                return isinstance(supplied, str) and hmac.compare_digest(session["csrf_token"], supplied)
            return accounts.verify_csrf(self._cookie_token(), supplied)

        @staticmethod
        def _login_form(error: str = "", guest_active: bool = False) -> str:
            alert = f'<p class="error">{_esc(error)}</p>' if error else ""
            guest_notice = '<p class="error">Signing in starts your saved account profile. For privacy, this guest profile is not copied and will be forgotten.</p>' if guest_active else ""
            return f'''<h1>Sign in</h1><p class="lede">Your study profile and saved academic history are private to your account.</p>{alert}
            {guest_notice}
            <section class="panel"><form method="post" action="/login">{_field('Email','email','',required=True)}{_field('Password','password','',kind='password',required=True)}<button>Sign in</button></form></section>
            <section class="panel"><h2>Use without an account</h2><p class="muted">Guest data stays only in this running app session and is deleted when you choose “Forget this session”, the session expires, or the server restarts.</p><form method="post" action="/guest"><button>Continue as guest</button></form></section>
            <section class="panel"><h2>Create an optional account</h2><p class="muted">Use an account only if you want your profile saved for later visits.</p><form method="post" action="/register">{_field('Email','email','',required=True)}{_field('Password (at least 8 characters)','password','',kind='password',required=True)}<button class="secondary">Create account</button></form></section>'''

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            session = self._session()
            if parsed.path in {"/login", "/register"}:
                self._render("Sign in", self._login_form(guest_active=bool(session and session.get("is_guest"))), session)
                return
            if not session:
                self._render("Sign in", self._login_form(), None)
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if parsed.path == "/":
                    self._profile_page(session, query)
                elif parsed.path == "/recommend":
                    self._recommend_page(session, query)
                elif parsed.path == "/import":
                    self._import_page(session)
                elif parsed.path == "/documents":
                    self._documents_page(session, query)
                elif parsed.path == "/history":
                    self._history_page(session)
                elif parsed.path.startswith("/sources/"):
                    self._source_page(session, parsed.path.removeprefix("/sources/"), query)
                else:
                    self._render("Not found", '<div class="empty">Page not found.</div>', session, 404)
            except PermissionError as exc:
                self._render("Access denied", f'<p class="error">{_esc(exc)}</p>', session, 403)
            except (CorpusError, OSError, ValueError) as exc:
                self._render("Workbench", f'<p class="error">{_esc(exc)}</p>', session, 400)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/guest":
                self._start_guest()
                return
            if path in {"/login", "/register"}:
                self._login_or_register(path)
                return
            session = self._require_auth()
            if not session:
                return
            if path == "/import" and self.headers.get("Content-Type", "").split(";", 1)[0].casefold() == "multipart/form-data":
                self._import_post(session)
                return
            try:
                data = self._form()
            except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
                status = 413 if isinstance(exc, OverflowError) else 400
                self._render("Invalid request", f'<p class="error">{_esc(exc)}</p>', session, status)
                return
            if not self._check_csrf(session, data):
                self._render("Request expired", '<p class="error">Your form token is invalid or expired. Reload and try again.</p>', session, 403)
                return
            try:
                if path == "/logout":
                    if session.get("is_guest"):
                        discard_guest(self._cookie_token())
                    else:
                        token = self._cookie_token()
                        accounts.logout(token)
                        if token:
                            with previews_lock:
                                previews.pop(token_hash(token), None)
                            with chat_lock:
                                chat_threads.pop(token_hash(token), None)
                    self._send(_page("Signed out", '<p class="ok">You are signed out.</p><p><a href="/login">Sign in</a></p>'), extra={"Set-Cookie": "workbench_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
                elif path == "/":
                    self._save_profile(session, data)
                elif path == "/recommend":
                    self._recommend_page(session, {"q": [self._value(data, "q")], "record": ["1"]})
                elif path == "/import":
                    self._confirm_import(session, data)
                elif path == "/documents":
                    self._documents_page(session, {"q": [self._value(data, "q")], "clear": [self._value(data, "clear")]})
                elif path == "/history" and self._value(data, "clear") == "1":
                    self._clear_history(session)
                    self._history_page(session)
                else:
                    self._render("Not found", '<div class="empty">Page not found.</div>', session, 404)
            except PermissionError as exc:
                self._render("Access denied", f'<p class="error">{_esc(exc)}</p>', session, 403)
            except (AccountError, ProfileConflictError, ProfileValidationError, SnapshotError, CorpusError, MarksheetImportError, ValueError, OSError, json.JSONDecodeError) as exc:
                self._render("Could not save", f'<p class="error">{_esc(exc)}</p>', session, 400)

        def _start_guest(self) -> None:
            previous = self._cookie_token()
            accounts.logout(previous)
            discard_guest(previous)
            token = secrets.token_urlsafe(32)
            session = {
                "user_id": "guest",
                "profile_id": "guest-" + secrets.token_urlsafe(24),
                "role": "guest",
                "csrf_token": secrets.token_urlsafe(24),
                "expires_at": time.time() + GUEST_SESSION_SECONDS,
                "is_guest": True,
            }
            with guest_lock:
                guest_sessions[token_hash(token)] = session
            self.send_response(303)
            self.send_header("Location", "/")
            
            self.send_header("Set-Cookie", f"workbench_session={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _login_or_register(self, path: str) -> None:
            try:
                data = self._form()
                email_value, password = self._value(data, "email"), self._value(data, "password")
                if path == "/register":
                    accounts.register(email_value, password)
                token, _ = accounts.login(email_value, password)
            except (AccountExistsError, AccountError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
                current = self._session()
                self._render("Sign in", self._login_form(str(exc), bool(current and current.get("is_guest"))), current, 400)
                return
            discard_guest(self._cookie_token())
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"workbench_session={token}; Path=/; Max-Age=1209600; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _profile_page(self, session: dict, query: dict[str, list[str]]) -> None:
            repository = profile_repository(session)
            profile = repository.get(session["profile_id"])
            if profile is not None and not owns_profile(session, profile.profile_id):
                raise PermissionError("This profile does not belong to the current session.")
            review = self._current_preview()
            transcript_attempts = _attempts_from_preview(review)
            metadata = (review or {}).get("profile_candidates", {})
            student_id = str(metadata.get("student_id") or "")
            hints = _transcript_profile_hints(student_id)
            admission_value = profile.admission_year if profile else hints.get("admission_year", "")
            programmes = profile.programme_ids if profile else tuple(hints.get("programme_ids", ()))
            primary = programmes[0] if programmes else ""
            secondary = programmes[1] if len(programmes) > 1 else ""
            campus_value = profile.campus if profile else str(hints.get("campus") or "Pilani")
            transcript_terms = {item.term_id for item in transcript_attempts if item.term_id}
            current_semester_value = profile.current_semester if profile else max(1, len(transcript_terms))
            if review:
                identity = " · ".join(filter(None, (metadata.get("student_name"), student_id, f'CGPA {metadata.get("cgpa")}' if metadata.get("cgpa") else None)))
                programme_labels = dict(PROGRAMME_OPTIONS)
                detected = " + ".join(programme_labels.get(item, item) for item in programmes)
                detected_profile = " · ".join(filter(None, (campus_value + " campus" if hints else None, detected or None, f"joined {admission_value}" if admission_value else None)))
                skipped = len(review.get("attempt_candidates", [])) - len(transcript_attempts)
                saved_text = " and saved to your profile" if not session.get("is_guest") else ""
                transcript_note = f'<p class="ok">Loaded {_esc(review.get("filename"))}: {len(transcript_attempts)} course records are filled below{saved_text}{f"; {skipped} uncertain row(s) need manual attention" if skipped else ""}. {_esc(identity)}{f" · Detected: { _esc(detected_profile) }" if detected_profile else ""}</p>'
            else:
                transcript_note = '<p class="muted">Upload your latest performance sheet and the complete recognized course history will be filled into this profile automatically.</p>'
            transcript_upload = f'''<section class="panel"><h2>Upload latest transcript</h2>{transcript_note}<form method="post" action="/import" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="action" value="preview"><input type="file" name="file" accept="application/pdf,.pdf" required><button>{'Replace transcript' if review or profile else 'Upload and fill my course history'}</button></form></section>'''
            version = profile.profile_version if profile else 0
            catalog = course_catalog()
            admission_number = int(admission_value) if str(admission_value).isdigit() else None
            planning_terms = _planning_term_options()
            planning_ids = {value for value, _ in planning_terms}
            saved_target = profile.target_semester_id if profile else ""
            target_value = saved_target if saved_target in planning_ids else planning_terms[0][0]
            details = f'''<section class="panel"><form method="post" action="/">
            <input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="profile_version" value="{version}">
            {_select('Campus','campus',CAMPUS_OPTIONS,campus_value,required=True,blank=None)}
            {_field('Year you joined BITS','admission_year',admission_value,kind='number',required=True)}
            {_select('Degree / programme','primary_programme',PROGRAMME_OPTIONS,primary,required=True)}
            {_select('Second degree (only for dual-degree students)','secondary_programme',PROGRAMME_OPTIONS,secondary,blank='No second degree')}
            {_field('Which semester are you currently in?','current_semester',current_semester_value,kind='number',required=True)}
            {_select('Semester you are planning for','target_semester',planning_terms,target_value,required=True,blank=None)}
            {_select('Minor (optional)','minor',MINOR_OPTIONS,profile.minor_id if profile and profile.minor_id else '',blank='No minor / not enrolled in one')}
            {_attempt_editor(profile, transcript_attempts, catalog, admission_number)}
            <button>Continue to course preferences</button></form></section>'''
            body = f'<h1>Your study profile</h1>{transcript_upload}{details if review or profile else ""}'
            self._render("Your profile", body, session)

        def _recommend_page(self, session: dict, query: dict[str, list[str]]) -> None:
            profile = profile_repository(session).get(session["profile_id"])
            if profile is None:
                self._render(
                    "Course preferences",
                    '<h1>Course preferences</h1><div class="empty">Complete your study profile first.</div><p><a class="link" href="/">Go to profile</a></p>',
                    session,
                )
                return
            q = query.get("q", [""])[0].strip()
            saved = query.get("saved", [""])[0] == "1"
            should_record = query.get("record", [""])[0] == "1"
            saved_notice = '<p class="ok">Profile saved.</p>' if saved else ""
            body = f'''<h1>Course preferences</h1><p class="lede">Step 2 of 2 · Tell us what you want from your next courses.</p>{saved_notice}
            <form class="panel" method="post" action="/recommend"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}">
            {_textarea('What kind of courses are you looking for?','q',q,4)}
            <button>Get recommendations</button></form>'''
            snapshot = active_snapshot()
            history_result = None
            if q and snapshot:
                if academic_agent is None:
                    body += '<p class="error">AI recommendations are not configured. Add a Groq API key and try again.</p>'
                else:
                    try:
                        result = academic_agent.run(profile, q, snapshot)
                        body += self._recommendations(result)
                        history_result = result
                    except Exception:
                        body += '<p class="error">The AI recommendation service is temporarily busy. Try again in a moment.</p>'
            elif q:
                staged = staged_snapshot()
                if staged:
                    if staged_agent is None:
                        body += '<p class="error">AI recommendations are not configured. Add a Groq API key and try again.</p>'
                    else:
                        try:
                            result = staged_agent.run(profile, q, staged)
                            body += self._recommendations(result)
                            history_result = result
                        except Exception:
                            body += '<p class="error">The AI recommendation service is temporarily busy. Try again in a moment.</p>'
                else:
                    body += '<section class="panel"><h2>Recommendations</h2><p class="muted">Upload or index course documents before requesting recommendations.</p></section>'
            if should_record and q and history_result is not None:
                self._record_recommendation(session, q, history_result)
            self._render("Course preferences", body, session)

        @staticmethod
        def _recommendations(result: dict) -> str:
            cards = []
            for item in result["recommendations"]:
                reference_count = len(item.get("evidence_references", []))
                source_note = item.get("source_note") or ("Based on the supplied academic documents." if reference_count else "Course information is limited.")
                matched = ", ".join(item.get("matched_topics", [])) or "Matches your academic requirements"
                badge = item.get("badge", "Eligible")
                cards.append(f'<article class="card"><span class="pill">{_esc(badge)}</span><h3>{_esc(item["course_code"])} — {_esc(item["title"])}</h3><p>{_esc(matched)}</p><p class="muted">{_esc(source_note)}</p></article>')
            if not cards:
                reason = result.get("no_result_reason") or "No recommendations found for this request."
                cards.append(f'<div class="empty">{_esc(reason)}</div>')
            return '<section class="panel"><h2>Recommendations</h2>' + ''.join(cards) + '</section>'

        @staticmethod
        def _history_entry(query: str, result: dict) -> dict:
            recommendations = []
            for item in result.get("recommendations", [])[:20]:
                recommendations.append({
                    "course_code": str(item.get("course_code") or ""),
                    "title": str(item.get("title") or ""),
                    "reason": ", ".join(str(value) for value in item.get("matched_topics", []) if value),
                })
            return {
                "query": query,
                "recommendations": recommendations,
                "no_result_reason": str(result.get("no_result_reason") or ""),
            }

        def _record_recommendation(self, session: dict, query: str, result: dict) -> None:
            entry = self._history_entry(query, result)
            if session.get("is_guest"):
                token = self._cookie_token()
                key = token_hash(token) if token else ""
                entry["created_at"] = time.time()
                with recommendation_lock:
                    guest_recommendations[key] = [entry, *guest_recommendations.get(key, [])][:50]
            else:
                accounts.add_recommendation(session["user_id"], entry)

        def _history_records(self, session: dict) -> list[dict]:
            if not session.get("is_guest"):
                return accounts.recommendation_history(session["user_id"])
            token = self._cookie_token()
            key = token_hash(token) if token else ""
            with recommendation_lock:
                return list(guest_recommendations.get(key, []))

        def _clear_history(self, session: dict) -> None:
            if not session.get("is_guest"):
                accounts.clear_recommendation_history(session["user_id"])
                return
            token = self._cookie_token()
            key = token_hash(token) if token else ""
            with recommendation_lock:
                guest_recommendations.pop(key, None)

        def _history_page(self, session: dict) -> None:
            entries = self._history_records(session)
            cards = []
            for entry in entries:
                timestamp = time.strftime("%d %b %Y, %I:%M %p", time.localtime(float(entry.get("created_at") or 0)))
                courses = []
                for item in entry.get("recommendations", []):
                    reason = f'<br><span class="muted">{_esc(item.get("reason"))}</span>' if item.get("reason") else ""
                    courses.append(f'<li><strong>{_esc(item.get("course_code"))} — {_esc(item.get("title"))}</strong>{reason}</li>')
                result = f'<ul>{"".join(courses)}</ul>' if courses else f'<p class="muted">{_esc(entry.get("no_result_reason") or "No recommendations were found.")}</p>'
                rerun = f'''<form method="post" action="/recommend"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="q" value="{_esc(entry.get('query'))}"><button class="secondary">Run again</button></form>'''
                cards.append(f'<article class="panel"><p class="muted">{_esc(timestamp)}</p><h2>{_esc(entry.get("query"))}</h2>{result}{rerun}</article>')
            if cards:
                clear = f'''<form method="post" action="/history"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="clear" value="1"><button class="secondary">Clear history</button></form>'''
                body = "".join(cards) + clear
            else:
                body = '<div class="empty">Your past recommendations will appear here.</div>'
            self._render("Recommendation history", f'<h1>Recommendation history</h1>{body}', session)

        def _save_profile(self, session: dict, data: dict[str, list[str]]) -> None:
            profile_id = session["profile_id"]
            if not owns_profile(session, profile_id):
                raise PermissionError("This profile does not belong to the current session.")
            repository = profile_repository(session)
            existing = repository.get(profile_id)
            version_text = self._value(data, "profile_version", "0")
            if not version_text.isdecimal():
                raise ValueError("Profile version is invalid; reload the page.")
            expected_version = int(version_text)
            attempts = _parse_attempt_form(data, course_catalog()) if "attempt_row" in data else _parse_attempt_text(self._value(data, "attempts"))
            if "primary_programme" in data:
                primary = self._value(data, "primary_programme").strip()
                secondary = self._value(data, "secondary_programme").strip()
                programs = tuple(dict.fromkeys(item for item in (primary, secondary) if item))
            else:
                programs = tuple(dict.fromkeys(part.strip() for part in self._value(data, "programmes").split(",") if part.strip()))
            allowed_programmes = {value for value, _ in PROGRAMME_OPTIONS} | set(existing.programme_ids if existing else ())
            if not programs or any(item not in allowed_programmes for item in programs):
                raise ValueError("Choose your degree from the programme list.")
            campus = self._value(data, "campus").strip()
            allowed_campuses = {value for value, _ in CAMPUS_OPTIONS} | ({existing.campus} if existing else set())
            if campus not in allowed_campuses:
                raise ValueError("Choose your campus from the list.")
            minor = self._value(data, "minor").strip() or None
            allowed_minors = {value for value, _ in MINOR_OPTIONS} | ({existing.minor_id} if existing and existing.minor_id else set())
            if minor is not None and minor not in allowed_minors:
                raise ValueError("Choose your minor from the list, or select no minor.")
            interests = tuple(dict.fromkeys(
                part.strip() for part in re.split(r"[,\n]+", self._value(data, "interests")) if part.strip()
            ))
            target_semester = self._value(data, "target_semester").strip()
            allowed_terms = {value for value, _ in _planning_term_options()}
            if target_semester not in allowed_terms:
                raise ValueError("Choose the semester you are planning for from the list.")
            profile = StudentProfile(
                profile_id=profile_id,
                campus=campus,
                admission_year=int(self._value(data, "admission_year")),
                programme_ids=programs,
                current_semester=int(self._value(data, "current_semester")),
                target_semester_id=target_semester,
                attempts=attempts,
                minor_id=minor,
                interests=interests,
            )
            repository.save(profile, expected_version=expected_version)
            token = self._cookie_token()
            if token:
                with previews_lock:
                    previews.pop(token_hash(token), None)
            self.send_response(303)
            self.send_header("Location", "/recommend?saved=1")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _profile_required(self, session: dict) -> StudentProfile:
            profile_id = session["profile_id"]
            if not owns_profile(session, profile_id):
                raise PermissionError("This profile does not belong to the current session.")
            profile = profile_repository(session).get(profile_id)
            if profile is None:
                raise ValueError("Save your profile before importing course history.")
            return profile

        def _import_page(self, session: dict) -> None:
            review = self._current_preview()
            if review and session.get("is_guest"):
                ready = f'<p class="ok">{len(_attempts_from_preview(review))} course records are ready. <a href="/">Return to your profile to save them.</a></p>'
            elif review:
                ready = f'<p class="ok">{len(_attempts_from_preview(review))} course records were saved. <a href="/">View your profile.</a></p>'
            else:
                ready = ""
            body = f'''<h1>Upload latest transcript</h1><p class="lede">Upload your latest BITS performance sheet. The PDF is discarded immediately after parsing; recognized past and current courses are filled into your profile.</p>{ready}
            <form class="panel" method="post" action="/import" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="action" value="preview"><label>Latest transcript (PDF)</label><input type="file" name="file" accept="application/pdf,.pdf" required><button>Upload and fill my profile</button></form>'''
            self._render("Upload transcript", body, session)

        @staticmethod
        def _render_preview(review: dict, session: dict) -> str:
            rows = []
            for item in review.get("attempt_candidates", []):
                idx = item["index"]
                selected = " checked" if item.get("selected") else ""
                mapping = item.get("course_id") or f"Not in the course list yet · {item.get('canonical_course_code')}"
                allow_unknown = f'<label><input style="width:auto" type="checkbox" name="unknown_{idx}" value="1"> Keep this course in my history anyway</label>' if not item.get("course_id") else ""
                rows.append(f'''<tr><td><input style="width:auto" type="checkbox" name="selected_{idx}" value="1"{selected}></td>
                <td>{_esc(mapping)}{allow_unknown}</td><td>{_esc(item.get('course_title'))}</td>
                <td><input name="grade_{idx}" value="{_esc(item.get('grade'))}" aria-label="Grade"></td>
                <td><input name="units_{idx}" value="{_esc(item.get('units'))}" aria-label="Units"></td>
                <td><input name="term_{idx}" value="{_esc(item.get('term'))}" aria-label="Term"></td>
                <td>{_status_select(f'status_{idx}', None)}</td></tr>''')
            if not rows:
                table = '<div class="empty">No course attempts were recognized. You can enter history manually on your profile.</div>'
            else:
                table = f'''<form method="post" action="/import" class="panel"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="action" value="confirm">
                <p class="muted">Extracted grades do not determine pass or fail. Choose a status for every selected row and check the grade, units, and term.</p>
                <table><thead><tr><th>Add</th><th>Course</th><th>Title</th><th>Grade</th><th>Units</th><th>Academic term</th><th>Result</th></tr></thead><tbody>{''.join(rows)}</tbody></table><button>Add selected courses to my history</button></form>'''
            issues = "".join(f'<li>{_esc(item.get("message"))}</li>' for item in review.get("issues", []))
            return f'<section class="panel"><h2>Transcript details: {_esc(review.get("filename"))}</h2>{table}<ul>{issues}</ul></section>'

        def _multipart(self) -> tuple[dict[str, str], bytes | None, str | None]:
            raw = self._read_body(MAX_MULTIPART_BYTES)
            content_type = self.headers.get("Content-Type", "")
            message = BytesParser(policy=email.policy.default).parsebytes(
                b"Content-Type: " + content_type.encode("ascii", "strict") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
            )
            fields: dict[str, str] = {}
            file_bytes = None
            filename = None
            if not message.is_multipart():
                raise ValueError("Could not parse multipart upload.")
            for part in message.iter_parts():
                disposition = part.get_content_disposition()
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True) or b""
                if disposition == "form-data" and name == "file":
                    filename = part.get_filename()
                    file_bytes = content
                elif disposition == "form-data" and name:
                    charset = part.get_content_charset() or "utf-8"
                    fields[name] = content.decode(charset, "strict")
            return fields, file_bytes, filename

        def _save_uploaded_transcript_profile(self, session: dict, review: dict) -> StudentProfile:
            if session.get("is_guest"):
                raise MarksheetImportError("A saved profile requires a signed-in account.")
            existing = profiles.get(session["profile_id"])
            metadata = review.get("profile_candidates", {})
            hints = _transcript_profile_hints(str(metadata.get("student_id") or ""))
            programmes = tuple(hints.get("programme_ids") or (existing.programme_ids if existing else ()))
            campus = str(hints.get("campus") or (existing.campus if existing else ""))
            admission_year = hints.get("admission_year") or (existing.admission_year if existing else None)
            if not programmes or not campus or not isinstance(admission_year, int):
                raise MarksheetImportError("Could not detect enough student details to create a saved profile from this transcript.")
            incoming = _attempts_from_preview(review)
            incoming_ids = {item.attempt_id for item in incoming if item.attempt_id}
            incoming_keys = {(item.course_id, item.term_id) for item in incoming}
            incoming_courses = {item.course_id for item in incoming}
            previous = existing.attempts if existing else ()
            kept = tuple(item for item in previous if not (
                (item.attempt_id and item.attempt_id in incoming_ids)
                or (item.course_id, item.term_id) in incoming_keys
                or (item.attempt_id is None and item.course_id in incoming_courses)
            ))
            terms = {item.term_id for item in incoming if item.term_id}
            planning_terms = _planning_term_options()
            profile = StudentProfile(
                profile_id=session["profile_id"],
                campus=campus,
                admission_year=admission_year,
                programme_ids=programmes,
                current_semester=existing.current_semester if existing else max(1, len(terms)),
                target_semester_id=existing.target_semester_id if existing else planning_terms[0][0],
                attempts=kept + incoming,
                minor_id=existing.minor_id if existing else None,
                interests=existing.interests if existing else (),
                profile_version=existing.profile_version if existing else 0,
            )
            return profiles.save(profile, expected_version=existing.profile_version if existing else 0)

        def _import_post(self, session: dict) -> None:
            try:
                data, payload, filename = self._multipart()
                supplied = data.get("csrf_token")
                valid_csrf = (isinstance(supplied, str) and hmac.compare_digest(session["csrf_token"], supplied)) if session.get("is_guest") else accounts.verify_csrf(self._cookie_token(), supplied)
                if not valid_csrf:
                    self._render("Request expired", '<p class="error">Your form token is invalid or expired.</p>', session, 403)
                    return
                token = self._cookie_token()
                key = token_hash(token) if token else ""
                if data.get("action") == "preview":
                    if payload is None or filename is None:
                        raise MarksheetImportError("Choose a PDF to upload.")
                    review = preview_marksheet(payload, filename, active_snapshot())
                    with previews_lock:
                        previews[key] = (time.time() + PREVIEW_TTL_SECONDS, review)
                    if not session.get("is_guest"):
                        self._save_uploaded_transcript_profile(session, review)
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                profile = self._profile_required(session)
                if data.get("action") != "confirm":
                    raise MarksheetImportError("Unknown marksheet action.")
                with previews_lock:
                    stored = previews.get(key)
                if not stored or stored[0] < time.time():
                    raise MarksheetImportError("This preview expired. Upload the PDF again.")
                candidates = stored[1].get("attempt_candidates", [])
                submitted = []
                for candidate in candidates:
                    idx = candidate["index"]
                    submitted.append({
                        "index": idx,
                        "selected": data.get(f"selected_{idx}") == "1",
                        "status": data.get(f"status_{idx}", ""),
                        "grade": data.get(f"grade_{idx}", ""),
                        "units": data.get(f"units_{idx}", ""),
                        "term_id": data.get(f"term_{idx}", ""),
                        "allow_unknown": data.get(f"unknown_{idx}") == "1",
                    })
                attempts = confirmed_attempts(stored[1], submitted)
                if not attempts:
                    raise MarksheetImportError("Select at least one course attempt to add.")
                updated = StudentProfile(
                    profile_id=profile.profile_id, campus=profile.campus,
                    admission_year=profile.admission_year, programme_ids=profile.programme_ids,
                    current_semester=profile.current_semester, target_semester_id=profile.target_semester_id,
                    attempts=profile.attempts + attempts, minor_id=profile.minor_id,
                    interests=profile.interests, profile_version=profile.profile_version,
                )
                saved = profile_repository(session).save(updated, expected_version=profile.profile_version)
                with previews_lock:
                    previews.pop(key, None)
                self._render("Course history updated", f'<p class="ok">Added {len(attempts)} course attempt(s) to your profile.</p><p><a href="/">Return to profile</a></p>', session)
            except (ValueError, TypeError, UnicodeError, OverflowError, MarksheetImportError, ProfileConflictError, ProfileValidationError, OSError) as exc:
                self._render("Could not import marksheet", f'<p class="error">{_esc(exc)}</p><p><a href="/import">Try another upload</a></p>', session, 400)

        def _documents_page(self, session: dict, query: dict[str, list[str]]) -> None:
            q = query.get("q", [""])[0].strip()
            token = self._cookie_token()
            key = token_hash(token) if token else ""
            if query.get("clear", [""])[0] == "1":
                with chat_lock:
                    chat_threads.pop(key, None)
            with chat_lock:
                history = list(chat_threads.get(key, []))
            error = ""
            if q:
                try:
                    if document_chat_agent is None:
                        raise RuntimeError("The chat service is not configured.")
                    response = document_chat_agent.answer(q, history)
                    history = [*history, response][-10:]
                    with chat_lock:
                        chat_threads[key] = history
                except Exception:
                    error = "I couldn't answer that right now. Please try again."
            conversation = []
            for item in history:
                answer_html = _esc(item.get("answer")).replace("\n", "<br>")
                grouped_sources: dict[tuple, dict] = {}
                for citation in item.get("citations", []):
                    page = citation.get("page")
                    unit_key = citation.get("unit_key")
                    section = citation.get("section")
                    source_key = (citation.get("document_id"), page, unit_key, section)
                    grouped = grouped_sources.setdefault(source_key, {"citation": citation, "numbers": []})
                    grouped["numbers"].append(str(citation.get("number")))
                sources = []
                for grouped in grouped_sources.values():
                    citation = grouped["citation"]
                    page = citation.get("page")
                    unit_key = citation.get("unit_key")
                    section = citation.get("section")
                    label = f'{citation.get("file_name")} · page {page}' if page is not None else f'{citation.get("file_name")} · {section or unit_key or "source"}'
                    suffix = f'?page={page}' if page is not None else (f'?unit={_esc(unit_key)}' if unit_key else f'?section={_esc(section)}' if section else '')
                    href = f'/sources/{_esc(citation["document_id"])}' + suffix
                    sources.append(f'<a class="link" href="{href}">[{_esc(",".join(grouped["numbers"]))}] {_esc(label)}</a>')
                source_html = f'<p class="muted">Sources: {" · ".join(sources)}</p>' if sources else ""
                conversation.append(f'<p><strong>You</strong><br>{_esc(item.get("question"))}</p><article class="card"><strong>BITSbuddy</strong><p>{answer_html}</p>{source_html}</article>')
            error_html = f'<p class="error">{_esc(error)}</p>' if error else ""
            empty = '<div class="empty">Ask about courses, regulations, handouts, timetables, or anything else.</div>' if not history else ""
            form = f'''<form method="post" action="/documents" class="panel"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}">
            {_textarea('Ask anything','q','',3)}<button>Send</button></form>'''
            clear = f'''<form method="post" action="/documents"><input type="hidden" name="csrf_token" value="{_esc(session['csrf_token'])}"><input type="hidden" name="clear" value="1"><button class="secondary">Clear chat</button></form>''' if history else ""
            self._render("Ask BITSbuddy", f'<h1>Ask BITSbuddy</h1>{form}{error_html}{empty}{"".join(conversation)}{clear}', session)

        def _source_page(self, session: dict, document_id: str, query: dict[str, list[str]]) -> None:
            if "/file" in document_id:
                clean_id = document_id.removesuffix("/file")
                self._source_file(clean_id)
                return
            doc = get_document(corpus_path, document_id)
            if not doc:
                self._render("Source not found", '<div class="empty">This source ID is not in the indexed library.</div>', session, 404)
                return
            page_text = query.get("page", [""])[0]
            page_number = int(page_text) if page_text.isdecimal() else None
            unit_key = query.get("unit", [""])[0] or None
            section = query.get("section", [""])[0] or None
            unit = get_document_page(corpus_path, document_id, page_number, section=section, unit_key=unit_key)
            label = f"Page {page_number}" if page_number is not None else f"Section {section}" if section else f"Unit {unit_key}" if unit_key else "Source details"
            text = _esc(unit.get("text", "")) if unit else "No page text was found for this reference."
            sources = []
            for path in doc.get("sources", []):
                try:
                    resolved = Path(path).resolve(strict=True)
                    resolved.relative_to(source_root)
                    file_link = f'<a href="/sources/{_esc(document_id)}/file">Open indexed source file</a>' if resolved.is_file() else ""
                    if file_link:
                        sources.append(file_link)
                except (OSError, ValueError):
                    continue
            body = f'<h1>{_esc(doc.get("primary_file_name"))}</h1><p class="muted">{_esc(doc.get("document_type"))} · {_esc(label)} · section {_esc(unit.get("section") if unit else "not indexed")}</p><article class="panel"><pre>{text}</pre></article><p>{" · ".join(sources)}</p>'
            self._render("Cited source", body, session)

        def _source_file(self, document_id: str) -> None:
            doc = get_document(corpus_path, document_id)
            if not doc:
                self._send(b"Not found", 404, "text/plain; charset=utf-8")
                return
            for path in doc.get("sources", []):
                try:
                    source = Path(path).resolve(strict=True)
                    source.relative_to(source_root)
                    if source.is_file() and source.suffix.casefold() in {".pdf", ".docx"}:
                        payload = source.read_bytes()
                        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", source.name)
                        self._send(payload, 200, "application/pdf" if source.suffix.casefold() == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document", {"Content-Disposition": f'inline; filename="{safe_name}"'})
                        return
                except (OSError, ValueError):
                    continue
            self._send(b"Not found", 404, "text/plain; charset=utf-8")

        @staticmethod
        def close_resources() -> None:
            with previews_lock:
                previews.clear()
            with chat_lock:
                chat_threads.clear()
            with recommendation_lock:
                guest_recommendations.clear()
            profiles.close()
            guest_profiles.close()
            accounts.close()

    return Handler
