from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any


class GroqAPIError(RuntimeError):
    pass


class GroqClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "openai/gpt-oss-20b",
        endpoint: str = "https://api.groq.com/openai/v1/chat/completions",
        timeout: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A Groq API key is required.")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.endpoint = endpoint
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> GroqClient | None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key,
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            endpoint=os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions"),
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        function_name = None
        if len(tools) == 1 and isinstance(tools[0].get("function"), dict):
            function_name = tools[0]["function"].get("name")
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": function_name}} if function_name else "auto",
            "temperature": 0.1,
            "max_completion_tokens": 1200,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "BITS-Helper/0.1",
            },
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                choices = parsed.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0].get("message"), dict):
                    raise GroqAPIError("Groq returned a response without an assistant message.")
                return parsed
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code == 429 and attempt < 2:
                    retry_match = re.search(r"try again in ([0-9.]+)s", body, re.IGNORECASE)
                    if retry_match:
                        suggested: str | float = retry_match.group(1)
                    else:
                        header_value = exc.headers.get("retry-after") if exc.headers else None
                        
                        
                        try:
                            suggested = float(header_value) if header_value is not None else 1.0
                        except (TypeError, ValueError):
                            suggested = 1.0
                    try:
                        delay = min(float(suggested) + 0.25, 15.0)
                    except (TypeError, ValueError):
                        delay = 1.25
                    time.sleep(max(delay, 0.1))
                    continue
                if exc.code == 400 and "tool_use_failed" in body and attempt < 2:
                    
                    
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise GroqAPIError(f"Groq API request failed with HTTP {exc.code}: {body}") from exc
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise GroqAPIError(f"Groq API request failed: {exc}") from exc
        raise GroqAPIError("Groq API rate limit retry budget was exhausted.")
