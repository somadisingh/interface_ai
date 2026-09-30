"""Model access behind a small interface, so the provider is swappable and the agent loop is
testable offline with a scripted stand-in."""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMReply:
    text: str
    tool_call: ToolCall | None
    content: list[dict[str, Any]]
    """Assistant content blocks, appended verbatim to the conversation history."""
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    model: str

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply: ...


class AnthropicClient:
    """Claude via the Messages API with tool use. Exactly one tool call per turn."""

    def __init__(self, model: str | None = None, max_tokens: int = 1024) -> None:
        import anthropic

        self.model = model or os.environ.get("CUA_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        }
        response = self._client.messages.create(**request)
        content = [block.model_dump(exclude_none=True) for block in response.content]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        call = next(
            (
                ToolCall(b["id"], b["name"], dict(b["input"]))
                for b in content
                if b.get("type") == "tool_use"
            ),
            None,
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return LLMReply(text, call, content, usage)


Script = Callable[[str], tuple[str, dict[str, Any]]]
"""Given the latest observation text, return (tool name, tool input)."""


class ScriptedClient:
    """Deterministic stand-in for tests and offline demos: each turn's tool call comes from a
    function of the observation the agent was shown."""

    model = "scripted"

    def __init__(self, script: list[Script]) -> None:
        self._script = list(script)
        self._ids = itertools.count(1)
        self.turns = 0

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        observation = _latest_text(messages)
        if self.turns >= len(self._script):
            name, args = "finish", {"success": False, "summary": "script exhausted"}
        else:
            name, args = self._script[self.turns](observation)
        self.turns += 1
        call = ToolCall(f"toolu_{next(self._ids):04d}", name, args)
        block = {"type": "tool_use", "id": call.id, "name": name, "input": args}
        return LLMReply("", call, [block], {"input_tokens": 0, "output_tokens": 0})


def _latest_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        texts = [b["text"] for b in content if b.get("type") == "text"]
        if texts:
            return texts[-1]
    return ""
