"""LLM backend: deterministic mock or NVIDIA API catalog."""
import json
import re

import httpx

from common.config import Settings
from gateway import mock_llm
from gateway.store import GatewayStore


class LLMError(Exception):
    """LLM call failed or returned nothing usable."""


FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


def clean_content(content: str) -> str:
    """Strip <think> blocks and a surrounding Markdown code fence."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    m = FENCE_RE.match(content)
    return m.group(1).strip() if m else content


class LLMBackend:
    def __init__(self, settings: Settings, store: GatewayStore):
        """Keep settings and store."""
        self.settings = settings
        self.store = store

    def complete(self, role: str, args: dict) -> dict:
        """Return {"content", "model"} for args {"task", "messages"}."""
        messages = args["messages"]
        if self.settings.LLM_MODE == "mock":
            try:
                return {"content": mock_llm.respond(role, args["task"], messages), "model": "mock"}
            except Exception as e:
                raise LLMError(str(e))
        return {"content": self._nvidia(messages), "model": self.settings.MODEL_NAME}

    def _nvidia(self, messages: list[dict]) -> str:
        """Call the OpenAI-compatible chat completions endpoint."""
        s = self.settings
        body = {"model": s.MODEL_NAME, "messages": messages, "temperature": 0, "max_tokens": 512}
        try:
            if s.NVIDIA_EXTRA_BODY_JSON:
                body.update(json.loads(s.NVIDIA_EXTRA_BODY_JSON.strip().strip("'")))
            resp = httpx.post(f"{s.NVIDIA_BASE_URL}/chat/completions", json=body, timeout=60,
                              headers={"Authorization": f"Bearer {s.NVIDIA_API_KEY}"})
            if resp.status_code != 200:
                raise LLMError(f"status {resp.status_code}")
            content = resp.json()["choices"][0]["message"].get("content") or ""
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(str(e))
        content = clean_content(content)
        if not content:
            raise LLMError("empty content")
        return content
