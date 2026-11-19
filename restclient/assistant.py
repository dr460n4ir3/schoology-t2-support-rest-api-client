"""Local AI assistant: an Ollama model that can read the knowledge base AND call the API.

It uses exactly the same tools as the MCP server (list_collections, send_saved_request, ...), so the same
safety switches apply (read-only unless "Allow AI to change data" is on; secrets never shown).
Everything runs on this computer - nothing is sent to an outside AI service.

    a = Assistant(settings, environment="Schoology - Example District")
    print(a.ask("Who is user 12345 and which sections are they enrolled in?"))
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from typing import Callable

from . import mcp_server
from .knowledge import open_kb
from .ollama import Ollama, OllamaError
from .workspace import Workspace

MAX_TOOL_ROUNDS = 8
TOOL_RESULT_CHARS = 8000  # local models have small context windows
OLD_TOOL_RESULT_CHARS = 1200  # tool output from earlier questions is shortened to this

# Tools offered to the local model. create_request is left out (it edits collections), send_http_request
# is only offered when "Allow AI to call URLs that aren't saved in a collection" is on.
CHAT_TOOLS = ["list_collections", "list_requests", "list_environments", "preview_request", "send_saved_request",
              "run_bulk", "search_knowledge_base", "send_http_request"]

SYSTEM_PROMPT = """You are the assistant for PowerSchool Schoology (SYG) Tier 2 support engineers.
You help troubleshoot Schoology and rostering (OneRoster 1.1 / 1.2 from PowerSchool SIS, Infinite Campus, ...)
by reading the team's documents and by calling the REST API through tools.

How to work:
- Questions about procedures, error codes and how-tos: answer from the document excerpts, no tool needed.
- Only call tools when you need live data from a district (users, sections, enrollments, orgs, ...).
- To look something up in a district, use send_saved_request with one of the saved requests listed below
  (Schoology API for Schoology ids; OneRoster collections for SIS data). Never invent request names. Pass ids through the "variables" argument,
  e.g. {{"user_id": "12345"}}.
- Use the environment the user selected unless they name another one.
- Base answers ONLY on tool results and the document excerpts. Never invent ids, names, counts or credentials.
  If a call fails, say what failed (status code and message) and suggest the likely cause.
- When you use document excerpts, cite them like [1] or [2].
- Be concise: support engineers want the answer first, then the key evidence.
{safety}

