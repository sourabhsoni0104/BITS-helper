from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from recommender.ingestion.corpus import build_corpus
from recommender.models import StudentProfile
from recommender.services.course_discovery import discover_courses


class CourseDiscoveryTests(unittest.TestCase):
    def test_full_indexed_handout_text_drives_matching(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sources"
            root.mkdir()
            source = root / "course-handout.pdf"
            source.write_bytes(b"pdf")
            database = Path(folder) / "corpus.sqlite"
            extracted = {
                "document": {"semester_scope": "2026-T1"},
                "pages": [
                    {"page_number": 1, "text": "Course title: Decision Models", "verification_status": "needs_review"},
                    {"page_number": 2, "text": "The course applies stochastic optimization to supply networks.", "verification_status": "needs_review"},
                ],
            }
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted):
                build_corpus(root, database)
            with closing(sqlite3.connect(database)) as connection:
                document_id = connection.execute("SELECT document_id FROM documents").fetchone()[0]
            snapshot = {
                "courses": [{
                    "course_id": "BITS F999",
                    "code": "BITS F999",
                    "title": "Decision Models",
                    "evidence_ids": ["EV-1"],
                }],
                "evidence": [{
                    "evidence_id": "EV-1",
                    "document_id": document_id,
                    "excerpt": "Course title: Decision Models",
                }],
            }
            profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")
            result = discover_courses(
                profile,
                "stochastic optimization",
                snapshot,
                corpus_path=database,
            )
        self.assertEqual([item["course_code"] for item in result["recommendations"]], ["BITS F999"])
        self.assertIn("stochastic", result["recommendations"][0]["matched_topics"][0])

    def test_admission_component_filter_and_document_deduplication(self) -> None:
        snapshot = {
            "courses": [
                {"course_id": "CS F407", "code": "CS F407", "title": "Artificial Intelligence", "evidence_ids": ["EV-SHARED"]},
                {"course_id": "CS U407", "code": "CS U407", "title": "Artificial Intelligence", "evidence_ids": ["EV-SHARED"]},
                {"course_id": "NEW F100", "code": "NEW F100", "title": "Artificial Intelligence Studio", "evidence_ids": ["EV-NEW"]},
            ],
            "offerings": [
                {"course_id": "CS F407", "campus": "Pilani", "semester_id": "2026-T1", "component_code": "1333", "evidence_ids": ["EV-SHARED"]},
                {"course_id": "CS U407", "campus": "Pilani", "semester_id": "2026-T1", "component_code": "5333", "evidence_ids": ["EV-SHARED"]},
                {"course_id": "NEW F100", "campus": "Pilani", "semester_id": "2026-T1", "component_code": "6001", "evidence_ids": ["EV-NEW"]},
            ],
            "evidence": [
                {"evidence_id": "EV-SHARED", "document_id": "DOC-SHARED", "excerpt": "Artificial intelligence and machine learning."},
                {"evidence_id": "EV-NEW", "document_id": "DOC-NEW", "excerpt": "Artificial intelligence studio."},
            ],
        }
        profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")
        result = discover_courses(profile, "AI", snapshot)
        self.assertEqual([item["course_code"] for item in result["recommendations"]], ["CS F407"])

    def test_ai_ignores_incidental_policy_mentions_and_query_filler(self) -> None:
        snapshot = {
            "courses": [
                {"course_id": "AI-1", "code": "CS F425", "title": "Deep Learning", "evidence_ids": ["EV-AI"]},
                {"course_id": "ECON-1", "code": "ECON F211", "title": "Principles of Economics", "evidence_ids": ["EV-ECON"]},
            ],
            "evidence": [
                {"evidence_id": "EV-AI", "document_id": "DOC-AI", "excerpt": "Neural networks and machine learning methods."},
                {"evidence_id": "EV-ECON", "document_id": "DOC-ECON", "excerpt": "Students must follow the generic AI use policy."},
            ],
        }
        profile = StudentProfile("profile", "Pilani", 2025, ("BE-CS",), 3, "2026-T1")
        result = discover_courses(profile, "Teach me something interesting about AI", snapshot)
        self.assertEqual([item["course_code"] for item in result["recommendations"]], ["CS F425"])


if __name__ == "__main__":
    unittest.main()
