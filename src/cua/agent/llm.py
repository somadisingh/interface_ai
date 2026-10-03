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

        self.model = model or _env_model(gemini=False) or DEFAULT_MODEL
        self.max_tokens = max_tokens
        # Reads ANTHROPIC_API_KEY. Keys that are not scoped to a workspace must name one.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self._client = anthropic.Anthropic(default_headers=headers, max_retries=6)

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


DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


class GeminiClient:
    """Gemini via the google-genai SDK with function calling, behind the same interface.

    The agent keeps its history in the Anthropic content-block shape; this adapter translates
    each request (tools -> function declarations, tool_use/tool_result -> function_call /
    function_response, base64 images -> inline data) and maps the reply back. The model's raw
    parts are carried in a ``gemini_parts`` block so thought signatures, which Gemini 3 models
    require on later turns, are sent back unchanged."""

    def __init__(self, model: str | None = None, max_tokens: int = 8192) -> None:
        from google import genai
        from google.genai import types

        self.model = model or _env_model(gemini=True) or DEFAULT_GEMINI_MODEL
        self.max_tokens = max_tokens  # thinking tokens count against this on Gemini 3
        # Overload (503) and rate limits (429) are common and usually pass within a minute:
        # back off exponentially (2s, 4s, ... capped at 60s) for up to ~4 minutes.
        retry = types.HttpRetryOptions(
            attempts=8,
            initial_delay=2.0,
            max_delay=60.0,
            http_status_codes=list(RETRYABLE_STATUS),
        )
        # Reads GEMINI_API_KEY (or GOOGLE_API_KEY).
        self._client = genai.Client(http_options=types.HttpOptions(retry_options=retry))
        self._ids = itertools.count(1)

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            tools=[types.Tool(function_declarations=gemini_functions(tools))],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._client.models.generate_content(
            model=self.model, contents=gemini_contents(messages), config=config
        )
        candidate = response.candidates[0] if response.candidates else None
        parts = list(candidate.content.parts or []) if candidate and candidate.content else []
        reply = gemini_reply(parts, lambda: f"gem_{next(self._ids):04d}")
        meta = response.usage_metadata
        usage = {
            "input_tokens": (meta.prompt_token_count or 0) if meta else 0,
            "output_tokens": ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
            if meta
            else 0,
        }
        return LLMReply(reply.text, reply.tool_call, reply.content, usage)


def gemini_functions(tools: list[dict[str, Any]]) -> list[Any]:
    from google.genai import types

    return [
        types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters_json_schema=t["input_schema"],
        )
        for t in tools
    ]


def gemini_reply(parts: list[Any], new_id: Callable[[], str]) -> LLMReply:
    """Map Gemini response parts to the agent's content blocks. Only the first function call
    is used: the agent acts one step at a time."""
    text = "".join(p.text for p in parts if p.text and not p.thought)
    call: ToolCall | None = None
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for p in parts:
        if p.function_call is not None and call is None:
            fc = p.function_call
            call = ToolCall(fc.id or new_id(), fc.name or "", dict(fc.args or {}))
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
            )
    content.append(
        {
            "type": "gemini_parts",
            "parts": [p.model_dump(mode="json", exclude_none=True) for p in parts],
        }
    )
    return LLMReply(text, call, content)


def gemini_contents(messages: list[dict[str, Any]]) -> list[Any]:
    """Translate the agent's Anthropic-shaped history into Gemini contents. Consecutive turns
    from the same side are merged, since Gemini expects user/model alternation."""
    import base64

    from google.genai import types

    names: dict[str, str] = {}  # tool_use id -> function name, for function responses
    gemini_ids: dict[str, str | None] = {}  # our id -> the id Gemini gave (often none)
    contents: list[Any] = []
    for message in messages:
        role = "model" if message["role"] == "assistant" else "user"
        blocks = message["content"]
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        parts: list[Any] = []
        raw = next((b for b in blocks if b.get("type") == "gemini_parts"), None)
        for b in blocks:
            if b.get("type") == "tool_use":
                names[b["id"]] = b["name"]
                gemini_ids[b["id"]] = b["id"] if not b["id"].startswith("gem_") else None
        if role == "model" and raw is not None:
            parts = [types.Part.model_validate(p) for p in raw["parts"]]
        else:
            for b in blocks:
                kind = b.get("type")
                if kind == "text":
                    parts.append(types.Part(text=b["text"]))
                elif kind == "image":
                    data = base64.b64decode(b["source"]["data"])
                    parts.append(
                        types.Part.from_bytes(data=data, mime_type=b["source"]["media_type"])
                    )
                elif kind == "tool_use":
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(name=b["name"], args=b["input"])
                        )
                    )
                elif kind == "tool_result":
                    result = b["content"]
                    if not isinstance(result, str):
                        result = "".join(x.get("text", "") for x in result)
                    key = "error" if b.get("is_error") else "output"
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=gemini_ids.get(b["tool_use_id"]),
                                name=names.get(b["tool_use_id"], "unknown"),
                                response={key: result},
                            )
                        )
                    )
        if not parts:
            continue
        if contents and contents[-1].role == role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))
    return contents


PROVIDERS = ("anthropic", "gemini")


def _env_model(*, gemini: bool) -> str | None:
    """$CUA_MODEL, unless it names the other provider's model (e.g. a leftover Claude id
    after switching CUA_LLM_PROVIDER to gemini)."""
    name = os.environ.get("CUA_MODEL") or ""
    return name if name and name.startswith("gemini") == gemini else None


def make_llm(provider: str | None = None, model: str | None = None) -> LLMClient:
    """Pick the provider: explicit argument, then CUA_LLM_PROVIDER, then the model name's
    prefix, else Anthropic."""
    chosen = (provider or os.environ.get("CUA_LLM_PROVIDER") or "").strip().lower()
    if not chosen:
        hint = model or os.environ.get("CUA_MODEL") or ""
        chosen = "gemini" if hint.startswith("gemini") else "anthropic"
    if chosen == "gemini":
        return GeminiClient(model)
    if chosen == "anthropic":
        return AnthropicClient(model)
    raise ValueError(f"unknown LLM provider {chosen!r}; expected one of {', '.join(PROVIDERS)}")


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
