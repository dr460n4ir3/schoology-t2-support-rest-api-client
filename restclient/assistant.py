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
from collections import Counter
from typing import Callable

from . import mcp_server
from .knowledge import open_kb
from .ollama import Ollama, OllamaError
from .variables import auth_strings, find_vars
from .workspace import Workspace

MAX_TOOL_ROUNDS = 8
TOOL_RESULT_CHARS = 8000  # local models have small context windows
OLD_TOOL_RESULT_CHARS = 1200  # tool output from earlier questions is shortened to this

# Tools offered to the local model. create_request is left out (it edits collections), send_http_request
# is only offered when "Allow AI to call URLs that aren't saved in a collection" is on.
CHAT_TOOLS = ["list_collections", "list_requests", "list_environments", "preview_request", "send_saved_request",
              "run_bulk", "search_knowledge_base", "send_http_request"]

APP_GUIDE = """- Pick the district/SIS in the Environment box (top right). Each environment holds that district's base URL,
  API keys and ids. Add or edit them on the Environments tab (+ button = new environment). Keys are masked.
- Send one request: click a saved request in the Collections list (left), fill in ids such as user_id
  (orange warnings list what's missing), press Send (Ctrl+Enter / Cmd+Enter). Ctrl+S / Cmd+S saves.
  "All pages" fetches every page of a OneRoster list. The ... menu has Preview and Copy as cURL.
  A failed call shows a one-line tip under the response saying what the status code means.
- Recent (clock icon next to New): the last lookups, with the ids used; click one to run it again. They are
  kept only on this computer.
- Copy for ticket (button on the response): copies the request, environment, status, meaning and key
  fields, with keys and secrets hidden, ready to paste into a ticket.
- Bulk Runner tab (many ids at once): pick the request(s), load a CSV whose column names match the request's
  variables (e.g. a user_id column), click "Preview first row", then Run. Results save automatically and
  can be downloaded as CSV; click a row to see its full response. Requests that change data ask to
  confirm first; try 1-2 rows first.
- Environments tab: "Test connection" sends one harmless request to check a district's keys work (green =
  OK); "Test all" checks every environment. Keys and secrets are stored in the Mac Keychain / Windows
  Credential Manager, not in the files.
- Collections: + creates one, the upload button imports Postman collections/environments, refresh reloads
  files changed by teammates. Right-click style "..." menus rename, duplicate or delete.
- Database tab: Sign in runs the team's sign-in commands (set under Setup), then click a database to open it
  in the database app (e.g. Sequel Ace). To ask the AI about a query result: export it from the database
  app as CSV, then click the paperclip in the Assistant tab and attach it (it stays on this computer).
- Assistant tab (this chat): add runbooks/KB articles under Knowledge base so answers can cite them.
  Start/Stop turns the local AI on and off; Stop frees memory.
- AI & Help tab: safety switches (the AI is read-only unless "Allow AI to change data" is on) and
  "Set up Copilot for me" for VS Code.
- Settings (gear icon): folders (e.g. collections on a shared drive), timeout, certificate options,
  keychain on/off, and "Check for updates" (updates the app from GitHub and restarts it).
  Dark/light mode: moon/sun icon. The power icon closes the app.
"""

