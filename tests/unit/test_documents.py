from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recommender.ingestion.documents import extract_document, inventory_sources, write_json_atomic
from recommender.ingestion.handouts import HandoutScrapeError, parse_handout_index
from recommender.ingestion.marksheets import MarksheetParseError, parse_marksheet


class DocumentIngestionTests(unittest.TestCase):
    def test_inventory_does_not_treat_filename_guess_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "academic_regulations.txt").write_text("Synthetic input", encoding="utf-8")
            report = inventory_sources(root)
            self.assertEqual(report["files"][0]["candidate_document_type"], "regulations")
            self.assertEqual(report["files"][0]["classification_status"], "needs_review")
            self.assertIn("handout", report["classes_without_candidates"])

    def test_inventory_uses_a_handout_collection_directory_as_a_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handouts = root / "handouts"
            handouts.mkdir()
            (handouts / "CS_F111_123.pdf").write_bytes(b"%PDF-synthetic")
            report = inventory_sources(root)
            self.assertEqual(report["files"][0]["candidate_document_type"], "handout")
            self.assertIn("handout", report["candidate_classes_present"])
            self.assertEqual(report["runtime_input_classes"], ["profile_history"])
            self.assertNotIn("profile_history", report["classes_without_candidates"])

    def test_text_extraction_preserves_logical_pages_and_unknown_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "handout.txt"
            source.write_text("First page\fSecond page", encoding="utf-8")
            result = extract_document(source, "handout", semester_scope="SYN-TERM")
            self.assertEqual([page["page_number"] for page in result["pages"]], [1, 2])
            self.assertEqual(result["pages"][1]["text"], "Second page")
            self.assertEqual(result["document"]["status"], "review_needed")
            self.assertIn("UNKNOWN_SCOPE", [issue["code"] for issue in result["issues"]])

    def test_atomic_report_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested/report.json"
            write_json_atomic({"ok": True}, destination)
            self.assertIn('"ok": true', destination.read_text(encoding="utf-8"))

    def test_saved_handout_index_is_parsed_with_metadata(self) -> None:
        html = """
        <table><tr><td>1</td><td>2423</td><td>AN F314</td><td>Introduction to Flight</td>
        <td>31-Jul-2026 10:14 AM</td><td><a href="https://academic.bits-pilani.ac.in/Faculty/Course_Handouts/Handout_Files/Current_Handouts/AN_F314_2423_test.pdf">Download Handout</a></td></tr></table>
        """
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "index.html"
            source.write_text(html, encoding="utf-8")
            entries = parse_handout_index(source)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].course_code, "AN F314")
        self.assertEqual(entries[0].component_code, "2423")
        self.assertEqual(entries[0].filename, "AN_F314_2423_test.pdf")

    def test_handout_index_rejects_unapproved_hosts(self) -> None:
        html = '<table><tr><td>1</td><td>x</td><td>X F100</td><td>X</td><td>today</td><td><a href="https://example.com/file.pdf">Download</a></td></tr></table>'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "index.html"
            source.write_text(html, encoding="utf-8")
            with self.assertRaises(HandoutScrapeError):
                parse_handout_index(source)

    def test_marksheet_parser_preserves_grade_uncertainty(self) -> None:
        text = "Course Grade\nCS F111 Computer Programming 4 A\fMATH F111 Mathematics I PASSED"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "marksheet.txt"
            source.write_text(text, encoding="utf-8")
            result = parse_marksheet(source)
        self.assertEqual(len(result["attempt_candidates"]), 2)
        self.assertEqual(result["attempt_candidates"][0]["course_code"], "CS F111")
        self.assertEqual(result["attempt_candidates"][0]["grade"], "A")
        self.assertEqual(result["attempt_candidates"][0]["attempt_status"], "unresolved")
        self.assertEqual(result["attempt_candidates"][1]["attempt_status"], "completed")
        self.assertIn("GRADE_SEMANTICS_UNRESOLVED", [issue["code"] for issue in result["issues"]])

    def test_performance_sheet_splits_columns_and_excludes_pending_courses(self) -> None:
        text = """Performance Sheet
Student ID: 2025A7PS0001P   ERP ID: 111   Status: Normal
Name: TEST STUDENT                                      CGPA: 8.50
Completed Courses/Registered Courses
Academic Year 2025 - 2026
FIRST SEMESTER 2025-2026                                                   SECOND SEMESTER 2025-2026
Course No. Course Title Units Grade Tag Course No. Course Title Units Grade Tag
BIO F101      INTRO TO BIO SCI                 3.0 B                       BITS F101-2    SOCIAL CONDUCT                  0.5 A
Academic Year 2026 - 2027
FIRST SEMESTER 2026-2027
CS F213       OBJECT ORIENTED PROG             4.0
Pending Courses (To be eligible for graduation)
Summer Term
Course No. Course Title Units
BITS F221     PRACTICE SCHOOL I                5.0
"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "performance.txt"
            source.write_text(text, encoding="utf-8")
            result = parse_marksheet(source)
        self.assertEqual(result["report_type"], "performance_sheet")
        self.assertEqual(result["profile_candidates"]["student_name"], "TEST STUDENT")
        self.assertEqual([item["course_code"] for item in result["attempt_candidates"]], ["BIO F101", "BITS F101-2", "CS F213"])
        self.assertEqual(result["attempt_candidates"][0]["grade"], "B")
        self.assertEqual(result["attempt_candidates"][1]["grade"], "A")
        self.assertEqual(result["attempt_candidates"][0]["attempt_status"], "completed")
        self.assertEqual(result["attempt_candidates"][1]["attempt_status"], "completed")
        self.assertEqual(result["attempt_candidates"][2]["attempt_status"], "in_progress")
        self.assertEqual([item["course_code"] for item in result["pending_course_candidates"]], ["BITS F221"])
        self.assertEqual(result["pending_course_candidates"][0]["planned_group"], "Summer Term")

    def test_academic_structure_is_not_a_supported_profile_input(self) -> None:
        text = """Category-Wise Academic Structure for B.E.(Computer Science) with Practice School
Student ID No.                                     2025A7PS0001P
Student Name                                       Test Student
(I) General Institutional Requirement
Engineering Foundation 2 0 2 6 0 6
Sub-Total 2 0 2 6 0 6
"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "academic-report.txt"
            source.write_text(text, encoding="utf-8")
            with self.assertRaises(MarksheetParseError):
                parse_marksheet(source)

    def test_inventory_explicitly_ignores_repository_category_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "BIT_ACAD_RPT.pdf"
            source.write_bytes(b"%PDF-synthetic")
            report = inventory_sources(directory)
        self.assertEqual(report["ignored_files"], [str(source)])
        self.assertEqual(report["files"][0]["classification_status"], "ignored")
        self.assertIsNone(report["files"][0]["candidate_document_type"])


if __name__ == "__main__":
    unittest.main()
