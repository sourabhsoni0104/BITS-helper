from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing

from recommender.services.document_chat_agent import DocumentChatAgent
from recommender.services.document_sources import select_document_sources, source_policy
from tests.unit import test_course_handouts as fixtures
from tests.unit import test_document_chat_agent as chat_tests


class DocumentSourceTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CourseHandoutTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.corpus = fixture.corpus
        with closing(sqlite3.connect(self.corpus)) as db:
            for did, name, kind, text in (
                ("rules", "Academic-Regulations.pdf", "regulations", "The minimum CGPA required for graduation is documented in the academic regulations."),
                ("schedule", "Timetable.pdf", "timetable", "CS F222 Discrete Structures for Computer Science lecture timetable: Monday in room 101."),
                ("joining", "Thesis-Joining-FAQ.pdf", "reference", "To submit an off-campus thesis joining report, complete the joining form and send it to the division."),
            ):
                db.execute("INSERT INTO documents VALUES(?,?,?,?,NULL,1,'v1','now')", (did, did, name, kind))
                db.execute("INSERT INTO pages VALUES(?,1,'1',?,'needs_review',NULL)", (did, text))
                db.execute("INSERT INTO pages_fts VALUES(?,1,?,?,NULL,NULL,'1',?)", (did, name, kind, text))
            db.commit()

    def select(self, question):
        return select_document_sources(self.corpus, question)

    def types(self, result):
        return {item["document_type"] for item in result["citations"]}

    def test_course_number_uses_bulletin_without_teaching_plan(self):
        result = self.select("What is the course number of Discrete Structures for Computer Science?")
        self.assertEqual(result["intent"], "catalogue")
        self.assertEqual(result["depth"], "lookup")
        self.assertEqual(self.types(result), {"bulletin"})
        self.assertTrue(all("CS F222" in item["excerpt"] for item in result["citations"]))

    def test_detailed_topics_use_respective_handout(self):
        result = self.select("in disc structure for computer science what topics under graphs are tsaught")
        self.assertEqual(result["depth"], "detailed")
        self.assertEqual(self.types(result), {"handout"})
        self.assertEqual(result["citations"][0]["page"], 2)

    def test_both_catalogue_and_detail_facts_receive_both_source_types(self):
        result = self.select("What is the course code and what graph topics are taught in Discrete Structures for Computer Science?")
        self.assertEqual(result["intent"], "catalogue_and_detail")
        self.assertEqual(self.types(result), {"bulletin", "handout"})

    def test_short_description_and_short_teaching_summary_are_distinct(self):
        result = self.select("Give a brief description of CS F222.")
        self.assertEqual(result["depth"], "brief")
        self.assertEqual(self.types(result), {"bulletin"})
        result = self.select("Briefly list the topics taught in CS F222.")
        self.assertEqual(result["depth"], "brief")
        self.assertEqual(self.types(result), {"handout"})

    def test_university_rule_uses_regulations(self):
        result = self.select("What is the minimum CGPA required for graduation?")
        self.assertEqual(result["intent"], "academic_policy")
        self.assertEqual(self.types(result), {"regulations"})

    def test_schedule_uses_course_timetable(self):
        result = self.select("When are the CS F222 lectures scheduled?")
        self.assertEqual(self.types(result), {"timetable"})

    def test_procedure_uses_specific_reference_documents(self):
        result = self.select("How do I submit an off-campus thesis joining report?")
        self.assertEqual(result["intent"], "procedure")
        self.assertEqual(self.types(result), {"reference"})
        self.assertEqual(result["citations"][0]["document_id"], "joining")

    def test_catalogue_can_fall_back_to_handout_with_status(self):
        with closing(sqlite3.connect(self.corpus)) as db:
            db.execute("DELETE FROM pages_fts WHERE document_id='bulletin'")
            db.commit()
        result = self.select("What is the course number of Discrete Structures for Computer Science?")
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(self.types(result), {"handout"})

    def test_missing_handout_allows_labelled_catalogue_overview(self):
        with closing(sqlite3.connect(self.corpus)) as db:
            db.execute("DELETE FROM pages WHERE document_id='discrete'")
            db.execute("DELETE FROM pages_fts WHERE document_id='discrete'")
            db.commit()
        result = self.select("What graph topics are taught in CS F222?")
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(self.types(result), {"bulletin"})

    def test_mixed_question_marks_missing_teaching_details(self):
        with closing(sqlite3.connect(self.corpus)) as db:
            db.execute("DELETE FROM pages WHERE document_id='discrete'")
            db.commit()
        result = self.select("What is the course code and what graph topics are taught in CS F222?")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["missing_sources"], ["handout"])

    def test_degree_unit_requirements_are_policy_not_catalogue(self):
        self.assertEqual(source_policy("How many units are required for graduation?")["intent"], "academic_policy")
        self.assertEqual(source_policy("What is the coursecode of Discrete Structures?")["intent"], "catalogue")
        self.assertEqual(source_policy("How do I submit a registration form?")["intent"], "procedure")

    def test_unknown_course_does_not_borrow_other_course_documents(self):
        result = self.select("What topics are taught in CS F888?")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["citations"], [])

    def test_explicit_source_and_rejection_are_respected(self):
        result = self.select("According to the bulletin, what topics are in CS F222?")
        self.assertEqual(self.types(result), {"bulletin"})
        policy = source_policy("Use the handout, not the bulletin, for CS F222.")
        self.assertEqual(policy["source_priority"], [["handout"]])

    def test_model_receives_source_policy_and_lookup_depth(self):
        client = chat_tests.FakeClient("CS F222. [1]", "documents", [1])
        DocumentChatAgent(client, self.corpus).answer("What is the course number of Discrete Structures for Computer Science?")
        payload = json.loads(client.messages[0][1]["content"])
        self.assertEqual(payload["source_selection"]["depth"], "lookup")
        self.assertEqual({source["document_type"] for source in payload["repository_sources"]}, {"bulletin"})

    def test_followup_can_change_from_catalogue_lookup_to_handout_details(self):
        result = select_document_sources(self.corpus, "More details please", "What is the course number of Discrete Structures for Computer Science?")
        self.assertEqual(result["intent"], "course_detail")
        self.assertEqual(result["depth"], "detailed")
        self.assertEqual(self.types(result), {"handout"})
        self.assertIn(2, {source["page"] for source in result["citations"]})
        result = select_document_sources(self.corpus, "What is its code?", "What graph topics are taught in CS F222?")
        self.assertEqual(result["intent"], "catalogue")
        self.assertEqual(self.types(result), {"bulletin"})

    def test_general_course_overview_is_a_summary_not_a_code_lookup(self):
        policy = source_policy("Tell me about CS F222")
        self.assertEqual(policy["depth"], "summary")
        result = self.select("Explain CS F222 in detail")
        self.assertEqual(result["intent"], "course_detail")
        self.assertEqual(self.types(result), {"handout"})

    def test_asking_for_a_specific_source_does_not_force_a_long_answer(self):
        self.assertEqual(source_policy("From the handout, what is the course code of CS F222?")["depth"], "lookup")
        self.assertEqual(source_policy("Who teaches CS F222?")["depth"], "lookup")

    def test_course_identity_survives_several_changes_in_question_depth(self):
        client = chat_tests.FakeClient("A sourced answer. [1]", "documents", [1])
        agent = DocumentChatAgent(client, self.corpus)
        history = []
        for question, source_type in (
            ("What is the course number of Discrete Structures for Computer Science?", "bulletin"),
            ("More details about the graph topics taught, please.", "handout"),
            ("Just its course number, please.", "bulletin"),
        ):
            result = agent.answer(question, history)
            history.append(result)
            self.assertEqual(result["course_context"]["code"], "CS F222")
            payload = json.loads(client.messages[-1][1]["content"])
            self.assertEqual({source["document_type"] for source in payload["repository_sources"]}, {source_type})
        result = agent.answer("More details about MATH F213, please.", history)
        self.assertEqual(result["course_context"]["code"], "MATH F213")


if __name__ == "__main__":
    unittest.main()
