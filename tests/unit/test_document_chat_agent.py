from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from recommender.services.document_chat_agent import DocumentChatAgent


class FakeClient:
    model = "test-model"

    def __init__(self, answer: str, mode: str, source_numbers: list[int]) -> None:
        self.answer_text = answer
        self.mode = mode
        self.source_numbers = source_numbers
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "deliver_answer",
                            "arguments": json.dumps({
                                "answer": self.answer_text,
                                "mode": self.mode,
                                "source_numbers": self.source_numbers,
                            }),
                        },
                    }],
                },
            }],
        }


class DocumentChatAgentTests(unittest.TestCase):
    def make_corpus(self, folder: str) -> Path:
        path = Path(folder) / "corpus.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE documents(document_id TEXT PRIMARY KEY,content_sha256 TEXT,primary_file_name TEXT,document_type TEXT,semester TEXT,page_count INTEGER,parser_version TEXT,indexed_at TEXT);
                CREATE TABLE sources(source_path TEXT PRIMARY KEY,file_name TEXT,document_id TEXT);
                CREATE TABLE pages(document_id TEXT,page_number INTEGER,unit_key TEXT,text TEXT,verification_status TEXT,section TEXT);
                CREATE VIRTUAL TABLE pages_fts USING fts5(document_id UNINDEXED,page_number UNINDEXED,file_name UNINDEXED,document_type UNINDEXED,semester UNINDEXED,section UNINDEXED,unit_key UNINDEXED,text,tokenize='unicode61');
                INSERT INTO documents VALUES('DOC-1','hash','regulations.pdf','regulations',NULL,1,'v1','now');
                INSERT INTO pages VALUES('DOC-1',4,'4','A student must complete the documented prerequisite before registering for CS F999.','needs_review','Prerequisites');
                INSERT INTO pages_fts VALUES('DOC-1',4,'regulations.pdf','regulations',NULL,'Prerequisites','4','A student must complete the documented prerequisite before registering for CS F999.');
            """)
        return path

    def test_document_answer_keeps_only_valid_model_selected_sources(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient("CS F999 requires the documented prerequisite. [1]", "documents", [1, 99])
            result = DocumentChatAgent(client, self.make_corpus(folder)).answer("What is the CS F999 prerequisite?")
        self.assertEqual(result["mode"], "documents")
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(result["citations"][0]["document_id"], "DOC-1")

    def test_general_answer_works_without_document_matches(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient("Recursion is a function calling itself with a base case.", "general", [])
            result = DocumentChatAgent(client, self.make_corpus(folder)).answer("Explain recursion simply")
        self.assertEqual(result["mode"], "general")
        self.assertEqual(result["citations"], [])
        payload = json.loads(client.messages[0][1]["content"])
        self.assertEqual(payload["repository_sources"], [])

    def test_recent_question_is_used_for_followup_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient("It requires the documented prerequisite. [1]", "documents", [1])
            result = DocumentChatAgent(client, self.make_corpus(folder)).answer(
                "What about its prerequisite?",
                [{"question": "Tell me about CS F999", "answer": "It is a course."}],
            )
        self.assertEqual(len(result["citations"]), 1)


if __name__ == "__main__":
    unittest.main()
