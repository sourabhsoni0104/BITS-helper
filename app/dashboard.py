from __future__ import annotations

import argparse
import html
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recommender.ingestion.snapshots import SnapshotError, load_snapshot  
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile  
from recommender.policies.engine import PolicyResolutionError  
from recommender.services.academic_agent import AcademicAgent
from recommender.services.document_chat_agent import DocumentChatAgent
from recommender.services.groq_client import GroqClient
from recommender.storage.profiles import ProfileRepository  


CSS = """
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#19212a;background:#f4f6f8}
*{box-sizing:border-box}body{margin:0}.shell{max-width:1100px;margin:auto;padding:34px 22px 60px}
h1{font-size:clamp(2rem,5vw,3.5rem);letter-spacing:-.05em;margin:.2rem 0}.lede{color:#52606d;max-width:720px;line-height:1.6}
.banner{background:#fff0c2;border:1px solid #e8bf49;padding:12px 16px;border-radius:12px;margin:20px 0;font-weight:650}
.grid{display:grid;grid-template-columns:minmax(270px,.8fr) minmax(0,1.4fr);gap:20px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.panel,.card{background:white;border:1px solid #dce2e8;border-radius:16px;padding:20px;box-shadow:0 8px 30px #1b30430a}.panel h2,.card h3{margin-top:0}
label{display:block;font-size:.8rem;font-weight:750;margin:14px 0 5px;color:#52606d}input,textarea{width:100%;border:1px solid #b8c2cc;border-radius:9px;padding:10px;font:inherit}textarea{min-height:105px;resize:vertical}
button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:#0d6b58;color:white;font-weight:750;cursor:pointer}.card{margin:12px 0}.meta{font-size:.85rem;color:#687784}.pill{display:inline-block;background:#e6f6f2;color:#075e4c;border-radius:99px;padding:4px 9px;font-size:.75rem;font-weight:750;margin:0 5px 5px 0}.error{color:#9d2a1e;background:#fff0ee;padding:12px;border-radius:9px}.empty{padding:28px;border:1px dashed #9cabb8;border-radius:14px;color:#52606d}.progress{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.progress div{background:#f1f5f7;padding:10px;border-radius:9px;font-size:.83rem}@media(max-width:560px){.progress{grid-template-columns:repeat(2,1fr)}}
details{margin-top:10px}.sources{font-size:.78rem;color:#52606d}code{background:#eef2f5;padding:2px 5px;border-radius:4px}
"""


