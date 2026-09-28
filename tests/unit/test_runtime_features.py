from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from recommender.models import Course, DatasetSnapshot
from recommender.services import marksheet_import
from recommender.services.marksheet_import import MarksheetImportError, confirmed_attempts, preview_marksheet
from recommender.storage.accounts import AccountError, AccountRepository


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_version="test", synthetic=True, documents=(), evidence={},
        courses={
            "cs101": Course("cs101", "CS F101", "Intro", 3, (), ()),
            "variant": Course("variant", "BITS F101-2", "Social Conduct", 0.5, (), ()),
        },
        offerings=(), policy_contexts=(),
    )


class MarksheetImportTests(unittest.TestCase):
    def test_preview_maps_known_rows_and_never_infers_grade_status(self) -> None:
        def fake_parse(path):
            self.assertTrue(Path(path).exists())
            return {
                "report_type": "generic_marksheet",
                "attempt_candidates": [
                    {"course_code": "CS F101", "canonical_course_code": "CS F101", "grade": "A", "units_candidate": 3.0, "attempt_status": "unresolved", "term": "FIRST SEMESTER 2025-2026"},
                    {"course_code": "ZZ F123", "canonical_course_code": "ZZ F123", "grade": "B", "units_candidate": 4.0, "attempt_status": "unresolved"},
                    {"course_code": "BITS F101-2", "canonical_course_code": "BITS F101", "course_variant": "2", "grade": "A", "units_candidate": 0.5, "attempt_status": "unresolved"},
                ], "issues": [],
            }
        with patch.object(marksheet_import, "parse_marksheet", side_effect=fake_parse):
            review = preview_marksheet(b"%PDF-1.4 test", "marksheet.pdf", _snapshot())
        self.assertEqual([row["index"] for row in review["attempt_candidates"]], [0, 1, 2])
        self.assertEqual(review["attempt_candidates"][0]["course_id"], "cs101")
        self.assertEqual(review["attempt_candidates"][0]["status"], "unresolved")
        self.assertEqual(review["attempt_candidates"][1]["course_mapping_status"], "unknown")
        self.assertFalse(review["attempt_candidates"][1]["selected"])
        self.assertEqual(review["attempt_candidates"][2]["course_id"], "variant")
        self.assertEqual(review["attempt_candidates"][0]["attempt_order"], 2025 * 3 + 1)
        self.assertNotEqual(review["attempt_candidates"][0]["attempt_id"], review["attempt_candidates"][2]["attempt_id"])

    def test_preview_rejects_non_pdf_and_oversized_payload(self) -> None:
        with self.assertRaises(MarksheetImportError):
            preview_marksheet(b"not pdf", "marksheet.pdf", _snapshot())
        with self.assertRaises(MarksheetImportError):
            preview_marksheet(b"%PDF-" + b"x" * (10 * 1024 * 1024), "marksheet.pdf", _snapshot())

    def test_confirmation_requires_selection_unknown_consent_status_and_order(self) -> None:
        review = {"attempt_candidates": [
            {"index": 0, "course_id": "cs101", "course_mapping_status": "mapped", "grade": "A", "units": 3, "term": "2025-1", "status": "unresolved", "selected": True},
            {"index": 1, "canonical_course_code": "ZZ F123", "course_mapping_status": "unknown", "grade": "B", "units": 4, "term": "2025-2", "selected": False},
        ]}
        with self.assertRaises(MarksheetImportError):
            confirmed_attempts(review, [{"index": 0}])
        with self.assertRaises(MarksheetImportError):
            confirmed_attempts(review, [{"index": 1, "selected": True, "status": "completed", "course_id": "arbitrary", "allow_unknown": True}])
        with self.assertRaises(MarksheetImportError):
            confirmed_attempts(review, [{"index": 0, "selected": True, "status": "completed", "term_id": "some term"}])
        result = confirmed_attempts(review, [
            {"index": 0, "selected": True, "status": "completed", "grade": "b+", "units": "4.0", "term_id": "2025-1"},
            {"index": 1, "selected": True, "status": "failed", "allow_unknown": True, "term_id": "2025-2"},
        ])
        self.assertEqual(result[0].course_id, "cs101")
        self.assertEqual((result[0].grade, result[0].units_awarded), ("B+", 4.0))
        self.assertEqual(result[0].attempt_order, 2025 * 3 + 1)
        self.assertEqual(result[1].course_id, "ZZ F123")
        self.assertEqual(result[1].attempt_order, 2025 * 3 + 2)

    def test_repeated_attempts_need_terms_and_selected_unknown_default_stays_excluded(self) -> None:
        review = {"attempt_candidates": [
            {"index": 0, "course_id": "cs101", "course_mapping_status": "mapped", "selected": True},
            {"index": 1, "course_id": "cs101", "course_mapping_status": "mapped", "selected": True},
            {"index": 2, "canonical_course_code": "ZZ F123", "course_mapping_status": "unknown", "selected": False},
        ]}
        with self.assertRaisesRegex(MarksheetImportError, "term"):
            confirmed_attempts(review, [
                {"index": 0, "selected": True, "status": "completed", "attempt_order": 1},
                {"index": 1, "selected": True, "status": "completed", "attempt_order": 2},
            ])
        self.assertEqual(confirmed_attempts(review, [{"index": 2, "status": "completed", "allow_unknown": True}]), ())