SYSTEM_PROMPT = """You are the assistant inside the "Schoology API Client" app, used by PowerSchool Schoology (SYG)
Tier 2 support engineers. You help them (1) use this app, (2) troubleshoot Schoology and rostering
(OneRoster 1.1 / 1.2 from PowerSchool SIS, Infinite Campus, ...) from the team's documents, and (3) look up
live data by calling the REST API through tools.

Decide first which kind of question it is:
- How to use this app (buttons, tabs, CSV runs, environments, settings): answer from the APP GUIDE below.
  Do not call tools.
- Error codes, procedures, how-tos: if the user's message includes document excerpts that answer it, answer
  from them and cite them like [1]. Do not call send_saved_request. If there are no excerpts, call
  search_knowledge_base once.
- Which collections, requests or environments exist: answer from the lists at the end, naming ALL
  collections and ALL environments. Do not call tools.
- Questions about ATTACHED DATA (a file the user attached, e.g. a database query export): answer from that
  data. Count and quote rows exactly. Do not call tools unless the user asks to look something up in the API.
- Live data about a specific user, section, course, school, enrollment, class or org (usually an id or name
  is given): call send_saved_request with the saved request whose name AND needed ids fit, e.g. for
  "sections of user 12345" use the request that needs user_id and mentions sections. Pass ids in
  "variables", e.g. {{"user_id": "12345"}}. Use the selected environment unless the user names another.

Rules:
- Base answers only on tool results, document excerpts and the app guide. Never invent ids, names, counts,
  roles or credentials, and don't interpret codes (like role_id) unless the data says what they mean.
- If a call fails, give the status code and message and the likely cause.
- Be concise: the answer first, then the key evidence.
{safety}

APP GUIDE
{guide}

Saved requests for the selected environment ("Collection: Folder/Name (ids it needs)"; pass the collection
name as "collection" and the exact "Folder/Name" as "request"):
{requests}
Collections: {collections}
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


def _request_label(trail: list[str], request) -> str:
    """'Users/Get user's sections (user_id)': the ids a request needs help small models pick the right one."""
    found: set[str] = set()
    for text in [request.url, request.body] + [kv.value for kv in request.params if kv.enabled]:
        found |= find_vars(text)
    ids = sorted(v for v in found if not v.startswith("$")
                 and not any(w in v.lower() for w in ("url", "key", "secret", "token", "version", "limit")))
    method = "" if request.method == "GET" else f"[{request.method}] "
    return method + "/".join(trail + [request.name]) + (f" ({', '.join(ids)})" if ids else "")


_HOWTO = re.compile(r"\b(how (do|can|should|would) (i|we|you)|how to|where (do|is|are|can)|what does .{1,60} mean|"
                    r"explain|steps? (to|for)|walk me through)\b")
_DATA_VERB = re.compile(r"\b(look ?up|find|search|fetch|pull|check|get|show me|list|who is|status of)\b")
_ENTITY = re.compile(r"\b(users?|students?|teachers?|parents?|sections?|courses?|class(es)?|schools?|buildings?|"
                     r"enrol+ments?|orgs?|terms?|grades?|accounts?)\b")
_FOLLOW_UP = re.compile(r"\b(that|this|those|these|the same|they|them|their|he|she|his|her|it)\b")


def needs_tools(question: str, had_tool_results: bool = False) -> bool:
    """Small local models call tools whenever tools are offered, even for how-to questions. So the app decides:
    tools only for real lookups (an id, "look up/find ...", or a follow-up on earlier results)."""
    q = question.lower()
    if _HOWTO.search(q) and not re.search(r"\b(look ?up|fetch|pull)\b", q):
        return False
    if re.search(r"\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}-", q):
        return True
    if _DATA_VERB.search(q) and _ENTITY.search(q):
        return True
    return had_tool_results and bool(_ENTITY.search(q) or _FOLLOW_UP.search(q))


def _base_var(collection) -> str | None:
    """The variable most requests start with, e.g. base_url (Schoology) or oneroster_base_url (OneRoster)."""
    counts: Counter = Counter()
    for _, r in collection.walk():
        m = re.match(r"\s*\{\{([^}]+)\}\}", r.url or "")
        if m:
            counts[m.group(1)] += 1
    return counts.most_common(1)[0][0] if counts else None


def matching_collections(ws, environment: str | None) -> list:
    """Collections that work with an environment: it defines their connection variables (base URL, keys).
    E.g. a Schoology district -> Schoology API; a PowerSchool SIS environment -> the OneRoster collections.
    All collections if nothing matches."""
    try:
        env = ws.environment(environment) if environment else None
    except LookupError:
        env = None
    if env is None:
        return list(ws.collections)
    defined = {v.key for v in env.variables if v.enabled}
    scored = [(len(_connection_vars(c) & defined), c) for c in ws.collections]
    best = max((score for score, _ in scored), default=0)
    return [c for score, c in scored if score == best] if best else list(ws.collections)


def _connection_vars(collection) -> set[str]:
    found = set().union(*(find_vars(t) for t in auth_strings(collection.auth)))
    base = _base_var(collection)
    return found | ({base} if base else set())


