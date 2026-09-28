from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from recommender.services.groq_client import GroqClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"}}]}).encode()


class GroqClientTests(unittest.TestCase):
    def test_request_uses_chat_completions_tools_and_bearer_auth(self) -> None:
        client = GroqClient("local-test-secret", model="test-model")
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_open):
            result = client.complete([{"role": "user", "content": "hello"}], [{"type": "function"}])
        payload = json.loads(captured["request"].data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["tools"], [{"type": "function"}])
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer local-test-secret")
        self.assertEqual(result["choices"][0]["message"]["content"], "done")

    def test_environment_configuration_is_optional(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(GroqClient.from_environment())
        with patch.dict(os.environ, {"GROQ_API_KEY": "test", "GROQ_MODEL": "model"}, clear=True):
            client = GroqClient.from_environment()
        self.assertIsNotNone(client)
        self.assertEqual(client.model, "model")

    def test_single_named_function_is_forced(self) -> None:
        client = GroqClient("local-test-secret")
        captured = {}

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return FakeResponse()

        tool = {"type": "function", "function": {"name": "rank_courses", "parameters": {"type": "object"}}}
        with patch("urllib.request.urlopen", side_effect=fake_open):
            client.complete([{"role": "user", "content": "rank"}], [tool])
        self.assertEqual(captured["payload"]["tool_choice"], {
            "type": "function",
            "function": {"name": "rank_courses"},
        })


if __name__ == "__main__":
    unittest.main()