class AccountRepositoryTests(unittest.TestCase):
    def test_accounts_hash_passwords_issue_sessions_and_enforce_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = AccountRepository(Path(directory) / "accounts.sqlite")
            user_id, profile_id = repo.register("First@example.edu", "long password")
            other_user, other_profile = repo.register("second@example.edu", "another pass")
            token, session = repo.login("FIRST@example.edu", "long password")
            self.assertEqual((session["user_id"], session["profile_id"], session["role"]), (user_id, profile_id, "admin"))
            
            self.assertNotIn("csrf_token", repo.get_session(token))
            with repo.lock:
                row = repo.connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertNotIn("csrf_token", row.keys())
            self.assertTrue(repo.verify_csrf(token, session["csrf_token"]))
            self.assertFalse(repo.verify_csrf(token, "wrong"))
            self.assertTrue(repo.owns_profile(user_id, profile_id))
            self.assertFalse(repo.owns_profile(user_id, other_profile))
            self.assertFalse(repo.session_for_profile(token, other_profile))
            self.assertTrue(repo.session_for_profile(token, profile_id))
            self.assertEqual(repo.get_session(token)["role"], "admin")
            student_token, student = repo.login("second@example.edu", "another pass")
            self.assertEqual((student["role"], student["user_id"]), ("student", other_user))
            with self.assertRaises(AccountError):
                repo.login("first@example.edu", "wrong password")
            repo.logout(token)
            self.assertIsNone(repo.get_session(token))
            repo.close()

    def test_expired_session_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = AccountRepository(Path(directory) / "accounts.sqlite")
            repo.register("person@example.edu", "password123")
            token, _ = repo.login("person@example.edu", "password123")
            repo.connection.execute("UPDATE account_sessions SET expires_at=?", (time.time() - 1,))
            repo.connection.commit()
            self.assertIsNone(repo.get_session(token))
            repo.close()

    def test_expired_sessions_are_swept_periodically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = AccountRepository(Path(directory) / "accounts.sqlite")
            repo.register("sweep@example.edu", "password123")
            token, _ = repo.login("sweep@example.edu", "password123")
            repo.connection.execute("UPDATE account_sessions SET expires_at=?", (time.time() - 10,))
            repo.connection.commit()
            
            repo._last_sweep = 0.0
            self.assertIsNone(repo.get_session(token))
            with repo.lock:
                remaining = repo.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0]
            self.assertEqual(remaining, 0)
            repo.close()

    def test_login_rate_limiting_locks_after_repeated_failures(self) -> None:
        from recommender.storage.accounts import AccountLockedError, MAX_FAILED_LOGINS
        with tempfile.TemporaryDirectory() as directory:
            repo = AccountRepository(Path(directory) / "accounts.sqlite")
            repo.register("target@example.edu", "correct password")
            for _ in range(MAX_FAILED_LOGINS):
                with self.assertRaises(AccountError):
                    repo.login("target@example.edu", "wrong password")
            with self.assertRaises(AccountLockedError):
                repo.login("target@example.edu", "correct password")
            
            repo.connection.execute("UPDATE login_attempts SET locked_until=0")
            repo.connection.commit()
            token, _ = repo.login("target@example.edu", "correct password")
            self.assertTrue(repo.get_session(token))
            repo.close()

    def test_recommendation_history_persists_and_can_be_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite"
            repo = AccountRepository(database)
            user_id, _ = repo.register("person@example.edu", "password123")
            repo.add_recommendation(user_id, {
                "query": "AI courses",
                "recommendations": [{"course_code": "CS F407", "title": "Artificial Intelligence", "reason": "AI"}],
                "no_result_reason": "",
            })
            repo.close()
            reopened = AccountRepository(database)
            history = reopened.recommendation_history(user_id)
            self.assertEqual(history[0]["query"], "AI courses")
            self.assertEqual(history[0]["recommendations"][0]["course_code"], "CS F407")
            reopened.clear_recommendation_history(user_id)
            self.assertEqual(reopened.recommendation_history(user_id), [])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
