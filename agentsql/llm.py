"""LLM backends behind one tiny interface, so the agent loop is provider-neutral.

    conv = backend.new_conversation(system, user, tools, task)
    turn = conv.step()                      # one model call
    conv.add_tool_results([(id, text, is_error), ...])

Backends:
  anthropic - Claude via the official SDK (ANTHROPIC_API_KEY). Default model:
              claude-haiku-5-5 (cheapest current Claude).
  openai    - any OpenAI-compatible endpoint: OpenAI, vLLM, SGLang
              (OPENAI_API_KEY, OPENAI_BASE_URL).
  mock      - scripted, no network. Used to test the harness end to end.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from agentsql.tasks import Task


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict  # JSON schema for the input object


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0


# --------------------------------------------------------------------------
# Anthropic (Claude)
# --------------------------------------------------------------------------
_SERVER_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
                           "claude-sonnet-5-5"}

class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model: str = "claude-haiku-5-5", effort: str = "medium",
                 max_tokens: int = 16000):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model, self.effort, self.max_tokens = model, effort, max_tokens

    def new_conversation(self, system, user, tools, task=None):
        return _AnthropicConversation(self, system, user, tools)


class _AnthropicConversation:
    def __init__(self, backend: AnthropicBackend, system, user, tools):
        self.b = backend
        self.system = system
        self.tools = [
            {"name": t.name, "description": t.description,
             "input_schema": t.schema, "strict": True}
            for t in tools
        ]
        self.messages: list = [{"role": "user", "content": user}]

    def step(self) -> Turn:
        # Thinking is omitted -> adaptive on current models.
        params = dict(model=self.b.model, max_tokens=self.b.max_tokens, system=self.system,
                      tools=self.tools, messages=self.messages,
                      output_config={"effort": self.b.effort})
        if self.b.model in _SERVER_FALLBACK_MODELS:
            # Reroute the request if a safety classifier declines it.
            resp = self.b.client.beta.messages.create(
                **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:  # e.g. Haiku 5.5: no server-side fallback
            resp = self.b.client.messages.create(**params)
        # Append the full content (including thinking blocks) unchanged.
        self.messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input))
                 for b in resp.content if b.type == "tool_use"]
        return Turn(text, calls, resp.stop_reason or "",
                    resp.usage.input_tokens, resp.usage.output_tokens)

    def add_tool_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}
            for i, text, err in results
        ]})


# --------------------------------------------------------------------------
# OpenAI-compatible (OpenAI, vLLM, SGLang)
# --------------------------------------------------------------------------
class OpenAIBackend:
    name = "openai"

    def __init__(self, model: str, max_tokens: int = 4096):
        from openai import OpenAI

        self.client = OpenAI()  # reads OPENAI_API_KEY / OPENAI_BASE_URL
        self.model, self.max_tokens = model, max_tokens

    def new_conversation(self, system, user, tools, task=None):
        return _OpenAIConversation(self, system, user, tools)


class _OpenAIConversation:
    def __init__(self, backend: OpenAIBackend, system, user, tools):
        self.b = backend
        self.tools = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.schema}}
            for t in tools
        ]
        self.messages: list = [{"role": "system", "content": system},
                               {"role": "user", "content": user}]

    def step(self) -> Turn:
        resp = self.b.client.chat.completions.create(
            model=self.b.model, messages=self.messages, tools=self.tools,
            max_tokens=self.b.max_tokens,
        )
        choice = resp.choices[0]
        msg = choice.message
        self.messages.append(msg.model_dump(exclude_none=True))
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        usage = resp.usage
        return Turn(msg.content or "", calls, choice.finish_reason or "",
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0)

    def add_tool_results(self, results):
        for i, text, err in results:
            self.messages.append({"role": "tool", "tool_call_id": i,
                                  "content": ("ERROR: " + text) if err else text})


# --------------------------------------------------------------------------
# Mock: a scripted "agent" that explores like a careful analyst would.
# --------------------------------------------------------------------------
class MockBackend:
    """list_tables -> describe each relevant table -> run the reference SQL -> answer.

    Deterministic, so identical tasks produce identical SQL (an upper bound on
    reuse; real models vary their SQL text).
    """

    name = "mock"

    def __init__(self, latency_s: float = 0.02):
        self.latency_s = latency_s

    def new_conversation(self, system, user, tools, task: Task | None = None):
        if task is None:
            raise ValueError("mock backend needs the task")
        return _MockConversation(self, task, aware=any(t.name == "cached_queries" for t in tools))


class _MockConversation:
    def __init__(self, backend: MockBackend, task: Task, aware: bool = False):
        self.latency_s = backend.latency_s
        self.plan = (
            ([("cached_queries", {})] if aware else [])
            + [("list_tables", {})]
            + [("describe_table", {"table": t}) for t in task.tables]
            + [("run_sql", {"sql": task.reference_sql})]
        )
        self.i = 0
        self.last_result = ""

    def step(self) -> Turn:
        time.sleep(self.latency_s)
        if self.i < len(self.plan):
            name, args = self.plan[self.i]
            self.i += 1
            return Turn("", [ToolCall(f"call_{self.i}", name, args)], "tool_use")
        # Answer = first cell of the last result (line 2 of the tab table).
        lines = self.last_result.splitlines()
        answer = lines[1].split("\t")[0] if len(lines) > 1 else "unknown"
        self.i += 1
        return Turn("", [ToolCall(f"call_{self.i}", "final_answer",
                                  {"answer": answer})], "tool_use")

    def add_tool_results(self, results):
        if results:
            self.last_result = results[-1][1]


def make_backend(kind: str, model: str | None = None, effort: str = "medium"):
    if kind == "anthropic":
        return AnthropicBackend(model or "claude-haiku-5-5", effort)
    if kind == "openai":
        if not model:
            raise ValueError("--model is required for the openai backend")
        return OpenAIBackend(model)
    if kind == "mock":
        return MockBackend()
    raise ValueError(f"unknown backend {kind!r}")
