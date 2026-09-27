from __future__ import annotations

import io
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app.dashboard import _values_from_profile, make_handler, render_result
from recommender.ingestion.snapshots import load_snapshot
from recommender.models import AttemptStatus, CourseAttempt, StudentProfile
from recommender.storage.profiles import ProfileRepository


ROOT = Path(__file__).resolve().parents[2]


class _OpenBytesIO(io.BytesIO):
    def close(self) -> None:
        pass


class _FakeSocket:
    def __init__(self, request: bytes) -> None:
        self.request = io.BytesIO(request)
        self.response = _OpenBytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        return self.request if "r" in mode else self.response

    def sendall(self, data: bytes) -> None:
        self.response.write(data)


class _Server:
    server_name = "localhost"
    server_port = 80


def post(handler, body: bytes, *, path: str = "/", content_type: str = "application/x-www-form-urlencoded", length: str | None = None) -> tuple[int, str]:
    declared_length = str(len(body)) if length is None else length
    request = (
        f"POST {path} HTTP/1.0\r\nContent-Type: {content_type}\r\nContent-Length: {declared_length}\r\n\r\n".encode("ascii")
        + body
    )
    sock = _FakeSocket(request)
    handler(sock, ("127.0.0.1", 12345), _Server())
    response = sock.response.getvalue()
    headers, payload = response.split(b"\r\n\r\n", 1)
    status = int(headers.split(b" ", 2)[1])
    return status, payload.decode("utf-8")


def profile(*attempts: CourseAttempt, profile_id: str = "student") -> StudentProfile:
    return StudentProfile(profile_id, "SYNTHETIC", 2025, ("BSC-SYNTH",), 3, "SYN-2026-T1", attempts=attempts)


class DashboardHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = load_snapshot(ROOT / "data/synthetic/demo_snapshot.json")

    def setUp(self) -> None:
        self.profiles = ProfileRepository(":memory:")
        self.handler = make_handler(self.snapshot, self.profiles, ROOT / "missing-corpus.sqlite")

    def tearDown(self) -> None:
        self.profiles.close()

    def _form_body(self, saved: StudentProfile, **updates: str) -> bytes:
        values = _values_from_profile(saved, "Suggest DELs related to AI.")
        values.update(updates)
        return urlencode(values).encode("utf-8")

    def test_post_preserves_attempt_metadata_and_uneditable_failed_history(self) -> None:
        saved = self.profiles.save(profile(
            CourseAttempt("SYN-100", AttemptStatus.COMPLETED, grade="A", units_awarded=3),
            CourseAttempt("SYN-200", AttemptStatus.IN_PROGRESS, grade="B", units_awarded=2),
            CourseAttempt("OLD-FAILED", AttemptStatus.FAILED, grade="F", units_awarded=0),
            CourseAttempt("OLD-WITHDRAWN", AttemptStatus.WITHDRAWN),
        ), expected_version=0)
        status, response = post(self.handler, self._form_body(saved))
        self.assertEqual(status, 200)
        loaded = self.profiles.get("student")
        attempts = {item.course_id: item for item in loaded.attempts}
        self.assertEqual(attempts["SYN-100"], CourseAttempt("SYN-100", AttemptStatus.COMPLETED, "A", 3))
        self.assertEqual(attempts["SYN-200"], CourseAttempt("SYN-200", AttemptStatus.IN_PROGRESS, "B", 2))
        self.assertEqual(attempts["OLD-FAILED"], CourseAttempt("OLD-FAILED", AttemptStatus.FAILED, "F", 0))
        self.assertEqual(attempts["OLD-WITHDRAWN"].status, AttemptStatus.WITHDRAWN)
        self.assertIn("Saved profile version 2", response)

    def test_intentional_status_change_clears_stale_attempt_metadata(self) -> None:
        saved = self.profiles.save(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, grade="A", units_awarded=3)), expected_version=0)
        status, _ = post(self.handler, self._form_body(saved, completed="", current="SYN-100"))
        self.assertEqual(status, 200)
        self.assertEqual(self.profiles.get("student").attempts, (CourseAttempt("SYN-100", AttemptStatus.IN_PROGRESS),))

    def test_invalid_requests_never_write_and_unknown_post_path_is_404(self) -> None:
        saved = self.profiles.save(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, grade="A")), expected_version=0)
        valid = self._form_body(saved)
        malformed = [
            (valid, "/", "application/json", None, 415),
            (valid, "/", "application/x-www-form-urlencoded", "bad", 400),
            (valid, "/", "application/x-www-form-urlencoded", "0", 400),
            (valid, "/", "application/x-www-form-urlencoded", "-1", 400),
            (valid, "/", "application/x-www-form-urlencoded", str(64 * 1024 + 1), 413),
            (b"\xff", "/", "application/x-www-form-urlencoded", None, 400),
            (valid, "/elsewhere", "application/x-www-form-urlencoded", None, 404),
        ]
        for body, path, content_type, length, expected_status in malformed:
            with self.subTest(expected_status=expected_status, path=path, length=length):
                status, _ = post(self.handler, body, path=path, content_type=content_type, length=length)
                self.assertEqual(status, expected_status)
                self.assertEqual(self.profiles.get("student").profile_version, 1)
        missing_query = _values_from_profile(saved, "")
        missing_query.pop("query")
        status, _ = post(self.handler, urlencode(missing_query).encode())
        self.assertEqual(status, 400)
        self.assertEqual(self.profiles.get("student").profile_version, 1)

    def test_stale_form_version_is_rejected_after_loading_current_attempts(self) -> None:
        saved = self.profiles.save(profile(CourseAttempt("SYN-100", AttemptStatus.COMPLETED, grade="A")), expected_version=0)
        self.profiles.save(saved, expected_version=1)
        status, response = post(self.handler, self._form_body(saved))
        self.assertEqual(status, 200)
        self.assertIn("changed from version 1 to 2", response)
        self.assertEqual(self.profiles.get("student").profile_version, 2)

    def test_save_notice_is_shown_when_policy_resolution_fails(self) -> None:
        body = urlencode({
            "profile_id": "unmatched", "profile_version": "0", "campus": "SYNTHETIC",
            "admission_year": "2025", "programmes": "UNKNOWN", "current_semester": "3",
            "target_semester": "SYN-2026-T1", "completed": "", "current": "", "minor": "",
            "interests": "", "query": "Suggest DELs related to AI.",
        }).encode()
        status, response = post(self.handler, body)
        self.assertEqual(status, 200)
        self.assertEqual(self.profiles.get("unmatched").profile_version, 1)
        self.assertIn("Profile saved; recommendations unavailable", response)
        self.assertIn("Saved profile version 1", response)

    def test_none_contribution_has_accurate_rendering(self) -> None:
        result = {
            "requirement_summary": [], "interpreted_query": {"requested_category": "DEL", "topic_preferences": []},
            "clarification_questions": [], "recommendations": [{
                "eligibility_status": "pass", "eligibility_reasons": [], "requirement_contribution": None,
                "course_code": "X 1", "title": "Course", "matched_topics": [], "evidence_references": [],
            }], "unverified_alternatives": [], "no_result_reason": None,
        }
        self.assertIn("No verified requirement contribution", render_result(result))


if __name__ == "__main__":
    unittest.main()
