from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from recommender.ingestion.corpus import build_corpus, get_document, get_document_page, library_stats, search_corpus
from recommender.ingestion.documents import _extract_docx
from recommender.services.document_answers import answer_question


class LibraryFeatureTests(unittest.TestCase):
    def test_docx_tables_and_sections_are_logical_not_physical_pages(self) -> None:
        xml = '''<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Eligibility</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>CS F111</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Programming</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "guide.docx"
            with zipfile.ZipFile(file, "w") as archive:
                archive.writestr("word/document.xml", xml)
            pages, issues = _extract_docx(file)
        self.assertFalse(issues)
        self.assertIsNone(pages[0]["page_number"])
        self.assertIn("CS F111 | Programming", pages[0]["text"])
        self.assertEqual(pages[0]["section"], "Eligibility")

    def test_docx_corpus_retrieval_filters_and_extractive_answers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sources"
            root.mkdir()
            source = root / "guide.docx"
            source.write_bytes(b"docx-fake")
            db = Path(folder) / "library.sqlite"
            extracted = {"document": {"semester_scope": None}, "pages": [{"page_number": None, "section": "Eligibility", "text": "CS F111 eligibility requires a completed prerequisite course.", "verification_status": "needs_review"}]}
            with patch("recommender.ingestion.corpus.extract_document", return_value=extracted):
                report = build_corpus(root, db)
            self.assertEqual(report["discovered_documents"], 1)
            matches = search_corpus(db, "CS F111 prerequisite", document_type="reference")["matches"]
            self.assertEqual(len(matches), 1)
            self.assertIsNone(matches[0]["page"])
            self.assertEqual(matches[0]["section"], "Eligibility")
            self.assertEqual(search_corpus(db, "CS F111", semester="2026-27")["matches"], [])
            citation = answer_question(db, "CS F111 prerequisite")["citations"][0]
            answer = answer_question(db, "CS F111 prerequisite")
            self.assertEqual(answer["mode"], "extractive")
            self.assertIn("- CS F111 eligibility requires a completed prerequisite course. [1]", answer["answer"])
            self.assertNotIn("[CS F111]", answer["answer"])
            self.assertEqual(citation["document_id"], matches[0]["document_id"])
            doc = get_document(db, citation["document_id"])
            self.assertEqual(doc["primary_file_name"], "guide.docx")
            self.assertEqual(get_document_page(db, citation["document_id"])["section"], "Eligibility")
            self.assertEqual(library_stats(db)["documents"], 1)
            no_match = answer_question(db, "Zebra submarine")
            self.assertEqual(no_match["answer_status"], "not_found")

    def test_answer_uses_complete_relevant_passages_and_rejects_generic_or_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "sources"
            root.mkdir()
            (root / "guide.docx").write_bytes(b"guide")
            (root / "protocol.pdf").write_bytes(b"protocol")
            db = Path(folder) / "library.sqlite"

            def extraction(path, document_type):
                if path.name == "guide.docx":
                    pages = [
                        {"page_number": None, "section": "Dates", "text": "Registration deadline is 12 September.", "verification_status": "needs_review"},
                        {"page_number": None, "section": "Teleportation", "text": "Quantum teleportation transfers an unknown quantum state using shared entanglement and classical communication.", "verification_status": "needs_review"},
                    ]
                else:
                    pages = [{"page_number": 2, "section": "Protocol", "text": "The quantum teleportation protocol requires shared entanglement and classical communication.", "verification_status": "needs_review"}]
                return {"document": {"semester_scope": None}, "pages": pages}

            with patch("recommender.ingestion.corpus.extract_document", side_effect=extraction):
                build_corpus(root, db)
            result = answer_question(db, "What is quantum teleportation deadline?")
            self.assertEqual(result["answer_status"], "evidence_found")
            self.assertEqual(len(result["citations"]), 2)
            self.assertTrue(all("quantum teleportation" in citation["excerpt"].casefold() for citation in result["citations"]))
            self.assertTrue(all("deadline" not in citation["excerpt"].casefold() for citation in result["citations"]))
            self.assertIn("[1]", result["answer"])
            self.assertIn("[2]", result["answer"])
            self.assertEqual(result["citations"][0]["section"], "Teleportation")
            self.assertTrue(result["citations"][0]["unit_key"].startswith("logical-"))

            generic_root = Path(folder) / "generic"
            generic_root.mkdir()
            (generic_root / "dates.docx").write_bytes(b"dates")
            generic_db = Path(folder) / "generic.sqlite"
            with patch("recommender.ingestion.corpus.extract_document", return_value={"document": {}, "pages": [
                {"page_number": None, "section": "Dates", "text": "Registration deadline is 12 September.", "verification_status": "needs_review"},
            ]}):
                build_corpus(generic_root, generic_db)
            irrelevant = answer_question(generic_db, "What is quantum teleportation deadline?")
            self.assertEqual(irrelevant["answer_status"], "not_found")


if __name__ == "__main__":
    unittest.main()
