from __future__ import annotations

import unittest

from recommender.models import Constraint
from recommender.query import parse_query


class QueryTests(unittest.TestCase):
    def test_negation_does_not_cross_from_midsem_to_topic(self) -> None:
        intent = parse_query("Suggest courses with no midsem and AI topics")
        self.assertEqual(intent.topic_preferences, ("artificial intelligence",))
        self.assertFalse(intent.clarification_questions)

    def test_need_projects_is_an_explicit_requirement(self) -> None:
        intent = parse_query("I need projects")
        self.assertEqual(intent.hard_constraints, (Constraint("project_present", "eq", True),))

    def test_optional_projects_are_not_treated_as_positive_or_negative_requirement(self) -> None:
        for query in ("projects are not required", "I want a DEL, not necessarily with projects"):
            with self.subTest(query=query):
                intent = parse_query(query)
                self.assertFalse(intent.hard_constraints)
                self.assertFalse(intent.soft_preferences)
                self.assertTrue(intent.clarification_questions)

    def test_parses_source_example(self) -> None:
        intent = parse_query("Suggest an AI-related DEL with no midsem.")
        self.assertEqual(intent.requested_category, "DEL")
        self.assertEqual(intent.topic_preferences, ("artificial intelligence",))
        self.assertEqual(intent.hard_constraints[0].field, "midsem_present")

    def test_project_is_soft_by_default(self) -> None:
        intent = parse_query("I need a HUEL and prefer project-based evaluation.")
        self.assertEqual(intent.requested_category, "HUEL")
        self.assertEqual(intent.soft_preferences[0].field, "project_present")

    def test_project_can_be_explicitly_excluded(self) -> None:
        intent = parse_query("Suggest a HUEL with no projects.")
        self.assertEqual(intent.hard_constraints, (Constraint("project_present", "eq", False),))
        self.assertEqual(intent.soft_preferences, ())

    def test_project_requirement_is_local_and_soft_preference_stays_soft(self) -> None:
        intent = parse_query("I must take a HUEL, but prefer project evaluation.")
        self.assertEqual(intent.hard_constraints, ())
        self.assertEqual(intent.soft_preferences[0].value, True)

    def test_project_requirement_can_be_hard(self) -> None:
        intent = parse_query("I must have a project in my HUEL.")
        self.assertEqual(intent.hard_constraints[0].value, True)

    def test_project_only_and_required_phrasings_are_hard(self) -> None:
        for query in ("Only project-based courses, please.", "Projects are required."):
            with self.subTest(query=query):
                intent = parse_query(query)
                self.assertEqual(intent.hard_constraints[0].value, True)

    def test_project_negation_and_conflicts(self) -> None:
        for query in ("Not projects, please.", "Not project-based courses.", "No project work."):
            with self.subTest(query=query):
                intent = parse_query(query)
                self.assertEqual(intent.hard_constraints[0].value, False)
        intent = parse_query("Projects preferred, but no projects.")
        self.assertTrue(intent.ambiguities)
        self.assertEqual(intent.hard_constraints, ())
        self.assertEqual(intent.soft_preferences, ())

    def test_project_detection_uses_word_boundaries(self) -> None:
        intent = parse_query("I enjoy projective geometry.")
        self.assertEqual(intent.hard_constraints, ())
        self.assertEqual(intent.soft_preferences, ())

    def test_conflicting_categories_are_clarified(self) -> None:
        intent = parse_query("Suggest a CDC or DEL course.")
        self.assertIsNone(intent.requested_category)
        self.assertTrue(intent.ambiguities)
        self.assertTrue(intent.clarification_questions)

    def test_negated_category_and_topic_are_clarified(self) -> None:
        category = parse_query("Not CDC, please.")
        self.assertIsNone(category.requested_category)
        self.assertTrue(category.ambiguities)
        topic = parse_query("I do not want AI-related courses.")
        self.assertEqual(topic.topic_preferences, ())
        self.assertTrue(topic.ambiguities)

    def test_makeup_spelling_variants_trigger_clarification(self) -> None:
        for spelling in ("make-up", "make up", "makeup"):
            with self.subTest(spelling=spelling):
                intent = parse_query(f"What is the lenient {spelling} policy?")
                self.assertEqual(intent.ambiguities, ("lenient makeup policy",))


if __name__ == "__main__":
    unittest.main()
