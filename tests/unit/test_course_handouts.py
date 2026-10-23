from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing

from recommender.services.course_handouts import course_handout_sources
from recommender.services.document_chat_agent import DocumentChatAgent
from tests.unit import test_document_chat_agent as chat_tests


class CourseHandoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.corpus = chat_tests.DocumentChatAgentTests().make_corpus(self.temp.name)
        with closing(sqlite3.connect(self.corpus)) as db:
            documents = (
                ("discrete", "CS_F222_handout.pdf", "handout", [
                    "Course No: CS F222\nCourse Title: Discrete Structures for Computer Science\nInstructor In Charge: Example Instructor\nDescription: graphs and colouring.",
                    "5. COURSE PLAN\nGraph Theory: connectivity, Euler and Hamiltonian graphs, planar graphs, graph colouring, matching, BFS, DFS, Prim, Kruskal, Dijkstra, Bellman Ford.",
                ]),
                ("other", "MATH_F213_handout.pdf", "handout", [
                    "Course No: MATH F213\nCourse Title: Discrete Mathematics\nGraphs, Ramsey theory and combinatorics.",
                ]),
                ("bulletin", "Bulletin.pdf", "bulletin", [
                    "Discrete Structures for Computer Science CS F222 graphs topics graphs taught graphs graphs graphs: a short catalogue overview.",
                ]),
            )
            for did, name, kind, pages in documents:
                db.execute("INSERT INTO documents VALUES(?,?,?,?,NULL,?,'v1','now')", (did, did, name, kind, len(pages)))
                for number, text in enumerate(pages, 1):
                    db.execute("INSERT INTO pages VALUES(?,?,?,?,?,NULL)", (did, number, str(number), text, "needs_review"))
                    db.execute("INSERT INTO pages_fts VALUES(?,?,?,?,NULL,NULL,?,?)", (did, number, name, kind, str(number), text))
            db.commit()

    def test_abbreviated_title_selects_matching_handout_and_detailed_plan(self):
        result = course_handout_sources(self.corpus, "in disc structure for computer science what topics under graphs are tsaught")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["course"]["code"], "CS F222")
        self.assertEqual(result["citations"][0]["page"], 2)
        self.assertIn("Bellman Ford", result["citations"][0]["excerpt"])
        self.assertEqual({source["document_id"] for source in result["citations"]}, {"discrete"})

    def test_chat_passes_only_matching_handout_to_model(self):
        client = chat_tests.FakeClient("The handout covers BFS and DFS. [1]", "documents", [1])
        answer = DocumentChatAgent(client, self.corpus).answer("What graph topics are taught in CS F222?")
        payload = json.loads(client.messages[0][1]["content"])
        self.assertEqual(payload["course_handout_selection"]["course"]["code"], "CS F222")
        self.assertTrue(all(source["file_name"] == "CS_F222_handout.pdf" for source in payload["repository_sources"]))
        self.assertEqual(answer["citations"][0]["page"], 2)

    def test_followup_uses_previous_course_but_new_code_overrides_it(self):
        result = course_handout_sources(self.corpus, "What graph topics are taught?", "Tell me about CS F222")
        self.assertEqual(result["course"]["code"], "CS F222")
        result = course_handout_sources(self.corpus, "What topics are taught in MATH F213?", "Tell me about CS F222")
        self.assertEqual(result["course"]["code"], "MATH F213")

    def test_unknown_code_never_uses_bulletin_or_previous_course(self):
        result = course_handout_sources(self.corpus, "What topics are taught in CS F888?", "Tell me about CS F222")
        self.assertEqual(result, {"status": "not_found", "citations": []})

    def test_explicit_bulletin_and_university_policy_use_library_search(self):
        self.assertIsNone(course_handout_sources(self.corpus, "According to the bulletin, what topics are in CS F222?"))
        self.assertIsNone(course_handout_sources(self.corpus, "What does the bulletin say about CS F222?"))
        self.assertIsNone(course_handout_sources(self.corpus, "What is the timetable for CS F222?"))
        self.assertIsNone(course_handout_sources(self.corpus, "How many courses are needed for graduation?"))

    def test_instructor_question_uses_handout_metadata_page(self):
        result = course_handout_sources(self.corpus, "Who teaches CS F222?")
        self.assertEqual(result["citations"][0]["page"], 1)

    def test_new_unknown_course_name_does_not_reuse_previous_course(self):
        result = course_handout_sources(self.corpus, "What topics are taught in biology?", "Tell me about CS F222")
        self.assertEqual(result["status"], "not_found")

    def test_followup_can_reject_the_bulletin(self):
        result = course_handout_sources(self.corpus, "not the bulletin", "What topics are taught in CS F222?")
        self.assertEqual(result["course"]["code"], "CS F222")

    def test_ambiguous_offering_requests_clarification(self):
        with closing(sqlite3.connect(self.corpus)) as db:
            db.execute("INSERT INTO documents VALUES('duplicate','duplicate','CS_F222_other_term.pdf','handout','2025-T1',1,'v1','now')")
            db.execute("INSERT INTO pages VALUES('duplicate',1,'1','Course No: CS F222\nCourse Title: Discrete Structures for Computer Science','needs_review',NULL)")
            db.commit()
        result = course_handout_sources(self.corpus, "What graph topics are taught in CS F222?")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["citations"], [])


if __name__ == "__main__":
    unittest.main()
