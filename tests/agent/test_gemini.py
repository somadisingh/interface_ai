"""Gemini adapter: request/response translation, and a full discovery run driven through the
adapter against a fake Gemini backend (no network)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from cua.agent.discovery import DiscoveryAgent, DiscoverySpec
from cua.agent.llm import (
    AnthropicClient,
    GeminiClient,
    Script,
    gemini_contents,
    gemini_functions,
    gemini_reply,
    make_llm,
)
from cua.agent.prompting import TOOLS
from cua.compiler.compile import compile_trace
from cua.handoff.channel import ScriptedChannel
from cua.policy import Redactor
from cua.runtime import open_runtime
from tests.agent.test_discovery import HAPPY, OUTPUTS, ROOT, SECRETS
from tests.conftest import LiveServer

SIGNATURE = b"\x01sig\xff"


def test_tools_become_function_declarations() -> None:
    decls = gemini_functions(TOOLS)
    assert [d.name for d in decls] == [t["name"] for t in TOOLS]
    click = next(d for d in decls if d.name == "click")
    assert "reason" in click.parameters_json_schema["required"]


def test_reply_keeps_first_call_and_raw_parts() -> None:
    parts = [
        types.Part(text="thinking out loud", thought=True),
        types.Part(text="Opening search."),
        types.Part(
            function_call=types.FunctionCall(name="click", args={"ref": "e1", "reason": "r"}),
            thought_signature=SIGNATURE,
        ),
        types.Part(function_call=types.FunctionCall(name="click", args={"ref": "e2"})),
    ]
    reply = gemini_reply(parts, lambda: "gem_0001")
    assert reply.text == "Opening search."  # thoughts are not surfaced as text
    assert reply.tool_call is not None
    assert (reply.tool_call.id, reply.tool_call.input["ref"]) == ("gem_0001", "e1")
    assert [b["type"] for b in reply.content] == ["text", "tool_use", "gemini_parts"]


def test_history_translation_round_trips_signatures_and_tool_results() -> None:
    png = base64.b64encode(b"\x89PNG fake").decode()
    reply = gemini_reply(
        [
            types.Part(
                function_call=types.FunctionCall(name="click", args={"ref": "e1"}),
                thought_signature=SIGNATURE,
            )
        ],
        lambda: "gem_0001",
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "page 1"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": png},
                },
            ],
        },
        {"role": "assistant", "content": reply.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "gem_0001",
                    "content": "ok",
                    "is_error": False,
                },
                {"type": "text", "text": "page 2"},
            ],
        },
        {"role": "user", "content": "Call exactly one tool."},
    ]
    contents = gemini_contents(messages)
    assert [c.role for c in contents] == ["user", "model", "user"]  # same-side turns merged
    assert contents[0].parts[1].inline_data.data == b"\x89PNG fake"
    assert contents[1].parts[0].thought_signature == SIGNATURE
    response = contents[2].parts[0].function_response
    assert (response.name, response.id, response.response) == ("click", None, {"output": "ok"})
    assert contents[2].parts[-1].text == "Call exactly one tool."


def test_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CUA_MODEL", raising=False)
    monkeypatch.delenv("CUA_LLM_PROVIDER", raising=False)
    assert isinstance(make_llm(), AnthropicClient)
    assert isinstance(make_llm(model="gemini-3.8-flash"), GeminiClient)
    monkeypatch.setenv("CUA_LLM_PROVIDER", "gemini")
    llm = make_llm()
    assert isinstance(llm, GeminiClient) and llm.model.startswith("gemini")
    monkeypatch.setenv("CUA_MODEL", "claude-sonnet-5-5")  # leftover from the other provider
    assert make_llm().model.startswith("gemini")
    with pytest.raises(ValueError):
        make_llm("openai")


class FakeGemini:
    """Stands in for google.genai.Client: checks each request is well-formed for Gemini, then
    answers from the scripted steps (which read the latest observation text)."""

    def __init__(self, script: list[Script]) -> None:
        self.script = list(script)
        self.models = self
        self.requests = 0

    def generate_content(self, *, model: str, contents: list[Any], config: Any) -> Any:
        roles = [c.role for c in contents]
        assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles
        assert (
            config.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.ANY
        )
        called: list[str] = []
        for c in contents:
            for p in c.parts:
                if p.function_call:
                    assert p.thought_signature == SIGNATURE  # signatures sent back intact
                    called.append(p.function_call.name)
                if p.function_response:
                    assert p.function_response.name == called[-1]
        latest = next(p.text for p in reversed(contents[-1].parts) if p.text)
        name, args = self.script[self.requests](latest)
        self.requests += 1
        part = types.Part(
            function_call=types.FunctionCall(name=name, args=args), thought_signature=SIGNATURE
        )
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=10
            ),
        )


def test_discovery_runs_end_to_end_through_the_adapter(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm = GeminiClient("gemini-test")
    fake = FakeGemini(HAPPY)
    llm._client = fake  # type: ignore[assignment]
    human = ScriptedChannel()
    with open_runtime(
        mockcore_url,
        human=human,
        mode="discovery",
        root=ROOT,
        runs_dir=tmp_path,
        secrets_source=SECRETS,
    ) as rt:
        trace = DiscoveryAgent(
            rt.surface,
            llm,
            engine=rt.engine,
            policy=rt.policy,
            human=human,
            log=rt.log,
            model_redactor=Redactor(),
            max_steps=12,
        ).run(
            DiscoverySpec(
                capability_id="member.read_savings_balance",
                product="mockcore",
                goal="Look up member 12345 and read their current savings balance",
                params={"member_id": "12345"},
                outputs=OUTPUTS,
                requires=["session.sign_on"],
            )
        )
    assert trace.status == "succeeded", trace.summary
    assert trace.model == "gemini-test" and fake.requests == len(HAPPY)
    assert trace.usage["input_tokens"] == 100 * len(HAPPY)
    assert compile_trace(trace).provenance.model == "gemini-test"


def test_client_retries_transient_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    retry = GeminiClient("gemini-test")._client._api_client._http_options.retry_options
    assert retry is not None and retry.attempts and retry.attempts >= 5
    assert 503 in (retry.http_status_codes or []) and 429 in (retry.http_status_codes or [])


class Overloaded:
    model = "gemini-test"

    def complete(self, system: str, messages: Any, tools: Any) -> Any:
        raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")


def test_model_outage_ends_discovery_cleanly_with_evidence(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    human = ScriptedChannel()
    with open_runtime(
        mockcore_url,
        human=human,
        mode="discovery",
        root=ROOT,
        runs_dir=tmp_path,
        secrets_source=SECRETS,
    ) as rt:
        trace = DiscoveryAgent(
            rt.surface,
            Overloaded(),
            engine=rt.engine,
            policy=rt.policy,
            human=human,
            log=rt.log,
            model_redactor=Redactor(),
            max_steps=5,
        ).run(
            DiscoverySpec(
                capability_id="member.read_savings_balance",
                product="mockcore",
                goal="Look up member 12345",
                params={"member_id": "12345"},
                outputs=OUTPUTS,
                requires=["session.sign_on"],
            )
        )
        log = (rt.log.run_dir / "events.jsonl").read_text()
    assert trace.status == "aborted" and "503 UNAVAILABLE" in trace.summary
    assert '"llm_error"' in log