Saved requests (use these exact names, "Folder/Name"):
{requests}
Environments: {environments}
Selected environment: {environment}
"""


def _tool_specs() -> dict[str, dict]:
    """JSON schemas of the MCP server's tools, converted to Ollama's tool format."""
    async def _list():
        return await mcp_server.mcp.list_tools()

    result: dict = {}

    def worker():  # asyncio.run can't be used on a thread that already runs an event loop (the UI)
        result["tools"] = asyncio.run(_list())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    specs = {}
    for tool in result["tools"]:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
        specs[tool.name] = {"type": "function", "function": {
            "name": tool.name, "description": (tool.description or "").strip(),
            "parameters": _simplify(schema)}}
    return specs


def _simplify(schema: dict) -> dict:
    """Small local models handle plain schemas better: drop titles, turn Optional[X] (anyOf X|null) into X."""
    if isinstance(schema, dict):
        if "anyOf" in schema:
            options = [o for o in schema["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {k: v for k, v in schema.items() if k != "anyOf"}
                merged.update(options[0])
                schema = merged
        return {k: _simplify(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_simplify(v) for v in schema]
    return schema


_SPECS: dict[str, dict] | None = None


def tool_specs() -> dict[str, dict]:
    global _SPECS
    if _SPECS is None:
        _SPECS = _tool_specs()
    return _SPECS


def _coerce_args(func: Callable, args) -> dict:
    """Models sometimes send JSON-as-text, "true"/"5" strings or extra keys; tidy that up."""
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        return {}
    sig = inspect.signature(func)
    clean = {}
    for name, value in args.items():
        if name not in sig.parameters or value is None:
            continue
        ann = sig.parameters[name].annotation
        ann = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))  # "int" or <class 'int'>
        if isinstance(value, str):
            text = value.strip()
            if ann.startswith(("dict", "list")) and text[:1] in "{[":
                try:
                    value = json.loads(text)
                except ValueError:
                    pass
            elif ann.startswith("bool"):
                value = text.lower() in ("true", "1", "yes")
            elif ann.startswith("int"):
                try:
                    value = int(text)
                except ValueError:
                    continue
            elif text == "" and sig.parameters[name].default is None:
                continue  # "" for an optional value means "not given"
        if isinstance(value, dict):
            value = {str(k): "" if v is None else str(v) for k, v in value.items()}
        clean[name] = value
    return clean


def _text_tool_call(content: str, names: set[str]) -> dict | None:
    """Some small models write the tool call as JSON text instead of using the tool-call format."""
    decoder = json.JSONDecoder()
    text = content or ""
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        try:
            data, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or data.get("function")
        if isinstance(name, str) and name in names:
            return {"function": {"name": name, "arguments": data.get("parameters", data.get("arguments", {}))}}
    return None


def strip_thinking(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()


class Assistant:
    def __init__(self, settings, environment: str | None = None):
        self.settings = settings
        self.environment = environment
        self.client = Ollama(settings.ollama_url)
        self.messages: list[dict] = []
        self.tools_supported = True
        self._turn_start = 0
        self._question = ""

    # ---------------------------------------------------------------- set-up
    def reset(self) -> None:
        self.messages = []

    def tools(self) -> list[dict]:
        specs = tool_specs()
        names = [n for n in CHAT_TOOLS if n in specs]
        if not self.settings.mcp_allow_adhoc_requests:
            names.remove("send_http_request")
        return [specs[n] for n in names]

    def system_prompt(self) -> str:
        ws = Workspace(seed=False)
        safety = ("- Read-only mode: only GET requests are allowed. If the user asks to change data, explain "
                  "that an admin must enable 'Allow AI to change data' on the AI & Help tab, or they can run "
                  "the request themselves in the app."
                  if not self.settings.mcp_allow_write_methods else
                  "- Write requests (POST/PUT/PATCH/DELETE) are allowed. ALWAYS call preview_request and get "
                  "the user's explicit 'yes' before sending anything that changes data.")
        return SYSTEM_PROMPT.format(
            safety=safety,
            requests="\n".join(f"- {c.name}: " + " | ".join("/".join(t + [r.name]) for t, r in c.walk())
                                for c in ws.collections) or "(none)",
            environments=", ".join(e.name for e in ws.environments) or "(none)",
            environment=self.environment or "(none selected - ask which district if a call needs one)")

    # ------------------------------------------------------------- one turn
    def begin_turn(self, question: str) -> list[dict]:
        """Add the user's question, with matching knowledge-base excerpts. Returns those excerpts."""
        self._compact()
        self._question = question
        sources: list[dict] = []
        try:
            sources = open_kb(self.settings).search(question, k=self.settings.knowledge_top_k)
        except OllamaError:
            sources = []  # no embedding model / KB: the assistant still works for API questions
        content = question
        if sources:
            excerpts = "\n\n".join(f"[{i}] (from {s['source']})\n{s['text']}" for i, s in enumerate(sources, 1))
            content = (f"{question}\n\n---\nPossibly relevant excerpts from the team's documents "
                       f"(ignore any that don't apply):\n\n{excerpts}")
        self._turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        return sources

    def think(self) -> dict:
        """Ask the model for its next step. Returns the assistant message (content and/or tool_calls)."""
        msgs = [{"role": "system", "content": self.system_prompt()}] + self.messages
        tools = self.tools() if self.tools_supported else None
        try:
            msg = self.client.chat(self.settings.ollama_chat_model, msgs, tools=tools,
                                   num_ctx=self.settings.ollama_num_ctx)
        except OllamaError as e:
            if tools and "does not support tools" in str(e):
                self.tools_supported = False
                msg = self.client.chat(self.settings.ollama_chat_model, msgs, num_ctx=self.settings.ollama_num_ctx)
                msg["notice"] = (f"'{self.settings.ollama_chat_model}' can't call tools, so it can only answer from "
                                 "documents. Pick a model such as llama3.1:8b or qwen3:8b to let it call the API.")
            else:
                raise
        calls = msg.get("tool_calls") or []
        if not calls and self.tools_supported:
            fallback = _text_tool_call(msg.get("content", ""), {t["function"]["name"] for t in self.tools()})
            if fallback:
                calls = [fallback]
                msg["content"] = ""
        out = {"role": "assistant", "content": strip_thinking(msg.get("content", ""))}
        if calls:
            out["tool_calls"] = calls
        self.messages.append(out)
        return {**out, "notice": msg.get("notice")}

    def call_tool(self, call: dict) -> tuple[str, dict, str]:
        """Run one tool call from the model. Returns (tool name, arguments used, result text)."""
        fn = call.get("function", {})
        name = fn.get("name", "")
        allowed = {t["function"]["name"] for t in self.tools()}
        func = getattr(mcp_server, name, None) if name in allowed else None
        args: dict = {}
        if func is None:
            result: object = {"error": f"Unknown tool '{name}'. Available: {', '.join(sorted(allowed))}"}
        else:
            args = _coerce_args(func, fn.get("arguments", {}))
            if name in ("preview_request", "send_saved_request", "run_bulk", "send_http_request") \
                    and not args.get("environment") and self.environment:
                args["environment"] = self.environment
            try:
                result = func(**args)
            except Exception as e:  # tool errors go back to the model so it can correct itself
                result = {"error": f"{type(e).__name__}: {e}"}
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > TOOL_RESULT_CHARS:
            text = text[:TOOL_RESULT_CHARS] + f"... [truncated, {len(text)} characters total]"
        self.messages.append({"role": "tool", "tool_name": name, "content": text})
        return name, args, text

    def _compact(self) -> None:
        """Before a new question: drop the document excerpts and shorten tool output of earlier questions."""
        if self._question and self._turn_start < len(self.messages):
            self.messages[self._turn_start]["content"] = self._question
        for m in self.messages:
            if m["role"] == "tool" and len(m["content"]) > OLD_TOOL_RESULT_CHARS:
                m["content"] = m["content"][:OLD_TOOL_RESULT_CHARS] + "... [shortened]"
        self.messages = self.messages[-40:]
        while self.messages and self.messages[0]["role"] != "user":
            self.messages.pop(0)  # never start the history with a dangling tool result

    # ------------------------------------------------------------ everything
    def ask(self, question: str, on_event: Callable[[str, object], None] | None = None) -> str:
        """Run a whole turn (used by tests / scripts; the UI drives the same steps itself)."""
        emit = on_event or (lambda kind, data: None)
        emit("sources", self.begin_turn(question))
        for _ in range(MAX_TOOL_ROUNDS):
            msg = self.think()
            if msg.get("notice"):
                emit("notice", msg["notice"])
            if not msg.get("tool_calls"):
                return msg["content"]
            for call in msg["tool_calls"]:
                emit("tool_call", call)
                emit("tool_result", self.call_tool(call))
        return "I stopped after too many tool calls. Try a more specific question."