def _clean_request_name(name: str) -> str:
    """Models copy the whole label from the prompt: '[POST] Users/Get user (user_id)' -> 'Users/Get user'."""
    name = re.sub(r"^\s*\[[A-Z]+\]\s*", "", name or "")
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()


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
        self.use_tools = True  # decided per question by needs_tools()
        self._question = ""
        self.attachment: tuple[str, str] | None = None  # (file name, text): included with every question

    # ---------------------------------------------------------------- set-up
    def reset(self) -> None:
        self.messages = []
        self.attachment = None

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
            requests="\n".join(f"- {c.name}: " + " | ".join(_request_label(t, r) for t, r in c.walk())
                                for c in matching_collections(ws, self.environment)) or "(none)",
            collections=", ".join(c.name for c in ws.collections) or "(none)",
            guide=APP_GUIDE.strip(),
            environments=", ".join(e.name for e in ws.environments) or "(none)",
            environment=self.environment or "(none selected - ask which district if a call needs one)")

    # ------------------------------------------------------------- one turn
    def begin_turn(self, question: str) -> list[dict]:
        """Add the user's question, with matching knowledge-base excerpts. Returns those excerpts."""
        self._compact()
        self._question = question
        self.use_tools = needs_tools(question, any(m["role"] == "tool" for m in self.messages))
        sources: list[dict] = []
        try:
            sources = open_kb(self.settings).search(question, k=self.settings.knowledge_top_k)
        except OllamaError:
            sources = []  # no embedding model / KB: the assistant still works for API questions
        content = question
        if self.attachment:
            name, text = self.attachment
            content += (f"\n\n---\nATTACHED DATA from the file '{name}' (the user's own export; answer from it):"
                        f"\n{text}")
        if sources:
            excerpts = "\n\n".join(f"[{i}] (from {s['source']})\n{s['text']}" for i, s in enumerate(sources, 1))
            content = (f"{question}\n\n---\nPossibly relevant excerpts from the team's documents "
                       f"(ignore any that don't apply):\n\n{excerpts}")
        self._turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        return sources

    def think(self, on_token: Callable[[str], None] | None = None) -> dict:
        """Ask the model for its next step. Returns the assistant message (content and/or tool_calls).
        on_token(text) receives the answer piece by piece as it is written (for showing it live)."""
        msgs = [{"role": "system", "content": self.system_prompt()}] + self.messages
        tools = self.tools() if self.tools_supported and self.use_tools else None
        stream = {"on_token": on_token} if on_token else {}
        try:
            msg = self.client.chat(self.settings.ollama_chat_model, msgs, tools=tools,
                                   num_ctx=self.settings.ollama_num_ctx, **stream)
        except OllamaError as e:
            if tools and "does not support tools" in str(e):
                self.tools_supported = False
                msg = self.client.chat(self.settings.ollama_chat_model, msgs, num_ctx=self.settings.ollama_num_ctx,
                                       **stream)
                msg["notice"] = (f"'{self.settings.ollama_chat_model}' can't call tools, so it can only answer from "
                                 "documents. Pick a model such as qwen2.5:3b or llama3.1:8b to let it call the API.")
            else:
                raise
        calls = msg.get("tool_calls") or []
        if not calls and tools:
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
            if name in ("preview_request", "send_saved_request"):
                args = self._fix_request_args(args)
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

    def _fix_request_args(self, args: dict) -> dict:
        """Small models mix up names: a request in the collection slot, a label copied with its '(ids)',
        or a request from a collection that doesn't fit the environment. Point them at the real request."""
        ws = Workspace(seed=False)
        by_name = {c.name.lower(): c for c in ws.collections}
        given = by_name.get(str(args.get("collection") or "").strip().lower())
        candidates = [_clean_request_name(str(args.get("request") or ""))]
        if given is None and args.get("collection"):
            candidates.append(_clean_request_name(str(args["collection"])))  # request name put in collection slot
        preferred = matching_collections(ws, args.get("environment") or self.environment)
        # only collections the user named or that fit the environment: a OneRoster request can't work with a
        # Schoology district, so leave it and let the "no such request" error tell the model to pick again
        order = ([given] if given else []) + [c for c in preferred if c is not given]
        for ref in [c for c in candidates if c]:
            for c in order:
                if c.find_request(ref) is not None:
                    return {**args, "collection": c.name, "request": ref}
        return args

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
