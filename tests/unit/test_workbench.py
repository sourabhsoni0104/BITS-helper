from __future__ import annotations

import html
import http.cookiejar
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

from app.workbench import (
    BRANCH_CODE_TO_PROGRAMME,
    PROGRAMME_OPTIONS,
    _course_matches_from_staged,
    _parse_attempt_form,
    _parse_attempt_text,
    _planning_term_options,
    _profile_attempt_text,
    _term_options,
    _transcript_profile_hints,
    make_workbench_handler,
)
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.storage.accounts import AccountRepository


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.corpus = root / "processed" / "corpus.sqlite"
        self.corpus.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.corpus)) as db:
            db.executescript("""
                CREATE TABLE documents(document_id TEXT PRIMARY KEY,content_sha256 TEXT,primary_file_name TEXT,document_type TEXT,semester TEXT,page_count INTEGER,parser_version TEXT,indexed_at TEXT);
                CREATE TABLE sources(source_path TEXT PRIMARY KEY,file_name TEXT,document_id TEXT);
                CREATE TABLE pages(document_id TEXT,page_number INTEGER,unit_key TEXT,text TEXT,verification_status TEXT,section TEXT);
                INSERT INTO documents VALUES('DOC-test','hash','rules.pdf','regulations',NULL,1,'v1','now');
                INSERT INTO pages VALUES('DOC-test',1,'1','Rules source text','needs_review','Section A');
            """)
        outside = root / "private.pdf"
        outside.write_bytes(b"private")
        with closing(sqlite3.connect(self.corpus)) as db:
            db.execute("INSERT INTO sources VALUES(?,?,?)", (str(outside), "private.pdf", "DOC-test"))
            db.commit()
        self.handler = make_workbench_handler(
            snapshot_path=root / "snapshot.json", corpus_path=self.corpus,
            profile_db=root / "profiles.sqlite", account_db=root / "accounts.sqlite",
            review_path=root / "review.json", index_path=root / "index.html",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.handler.close_resources()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def client(self):
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def register(self, opener, email):
        response = opener.open(Request(self.base + "/register", data=urlencode({
            "email": email, "password": "strong-passphrase",
        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}))
        return response.read().decode()

    @staticmethod
    def csrf(page):
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        return html.unescape(match.group(1)) if match else None

    def test_auth_required_profile_csrf_and_owner_scoping(self):
        anonymous = self.client()
        page = anonymous.open(self.base + "/").read().decode()
        self.assertIn("Sign in", page)
        self.assertNotIn("Save profile and get recommendations", page)

        first = self.client()
        self.register(first, "first@example.edu")
        login_page = first.open(self.base + "/").read().decode()
        csrf = self.csrf(login_page)
        self.assertTrue(csrf)
        denied = Request(self.base + "/", data=urlencode({
            "campus": "Pilani", "admission_year": "2025", "programmes": "BE-CS",
            "current_semester": "2", "target_semester": "2026-T1", "profile_version": "0",
        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(HTTPError) as caught:
            first.open(denied)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

        form = {
            "csrf_token": csrf, "campus": "Goa", "admission_year": "2025",
            "programmes": "BE-CS", "current_semester": "2", "target_semester": "2026-T1",
            "profile_version": "0", "attempts": "", "query": "",
        }
        first.open(Request(self.base + "/", data=urlencode(form).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})).read()
        accounts = AccountRepository(Path(self.temp.name) / "accounts.sqlite")
        first_user, first_profile = accounts.login("first@example.edu", "strong-passphrase")[1]["user_id"], accounts.login("first@example.edu", "strong-passphrase")[1]["profile_id"]
        accounts.close()

        second = self.client()
        self.register(second, "second@example.edu")
        second_page = second.open(self.base + f"/?profile_id={first_profile}").read().decode()
        self.assertNotIn(first_profile, second_page)
        self.assertNotIn('value="Goa" selected', second_page)
        
        
        self.assertTrue(first_user)

    def test_guest_profile_is_session_only_and_login_remains_optional(self):
        guest = self.client()
        landing = guest.open(self.base + "/").read().decode()
        self.assertIn("Continue as guest", landing)
        page = guest.open(Request(self.base + "/guest", data=b"", method="POST")).read().decode()
        self.assertIn("Guest mode", page)
        self.assertIn("Switch to saved account", page)
        self.assertNotIn("Source review", page)
        csrf = self.csrf(page)
        form = {
            "csrf_token": csrf, "campus": "Pilani", "admission_year": "2025",
            "primary_programme": "BE-CS", "secondary_programme": "", "minor": "",
            "current_semester": "2", "target_semester": "2026-T1",
            "profile_version": "0", "interests": "AI, systems", "query": "",
            "attempt_row": "0", "attempt_course_0": "CS F111",
            "attempt_status_0": "completed", "attempt_grade_0": "A",
            "attempt_units_0": "4", "attempt_term_0": "2025-T1",
            "attempt_id_0": "", "attempt_order_0": "",
        }
        saved = guest.open(Request(self.base + "/", data=urlencode(form).encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })).read().decode()
        self.assertIn("Profile saved at version 1", saved)
        edited = guest.open(self.base + "/").read().decode()
        self.assertIn("B.E. Computer Science", edited)
        self.assertIn("CS F111", edited)
        self.assertIn("Add another course", edited)
        self.assertNotIn("Programme IDs", edited)
        self.assertNotIn("CSV rows", edited)
        self.assertNotIn("Subjects or topics you enjoy", edited)
        self.assertNotIn("What kind of courses are you looking for?", edited)
        self.assertIn("Delete course", edited)
        self.assertNotIn("<th>Remove</th>", edited)
        self.assertIn("Continue to course preferences", edited)
        with closing(sqlite3.connect(self.root / "profiles.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM student_profiles").fetchone()[0], 0)
        guest.open(Request(self.base + "/logout", data=urlencode({"csrf_token": csrf}).encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })).read()
        after = guest.open(self.base + "/").read().decode()
        self.assertIn("Sign in", after)
        self.assertNotIn("Pilani", after)

    def test_latest_transcript_fills_profile_before_manual_setup(self):
        guest = self.client()
        page = guest.open(Request(self.base + "/guest", data=b"", method="POST")).read().decode()
        csrf = self.csrf(page)
        boundary = "----transcript-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{csrf}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"action\"\r\n\r\npreview\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"latest.pdf\"\r\nContent-Type: application/pdf\r\n\r\n"
        ).encode() + b"%PDF-1.4 transcript" + f"\r\n--{boundary}--\r\n".encode()
        review = {
            "filename": "latest.pdf",
            "profile_candidates": {"student_id": "2025A7PS0001P", "student_name": "TEST STUDENT", "cgpa": "8.50"},
            "attempt_candidates": [{
                "index": 0, "course_code": "CS F111", "canonical_course_code": "CS F111",
                "course_id": None, "course_mapping_status": "unknown", "course_title": "Programming",
                "grade": "A", "units": 4.0, "term": "2025-T1", "attempt_id": "marksheet-test-0",
                "attempt_order": 2025 * 3 + 1, "status": "completed", "selected": True,
            }],
            "issues": [],
        }
        with patch("app.workbench.preview_marksheet", return_value=review):
            filled = guest.open(Request(self.base + "/import", data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            })).read().decode()
        self.assertIn("Loaded latest.pdf", filled)
        self.assertIn("TEST STUDENT", filled)
        self.assertIn("CGPA 8.50", filled)
        self.assertIn('name="attempt_course_0" value="CS F111"', filled)
        self.assertIn('name="admission_year" type="number" value="2025"', filled)
        self.assertIn('<option value="BE-CS" selected>B.E. Computer Science</option>', filled)
        self.assertIn('<option value="Pilani" selected>Pilani campus</option>', filled)
        self.assertNotIn('value="2016-T1"', filled)
        self.assertIn('value="2025-T1"', filled)

    def test_student_id_decodes_single_and_integrated_dual_programmes(self):
        self.assertEqual({value for value, _ in PROGRAMME_OPTIONS}, set(BRANCH_CODE_TO_PROGRAMME.values()))
        self.assertEqual(len(PROGRAMME_OPTIONS), 18)
        self.assertEqual(_transcript_profile_hints("2025A7PS0642P"), {
            "admission_year": 2025,
            "programme_ids": ("BE-CS",),
            "campus": "Pilani",
            "branch_codes": ("A7",),
        })
        self.assertEqual(_transcript_profile_hints("2024B3A7PS1234H"), {
            "admission_year": 2024,
            "programme_ids": ("MSC-ECONOMICS", "BE-CS"),
            "campus": "Hyderabad",
            "branch_codes": ("B3", "A7"),
        })

    def test_course_history_accepts_catalog_code_or_name_and_filters_old_terms(self):
        catalog = (("COURSE-1", "CS F111", "Computer Programming"),)
        base = {
            "attempt_row": ["0"], "attempt_status_0": ["completed"],
            "attempt_grade_0": ["A"], "attempt_units_0": ["4"],
            "attempt_term_0": ["2025-T1"], "attempt_id_0": [""],
            "attempt_order_0": [""],
        }
        by_code = _parse_attempt_form({**base, "attempt_course_0": ["cs f111"]}, catalog)
        by_name = _parse_attempt_form({**base, "attempt_course_0": ["Computer Programming"]}, catalog)
        self.assertEqual(by_code[0].course_id, "COURSE-1")
        self.assertEqual(by_name[0].course_id, "COURSE-1")
        terms = _term_options(history=True, admission_year=2025)
        self.assertTrue(terms)
        self.assertTrue(all(int(value[:4]) >= 2025 for value, _ in terms))

    def test_planning_terms_never_include_completed_semesters(self):
        september = dict(_planning_term_options(current_year=2026, current_month=9))
        self.assertEqual(next(iter(september)), "2026-T1")
        self.assertNotIn("2025-T1", september)
        self.assertNotIn("2025-T2", september)
        january = dict(_planning_term_options(current_year=2026, current_month=1))
        self.assertEqual(next(iter(january)), "2025-T2")
        self.assertNotIn("2025-T1", january)

    def test_profile_course_autocomplete_uses_staged_catalog(self):
        (self.root / "review.json").write_text(json.dumps({
            "snapshot": {"courses": [{
                "course_id": "COURSE-1", "code": "CS F111", "title": "Computer Programming",
            }]},
        }), encoding="utf-8")
        guest = self.client()
        page = guest.open(Request(self.base + "/guest", data=b"", method="POST")).read().decode()
        self.assertIn('datalist id="course-catalog"', page)
        self.assertIn('<option value="CS F111">Computer Programming</option>', page)
        self.assertIn('<option value="Computer Programming">CS F111</option>', page)

    def test_staged_documents_produce_course_matches_without_publication(self):
        profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")
        staged = {
            "courses": [{
                "course_id": "CS F407", "code": "CS F407", "title": "Artificial Intelligence",
                "evidence_ids": ["EV-1"],
            }],
            "evidence": [{
                "evidence_id": "EV-1", "excerpt": "machine learning and intelligent systems",
            }],
        }
        result = _course_matches_from_staged(profile, "I want AI courses", staged)
        self.assertEqual(result["recommendations"][0]["course_code"], "CS F407")
        self.assertEqual(result["recommendations"][0]["badge"], "Course match")

    def test_vague_request_returns_diverse_discovery_not_literal_word_match(self):
        profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")
        staged = {
            "courses": [
                {"course_id": "ECON F355", "code": "ECON F355", "title": "Business Analysis", "evidence_ids": ["EV-RANDOM"]},
                {"course_id": "CS F407", "code": "CS F407", "title": "Artificial Intelligence", "evidence_ids": ["EV-AI"]},
                {"course_id": "GS F232", "code": "GS F232", "title": "Introductory Psychology", "evidence_ids": ["EV-PSY"]},
                {"course_id": "GS F241", "code": "GS F241", "title": "Creative Writing", "evidence_ids": ["EV-WRITE"]},
            ],
            "evidence": [
                {"evidence_id": "EV-RANDOM", "excerpt": "something in a handout"},
                {"evidence_id": "EV-AI", "excerpt": "intelligent systems"},
                {"evidence_id": "EV-PSY", "excerpt": "human behavior"},
                {"evidence_id": "EV-WRITE", "excerpt": "writing workshop"},
            ],
        }
        result = _course_matches_from_staged(profile, "something interesting", staged)
        codes = [item["course_code"] for item in result["recommendations"]]
        self.assertEqual(codes, ["CS F407", "GS F232", "GS F241"])
        self.assertNotIn("ECON F355", codes)

    def test_admin_role_gate_and_indexed_source_id_path_safety(self):
        admin = self.client()
        self.register(admin, "admin@example.edu")
        self.assertIn("Source review", admin.open(self.base + "/").read().decode())

        student = self.client()
        self.register(student, "student@example.edu")
        with self.assertRaises(HTTPError) as caught:
            student.open(self.base + "/review")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

        response = admin.open(self.base + "/sources/DOC-test?page=1").read().decode()
        self.assertIn("Rules source text", response)
        with self.assertRaises(HTTPError) as outside:
            admin.open(self.base + "/sources/DOC-test/file")
        self.assertEqual(outside.exception.code, 404)
        outside.exception.close()
        with self.assertRaises(HTTPError) as arbitrary:
            admin.open(self.base + "/sources/../../private/file")
        self.assertEqual(arbitrary.exception.code, 404)
        arbitrary.exception.close()

    def test_attempt_editor_round_trips_import_identity_and_order(self):
        profile = StudentProfile(
            "profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1",
            attempts=(CourseAttempt(
                "CS F111", AttemptStatus.COMPLETED, "A", 4.0,
                attempt_id="marksheet-upload-0", term_id="FIRST SEMESTER 2025-2026",
                attempt_order=6076,
            ),),
        )
        self.assertEqual(_parse_attempt_text(_profile_attempt_text(profile)), profile.attempts)

    def test_admin_can_verify_one_evidence_page_without_promoting_other_claims(self):
        review = self.root / "review.json"
        review.write_text(json.dumps({
            "status": "needs_review",
            "snapshot": {
                "courses": [],
                "evidence": [
                    {"evidence_id": "EV-1", "verification_status": "needs_review"},
                    {"evidence_id": "EV-2", "verification_status": "needs_review"},
                ],
            },
        }), encoding="utf-8")
        admin = self.client()
        self.register(admin, "reviewer@example.edu")
        csrf = self.csrf(admin.open(self.base + "/").read().decode())
        response = admin.open(Request(self.base + "/review", data=urlencode({
            "csrf_token": csrf,
            "action": "verify_evidence",
            "evidence_page": "1",
            "reviewed_id": ["EV-1"],
            "verified_id": ["EV-1"],
        }, doseq=True).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}))
        self.assertEqual(response.status, 200)
        saved = json.loads(review.read_text(encoding="utf-8"))
        statuses = {item["evidence_id"]: item["verification_status"] for item in saved["snapshot"]["evidence"]}
        self.assertEqual(statuses, {"EV-1": "verified", "EV-2": "needs_review"})


if __name__ == "__main__":
    unittest.main()
