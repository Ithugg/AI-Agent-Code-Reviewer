"""Provider-neutral model sessions, so every model runs through the same loop.

`AnthropicSession` talks to the Claude Messages API, `OpenAICompatSession` to
any OpenAI-compatible chat endpoint (for the open-model comparison), and
`ScriptedSession` replays canned turns in tests and dry runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0  # billed at the full input price
    output_tokens: int = 0  # includes thinking
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass(frozen=True)
class Turn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | ...
    usage: Usage


class Session(Protocol):
    model: str

    def send(self, *, user: str | None = None, results: list[ToolResult] | None = None) -> Turn: ...


class AnthropicSession:
    """One conversation with Claude.

    Caching follows the agent-loop pattern: an explicit breakpoint on the frozen
    system prompt (shared by every case) plus automatic caching for the growing
    conversation. Tools are sorted so their bytes never change between cases.
    """

    def __init__(
        self,
        client: Any,  # anthropic.Anthropic
        model: str,
        system: str,
        tools: list[ToolSpec],
        *,
        effort: str | None = None,
        max_tokens: int = 16000,
        output_schema: dict[str, Any] | None = None,
        adaptive_thinking: bool = True,  # False for models that predate adaptive thinking
    ):
        self.client = client
        self.model = model
        self.messages: list[dict[str, Any]] = []
        self._request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "cache_control": {"type": "ephemeral"},
        }
        if adaptive_thinking:
            self._request["thinking"] = {"type": "adaptive"}
        if tools:
            self._request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema, "strict": True}
                for t in sorted(tools, key=lambda t: t.name)
            ]
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if output_schema:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if output_config:
            self._request["output_config"] = output_config

    def send(self, *, user: str | None = None, results: list[ToolResult] | None = None) -> Turn:
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
            for r in results or []
        ]
        if user is not None:
            content.append({"type": "text", "text": user})
        self.messages.append({"role": "user", "content": content})
        response = self.client.messages.create(messages=self.messages, **self._request)
        # Thinking blocks must go back unchanged, so keep the SDK's content objects.
        self.messages.append({"role": "assistant", "content": response.content})
        u = response.usage
        return Turn(
            text="".join(b.text for b in response.content if b.type == "text"),
            tool_calls=[ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"],
            stop_reason=response.stop_reason or "end_turn",
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
        )


class OpenAICompatSession:
    """One conversation with an OpenAI-compatible /chat/completions endpoint."""

    _STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        model: str,
        system: str,
        tools: list[ToolSpec],
        *,
        max_tokens: int = 8000,
        output_schema: dict[str, Any] | None = None,
    ):
        self.http = http
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self._body: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
        if tools:
            self._body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in sorted(tools, key=lambda t: t.name)
            ]
        if output_schema:
            self._body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "review", "schema": output_schema, "strict": True},
            }

    def send(self, *, user: str | None = None, results: list[ToolResult] | None = None) -> Turn:
        for r in results or []:
            text = f"ERROR: {r.content}" if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": text})
        if user is not None:
            self.messages.append({"role": "user", "content": user})
        resp = self.http.post(self.url, json={**self._body, "messages": self.messages})
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        self.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        calls = []
        for tc in message.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc["function"].get("arguments")}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else {"_value": args}))
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return Turn(
            text=message.get("content") or "",
            tool_calls=calls,
            stop_reason=self._STOP.get(choice.get("finish_reason"), choice.get("finish_reason") or "end_turn"),
            usage=Usage(
                input_tokens=(usage.get("prompt_tokens") or 0) - cached,
                output_tokens=usage.get("completion_tokens") or 0,
                cache_read_tokens=cached,
            ),
        )


@dataclass
class ScriptedSession:
    """Replays turns (or functions of the latest tool results) in order; for tests and dry runs."""

    script: list[Turn | Callable[[list[ToolResult]], Turn]]
    model: str = "scripted"
    received: list[tuple[str | None, list[ToolResult]]] = field(default_factory=list)

    def send(self, *, user: str | None = None, results: list[ToolResult] | None = None) -> Turn:
        self.received.append((user, list(results or [])))
        step = self.script.pop(0) if self.script else Turn("", [], "end_turn", Usage())
        return step(list(results or [])) if callable(step) else step