def _load_local_environment(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("\"'")


def page(content: str, synthetic: bool = False) -> bytes:
    banner = '<div class="banner">Synthetic demo — all courses, rules, and evidence below are fictional.</div>' if synthetic else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>BITSbuddy</title><style>{CSS}</style></head><body><main class="shell"><div class="meta">BITSBUDDY</div><h1>Choose with the rules in view.</h1><p class="lede">Academic eligibility is resolved before course preferences. Unknown facts stay visible instead of becoming recommendations.</p>{banner}{content}</main></body></html>""".encode()


def form(values: dict[str, str]) -> str:
    esc = lambda key: html.escape(values.get(key, ""), quote=True)
    return f"""<section class="panel"><h2>Student context</h2>
    <form method="get"><label>Load saved profile</label><div style="display:flex;gap:8px"><input name="profile_id" value="{esc('profile_id')}" required><button style="width:auto;margin:0">Load</button></div></form>
    <form method="post"><input type="hidden" name="profile_version" value="{esc('profile_version')}">
    <label>Profile ID</label><input name="profile_id" value="{esc('profile_id')}" required>
    <label>Campus</label><input name="campus" value="{esc('campus')}" required>
    <label>Admission year</label><input name="admission_year" type="number" value="{esc('admission_year')}" required>
    <label>Programme IDs (comma-separated)</label><input name="programmes" value="{esc('programmes')}" required>
    <label>Current semester</label><input name="current_semester" type="number" min="1" value="{esc('current_semester')}" required>
    <label>Target term</label><input name="target_semester" value="{esc('target_semester')}" required>
    <label>Completed course IDs (comma-separated)</label><input name="completed" value="{esc('completed')}">
    <label>Current course IDs (comma-separated)</label><input name="current" value="{esc('current')}">
    <label>Minor ID (optional)</label><input name="minor" value="{esc('minor')}">
    <label>Academic interests (comma-separated)</label><input name="interests" value="{esc('interests')}">
    <label>What are you looking for?</label><textarea name="query" required>{html.escape(values.get('query', ''))}</textarea>
    <button>Save profile and recommend</button></form></section>"""


def render_result(result: dict) -> str:
    progress = "".join(
        f"<div><strong>{html.escape(item['category'])}</strong><br>{item['credited_amount']:g} / {item['required_amount'] if item['required_amount'] is not None else 'unknown'} {html.escape(item['metric'])}</div>"
        for item in result["requirement_summary"]
    )
    interpretation = result["interpreted_query"]
    summary = f"Category: {interpretation['requested_category'] or 'not specified'} · topics: {', '.join(interpretation['topic_preferences']) or 'none'}"
    if result["clarification_questions"]:
        cards = "".join(f'<p class="error">{html.escape(question)}</p>' for question in result["clarification_questions"])
    elif result["recommendations"]:
        cards = ""
        for item in result["recommendations"]:
            checks = "".join(f"<li>{html.escape(check['status'])}: {html.escape(check['explanation'])}</li>" for check in item["eligibility_reasons"])
            sources = ", ".join(item["evidence_references"])
            contribution = item["requirement_contribution"]
            requirement_line = "No requirement contribution found" if contribution is None else f"Can contribute to {contribution['requirement_id']} (remaining before selection: {contribution['remaining_before_selection']})"
            cards += f"""<article class="card"><span class="pill">{html.escape(item['eligibility_status'])} eligible</span><span class="pill">schedule not checked</span><h3>{html.escape(item['course_code'])} — {html.escape(item['title'])}</h3><p>{html.escape(requirement_line)}</p><p>Matches: {html.escape(', '.join(item['matched_topics']) or 'academic and category constraints')}</p><details><summary>Eligibility checks</summary><ul>{checks}</ul><p class="sources">Evidence IDs: {html.escape(sources)}</p></details></article>"""
    else:
        cards = f'<div class="empty">{html.escape(result["no_result_reason"] or "No result found.")}</div>'
        if result["unverified_alternatives"]:
            cards += "<h3>Other possible matches</h3>" + "".join(f'<p>{html.escape(item["course_code"])} — {html.escape(item["reason"])}</p>' for item in result["unverified_alternatives"])
    return f"""<section><div class="panel"><h2>Academic progress</h2><div class="progress">{progress}</div></div><div class="panel" style="margin-top:20px"><h2>Recommendations</h2><p class="meta">{html.escape(summary)}</p>{cards}</div></section>"""


def render_document_chat(agent: DocumentChatAgent | None, query: str = "") -> str:
    escaped_query = html.escape(query, quote=True)
    form_html = f"""<form method="get" action="/documents">
    <label>Ask anything</label>
    <textarea name="q" required>{escaped_query}</textarea>
    <button>Send</button></form>"""
    if not query:
        content = '<div class="empty">Ask about courses, regulations, handouts, timetables, or anything else.</div>'
    else:
        try:
            if agent is None:
                raise RuntimeError
            result = agent.answer(query)
            sources = " · ".join(f'[{item["number"]}] {html.escape(str(item["file_name"]))}' for item in result["citations"])
            source_html = f'<p class="sources">Sources: {sources}</p>' if sources else ""
            content = f'<article class="card"><strong>BITSbuddy</strong><p>{html.escape(result["answer"])}</p>{source_html}</article>'
        except Exception:
            content = '<p class="error">I couldn’t answer that right now. Please try again.</p>'
    return f'<section class="panel"><h2>Ask BITSbuddy</h2>{form_html}{content}</section>'


def _values_from_profile(profile: StudentProfile, query: str) -> dict[str, str]:
    return {
        "profile_id": profile.profile_id,
        "profile_version": str(profile.profile_version),
        "campus": profile.campus,
        "admission_year": str(profile.admission_year),
        "programmes": ",".join(profile.programme_ids),
        "current_semester": str(profile.current_semester),
        "target_semester": profile.target_semester_id,
        "completed": ",".join(item.course_id for item in profile.attempts if item.status == AttemptStatus.COMPLETED),
        "current": ",".join(item.course_id for item in profile.attempts if item.status == AttemptStatus.IN_PROGRESS),
        "minor": profile.minor_id or "",
        "interests": ",".join(profile.interests),
        "query": query,
    }


MAX_FORM_BYTES = 64 * 1024


def make_handler(snapshot, profiles: ProfileRepository, corpus_path: Path):
    groq_client = GroqClient.from_environment()
    academic_agent = AcademicAgent(groq_client, corpus_path) if groq_client is not None else None
    document_chat_agent = DocumentChatAgent(groq_client, corpus_path) if groq_client is not None else None
    defaults = {
        "profile_id": "demo-student" if snapshot.synthetic else "",
        "profile_version": "0",
        "campus": "SYNTHETIC" if snapshot.synthetic else "",
        "admission_year": "2025" if snapshot.synthetic else "",
        "programmes": "BSC-SYNTH" if snapshot.synthetic else "",
        "current_semester": "3" if snapshot.synthetic else "",
        "target_semester": "SYN-2026-T1" if snapshot.synthetic else "",
        "completed": "SYN-100" if snapshot.synthetic else "",
        "current": "",
        "minor": "",
        "interests": "artificial intelligence" if snapshot.synthetic else "",
        "query": "Suggest an AI-related DEL with no midsem." if snapshot.synthetic else "",
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query_values = parse_qs(parsed.query)
            if parsed.path == "/documents":
                query = query_values.get("q", [""])[0].strip()
                self._send(page(render_document_chat(document_chat_agent, query), snapshot.synthetic))
                return
            profile_id = query_values.get("profile_id", [""])[0].strip()
            values = defaults
            notice = "Submit a profile and request to run the live pipeline."
            if profile_id:
                loaded = profiles.get(profile_id)
                if loaded is None:
                    values = {**defaults, "profile_id": profile_id}
                    notice = f"No saved profile named {profile_id}."
                else:
                    values = _values_from_profile(loaded, defaults["query"])
                    notice = f"Loaded profile {profile_id} version {loaded.profile_version}."
            right = f'<section><div class="empty">{html.escape(notice)}<br><br>Dataset: <code>{html.escape(snapshot.dataset_version)}</code></div><div style="margin-top:20px">{render_document_chat(document_chat_agent)}</div></section>'
            self._send(page(f'<div class="grid">{form(values)}{right}</div>', snapshot.synthetic))

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/":
                self._send(page('<p class="error">Unknown request path.</p>'), 404)
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type != "application/x-www-form-urlencoded":
                self._send(page('<p class="error">Expected an HTML form submission.</p>'), 415)
                return
            raw_length = self.headers.get("Content-Length")
            try:
                if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                    raise ValueError
                length = int(raw_length)
                if length <= 0:
                    raise ValueError
            except ValueError:
                self._send(page('<p class="error">Request body length must be a positive integer.</p>'), 400)
                return
            if length > MAX_FORM_BYTES:
                self._send(page('<p class="error">Form is too large; the maximum request body is 64 KiB.</p>'), 413)
                return
            try:
                body_bytes = self.rfile.read(length)
                if len(body_bytes) != length:
                    self._send(page('<p class="error">Request body ended before the declared length.</p>'), 400)
                    return
                raw_body = body_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self._send(page('<p class="error">Form must contain valid UTF-8 text.</p>'), 400)
                return
            try:
                raw = parse_qs(raw_body, keep_blank_values=True, strict_parsing=False, errors="strict")
            except (UnicodeDecodeError, ValueError):
                self._send(page('<p class="error">Form fields are malformed or contain invalid UTF-8.</p>'), 400)
                return
            values = {key: items[0] for key, items in raw.items()}
            values = {**defaults, **values}
            query = values.get("query", "").strip()
            if "query" not in raw or not query:
                self._send(page(f'<div class="grid">{form(values)}<p class="error">Enter a request before saving the profile.</p></div>', snapshot.synthetic), 400)
                return
            required_fields = ("profile_id", "campus", "admission_year", "programmes", "current_semester", "target_semester")
            if any(key not in raw or not values.get(key, "").strip() for key in required_fields):
                self._send(page(f'<div class="grid">{form(values)}<p class="error">Complete all required profile fields before saving.</p></div>', snapshot.synthetic), 400)
                return
            try:
                completed = tuple(filter(None, (item.strip() for item in values.get("completed", "").split(","))))
                current = tuple(filter(None, (item.strip() for item in values.get("current", "").split(","))))
                profile_id = values["profile_id"].strip()
                existing = profiles.get(profile_id)
                expected_version = int(values.get("profile_version", "0"))
                requested_statuses = {
                    **{course_id: AttemptStatus.COMPLETED for course_id in completed},
                    **{course_id: AttemptStatus.IN_PROGRESS for course_id in current},
                }
                if set(completed) & set(current):
                    raise ValueError("A course cannot be both completed and in progress.")
                attempts: list[CourseAttempt] = []
                if existing is not None:
                    for old_attempt in existing.attempts:
                        requested = requested_statuses.pop(old_attempt.course_id, None)
                        if old_attempt.status in {AttemptStatus.FAILED, AttemptStatus.WITHDRAWN} and requested is None:
                            attempts.append(old_attempt)
                        elif requested is not None:
                            if requested == old_attempt.status:
                                attempts.append(old_attempt)
                            else:
                                attempts.append(CourseAttempt(old_attempt.course_id, requested))
                attempts.extend(CourseAttempt(course_id, status) for course_id, status in requested_statuses.items())
                profile = StudentProfile(
                    profile_id=profile_id,
                    campus=values["campus"].strip(),
                    admission_year=int(values["admission_year"]),
                    programme_ids=tuple(item.strip() for item in values["programmes"].split(",") if item.strip()),
                    current_semester=int(values["current_semester"]),
                    target_semester_id=values["target_semester"].strip(),
                    attempts=tuple(attempts),
                    minor_id=values.get("minor", "").strip() or None,
                    interests=tuple(item.strip() for item in values.get("interests", "").split(",") if item.strip()),
                )
                saved = profiles.save(profile, expected_version=expected_version)
                values = _values_from_profile(saved, query)
                notice = '<div class="banner" style="background:#e6f6f2;border-color:#83c7b7">Profile saved.</div>'
                try:
                    if academic_agent is None:
                        output = notice + '<section class="panel"><p class="error">AI recommendations are not configured. Add a Groq API key and try again.</p></section>'
                    else:
                        try:
                            result = academic_agent.run(saved, query, snapshot)
                            output = notice + render_result(result)
                        except Exception:
                            output = notice + '<section class="panel"><p class="error">The AI recommendation service is temporarily busy. Try again in a moment.</p></section>'
                except PolicyResolutionError as exc:
                    output = notice + f'<section class="panel"><h2>Profile saved; recommendations unavailable</h2><p class="error">{html.escape(str(exc))}</p></section>'
            except (KeyError, ValueError, PolicyResolutionError) as exc:
                output = f'<section class="panel"><h2>Cannot analyze this profile</h2><p class="error">{html.escape(str(exc))}</p></section>'
            self._send(page(f'<div class="grid">{form(values)}{output}</div>', snapshot.synthetic))

        def log_message(self, format: str, *args) -> None:
            print(f"dashboard: {format % args}")

    return Handler


def main() -> None:
    _load_local_environment(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.getenv("APP_DATA_PATH", "data/processed/active.json"))
    parser.add_argument("--demo", action="store_true", default=os.getenv("DEMO_MODE", "false").casefold() == "true")
    parser.add_argument("--host", default=os.getenv("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8501")))
    parser.add_argument("--profile-db", default=os.getenv("APP_PROFILE_DB", "data/processed/profiles.sqlite"))
    parser.add_argument("--corpus-db", default=os.getenv("APP_CORPUS_DB", "data/processed/document-corpus.sqlite"))
    parser.add_argument("--account-db", default=os.getenv("APP_ACCOUNT_DB", "data/processed/accounts.sqlite"))
    parser.add_argument("--review", default=os.getenv("APP_REVIEW_PATH", "data/processed/review-bundle.json"))
    parser.add_argument("--handout-index", default=os.getenv("APP_HANDOUT_INDEX", "data/raw/All Courses Handouts.html"))
    args = parser.parse_args()
    if not args.demo:
        from app.workbench import make_workbench_handler
        handler = make_workbench_handler(snapshot_path=Path(args.data), corpus_path=Path(args.corpus_db),
            profile_db=Path(args.profile_db), account_db=Path(args.account_db),
            review_path=Path(args.review), index_path=Path(args.handout_index))
        server = ThreadingHTTPServer((args.host, args.port), handler)
        print(f"Dashboard: http://{args.host}:{args.port} (real-data workbench)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            handler.close_resources()
        return
    path = ROOT / "data/synthetic/demo_snapshot.json" if args.demo else Path(args.data)
    corpus_path = Path(args.corpus_db)
    fallback_client = GroqClient.from_environment()
    fallback_chat_agent = DocumentChatAgent(fallback_client, corpus_path) if fallback_client is not None else None
    profiles = None
    try:
        snapshot = load_snapshot(path)
    except SnapshotError as exc:
        message = f"No validated academic dataset is active. Supply and publish normalized academic data first. Details: {exc}"

        class MissingDataHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/documents":
                    query = parse_qs(parsed.query).get("q", [""])[0].strip()
                    body = page(render_document_chat(fallback_chat_agent, query))
                    status = 200
                else:
                    body = page(f'<div class="grid"><div class="empty"><strong>Academic data required</strong><p>{html.escape(message)}</p><p>A student profile and structured academic dataset are required for recommendations.</p></div>{render_document_chat(fallback_chat_agent)}</div>')
                    status = 503
                self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, format: str, *args) -> None: pass

        handler = MissingDataHandler
    else:
        profiles = ProfileRepository(":memory:" if args.demo else args.profile_db)
        handler = make_handler(snapshot, profiles, corpus_path)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Dashboard: http://{args.host}:{args.port} ({'synthetic demo' if args.demo else 'real-data mode'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if profiles is not None:
            profiles.close()


if __name__ == "__main__":
    main()
