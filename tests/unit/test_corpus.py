from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recommender.ingestion.corpus import CorpusError, build_corpus, search_corpus


def extracted(text: str) -> dict[str, list[dict[str, object]]]:
    return {"pages": [{"page_number": 1, "text": text, "verification_status": "needs_review"}]}


class CorpusTests(unittest.TestCase):
    def test_uppercase_pdf_discovery_and_page_citation_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            (root / "Ordinary Handout.PDF").write_bytes(b"handout-pdf")
            database = Path(directory) / "corpus.sqlite"
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted("Project evaluation uses a final report.")):
                result = build_corpus(root, database)
            self.assertEqual(result["discovered_pdfs"], 1)
            self.assertEqual(result["source_pdfs"], 1)
            matches = search_corpus(database, "final report")["matches"]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["page"], 1)
            self.assertIn("Ordinary Handout.PDF", matches[0]["source_paths"][0])

    def test_excludes_performance_sheets_by_content_and_alias_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            (root / "renamed-document.PDF").write_bytes(b"performance-sheet")
            (root / "ordinary-handout.pdf").write_bytes(b"ordinary-handout")
            database = Path(directory) / "corpus.sqlite"

            def extract(path: Path, document_type: str) -> dict[str, list[dict[str, object]]]:
                if path.name == "renamed-document.PDF":
                    return extracted("Performance Sheet\nCompleted Courses\nRegistered Courses\nStudent Name: Private")
                return extracted("A handout explains course assessment and project work.")

            with patch("recommender.ingestion.corpus.extract_document", side_effect=extract):
                result = build_corpus(root, database)
            self.assertEqual(result["excluded_runtime_profiles"], 1)
            self.assertEqual(result["source_pdfs"], 1)
            self.assertEqual(search_corpus(database, "Private")["matches"], [])

    def test_explicit_transcript_title_and_course_no_table_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            (root / "renamed.pdf").write_bytes(b"transcript")
            database = Path(directory) / "corpus.sqlite"
            with patch(
                "recommender.ingestion.corpus.extract_document",
                return_value=extracted(
                    "Academic Transcript\nStudent Name: Private\nCourse No. Course Title Grade\nABC123 Intro A"
                ),
            ):
                with self.assertRaises(CorpusError):
                    build_corpus(root, database)
            self.assertFalse(database.exists())

    def test_excludes_every_alias_when_any_filename_is_private_or_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            data = b"same-private-pdf"
            (root / "anonymous.pdf").write_bytes(data)
            (root / "student-transcript.pdf").write_bytes(data)
            ignored = b"excluded-report"
            (root / "renamed-report.pdf").write_bytes(ignored)
            (root / "bit_acad_rpt.pdf").write_bytes(ignored)
            (root / "handout.pdf").write_bytes(b"ordinary")
            database = Path(directory) / "corpus.sqlite"
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted("Ordinary course handout.")):
                result = build_corpus(root, database)
            self.assertEqual(result["excluded_runtime_profiles"], 2)
            self.assertEqual(result["excluded_ignored_sources"], 2)
            self.assertEqual(result["source_pdfs"], 1)
            self.assertEqual(result["unique_documents"], 1)

    def test_reference_with_identity_course_and_grade_words_needs_transcript_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            (root / "course.pdf").write_bytes(b"ordinary-course")
            database = Path(directory) / "corpus.sqlite"
            reference = """Reference material on examinations and results.
Student identity requirements are listed here.
Course No. appears in the sample form; grade definitions follow in this section.
The academic transcript may list a student's course history and grades.
Course Code: ABC123, Grade: A is an example in the handout."""
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted(reference)):
                result = build_corpus(root, database)
            self.assertEqual(result["source_pdfs"], 1)
            self.assertEqual(result["excluded_runtime_profiles"], 0)

    def test_category_wise_report_is_counted_ignored_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            (root / "renamed.pdf").write_bytes(b"category-report")
            (root / "handout.pdf").write_bytes(b"valid-handout")
            database = Path(directory) / "corpus.sqlite"

            def extract(path: Path, document_type: str) -> dict[str, list[dict[str, object]]]:
                if path.name == "renamed.pdf":
                    return extracted("Category-Wise Academic Structure for B.E.\nStudent ID No.\nStudent Name")
                return extracted("Ordinary handout content.")

            with patch(
                "recommender.ingestion.corpus.extract_document",
                side_effect=extract,
            ):
                result = build_corpus(root, database)
            self.assertEqual(result["excluded_ignored_sources"], 1)
            self.assertEqual(result["excluded_runtime_profiles"], 0)
            self.assertEqual(result["source_pdfs"], 1)

    def test_empty_rebuild_preserves_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            pdf = root / "course.pdf"
            pdf.write_bytes(b"handout")
            database = Path(directory) / "corpus.sqlite"
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted("original searchable content")):
                build_corpus(root, database)
            
            
            pdf.write_bytes(b"changed profile document")
            with patch(
                "recommender.ingestion.corpus.extract_document",
                return_value=extracted("Performance Sheet\nCompleted Courses\nRegistered Courses"),
            ):
                with self.assertRaises(CorpusError):
                    build_corpus(root, database)
            self.assertEqual(len(search_corpus(database, "original content")["matches"]), 1)

    def test_extraction_failure_preserves_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            first_pdf = root / "first.pdf"
            first_pdf.write_bytes(b"first")
            database = Path(directory) / "corpus.sqlite"
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted("original searchable content")):
                build_corpus(root, database)
            first_pdf.unlink()
            (root / "failure.pdf").write_bytes(b"failure")
            with patch("recommender.ingestion.corpus.extract_document", side_effect=RuntimeError("extraction failed")):
                with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                    build_corpus(root, database)
            self.assertEqual(len(search_corpus(database, "original content")["matches"]), 1)
            self.assertFalse(list(Path(directory).glob("corpus.sqlite.*.tmp")))

    def test_invalid_database_is_reported_as_corpus_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bad.sqlite"
            database.write_text("not a sqlite database", encoding="utf-8")
            with self.assertRaises(CorpusError):
                search_corpus(database, "course")


if __name__ == "__main__":
    unittest.main()
