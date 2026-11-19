"""Tests for the local AI assistant and knowledge base, using a fake Ollama (no models needed)."""

import io
import json
import os
import re
import tempfile
import zipfile

os.environ.setdefault("RESTCLIENT_HOME", tempfile.mkdtemp(prefix="restclient_test_"))

from restclient import assistant as asst  # noqa: E402
from restclient.config import Settings  # noqa: E402
from restclient.knowledge import KnowledgeBase, chunk_text, extract_text  # noqa: E402
from restclient.ollama import OllamaError  # noqa: E402

WORDS = ["roster", "sync", "enrollment", "duplicate", "merge", "grade", "passback", "token", "oauth", "section",
         "student", "teacher", "error", "409", "401", "course"]


class FakeOllama:
    """Bag-of-words 'embeddings' + scripted chat replies."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.chats = []

    def embed(self, model, texts, batch=32):
        out = []
        for t in texts:
            tokens = re.findall(r"[a-z0-9]+", t.lower())
            out.append([float(tokens.count(w)) + 0.01 for w in WORDS])
        return out

    def chat(self, model, messages, tools=None, num_ctx=None, temperature=0.2):
        self.chats.append({"messages": messages, "tools": tools})
        return self.replies.pop(0)


def make_kb(tmp, model="fake-embed", client=None):
    return KnowledgeBase(tmp, client or FakeOllama(), model)


# ------------------------------------------------------------------ text handling
def test_chunk_text_covers_everything_and_respects_size():
    paras = [f"Paragraph {i}. " + "word " * 60 for i in range(30)]
    chunks = chunk_text("\n\n".join(paras), size=1000, overlap=150)
    assert len(chunks) > 5
    assert all(len(c) <= 1000 + 200 for c in chunks)
    joined = " ".join(chunks)
    assert all(f"Paragraph {i}." in joined for i in range(30))


def test_chunk_text_splits_one_huge_paragraph():
    chunks = chunk_text("x " * 5000, size=800, overlap=100)
    assert len(chunks) > 5 and all(len(c) <= 1000 for c in chunks)


def test_extract_docx_and_html():
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>roster</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Second para</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    assert extract_text("a.docx", buf.getvalue()) == "Hello roster\n\nSecond para"
    html = b"<html><head><style>x{}</style></head><body><p>Fix</p><script>alert(1)</script><p>sync</p></body></html>"
    text = extract_text("a.html", html)
    assert "Fix" in text and "sync" in text and "alert" not in text


def test_unsupported_file_type():
    try:
        extract_text("a.exe", b"MZ")
    except ValueError as e:
        assert "Unsupported" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ------------------------------------------------------------------ knowledge base
def test_knowledge_base_add_search_remove_and_persist(tmp_path):
    kb = make_kb(tmp_path)
    kb.add("roster.txt", b"Error 409 during roster sync means a duplicate account. Merge the duplicate.")
    kb.add("grades.md", b"Grade passback needs the oauth token and the section to be linked.")
    hits = kb.search("duplicate account 409 roster", k=1, min_score=0)
    assert hits[0]["source"] == "roster.txt"
    assert kb.search("grade passback section", k=1, min_score=0)[0]["source"] == "grades.md"

    # a second instance (e.g. the MCP server process) reads the same index from disk
    kb2 = make_kb(tmp_path)
    assert {d["name"] for d in kb2.documents()} == {"roster.txt", "grades.md"}
    assert len(kb2.vectors) == len(kb2.chunks)

    # re-adding replaces instead of duplicating; remove deletes the file too
    kb.add("roster.txt", b"Updated: roster sync error 409.")
    assert sum(1 for d in kb.documents() if d["name"] == "roster.txt") == 1
    kb.remove("grades.md")
    assert not (tmp_path / "files" / "grades.md").exists()
    kb2.reload_if_changed()
    assert [d["name"] for d in kb2.documents()] == ["roster.txt"]


def test_knowledge_base_flags_embedding_model_change(tmp_path):
    make_kb(tmp_path, "model-a").add("a.txt", b"roster sync")
    kb = make_kb(tmp_path, "model-b")
    assert kb.needs_rebuild
    try:
        kb.search("roster")
    except OllamaError as e:
        assert "Rebuild" in str(e)
    else:
        raise AssertionError("expected OllamaError")
    assert kb.rebuild() >= 1 and not kb.needs_rebuild
    assert kb.search("roster", min_score=0)


# ------------------------------------------------------------------ assistant helpers
def test_simplify_schema_drops_optional_null_and_titles():
    schema = {"title": "x", "type": "object", "properties": {
        "env": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Env"}}}
    assert asst._simplify(schema) == {"type": "object", "properties": {"env": {"type": "string", "default": None}}}


def test_coerce_args_fixes_common_model_mistakes():
    def tool(collection: str, variables: dict[str, str] | None = None, top_k: int = 5,
             all_pages: bool = False, environment: str | None = None):
        pass

    args = asst._coerce_args(tool, json.dumps({"collection": "OneRoster 1.1", "variables": '{"user_id": 5}',
                                               "top_k": "3", "all_pages": "true", "environment": "",
                                               "made_up": 1}))
    assert args == {"collection": "OneRoster 1.1", "variables": {"user_id": "5"}, "top_k": 3, "all_pages": True}


def test_tool_call_written_as_text_is_recognised():
    call = asst._text_tool_call('```json\n{"name": "list_collections", "parameters": {}}\n```', {"list_collections"})
    assert call == {"function": {"name": "list_collections", "arguments": {}}}
    assert asst._text_tool_call("The answer is {not json}", {"list_collections"}) is None
    embedded = 'Let me try that.\n\n{"name": "list_requests", "parameters": {"collection": "Schoology API"}}'
    assert asst._text_tool_call(embedded, {"list_requests"})["function"]["arguments"] == {"collection": "Schoology API"}
    assert asst._text_tool_call('{"name": "rm_rf", "parameters": {}}', {"list_collections"}) is None


def test_chat_tools_respect_safety_switches():
    a = asst.Assistant(Settings())
    names = {t["function"]["name"] for t in a.tools()}
    assert "send_saved_request" in names and "search_knowledge_base" in names
    assert "send_http_request" not in names and "create_request" not in names
    a.settings.mcp_allow_adhoc_requests = True
    assert "send_http_request" in {t["function"]["name"] for t in a.tools()}


def test_assistant_runs_tools_then_answers(tmp_path):
    from restclient.workspace import Workspace
    Workspace()  # seeds the example collections into the test home
    settings = Settings(knowledge_dir=str(tmp_path))
    a = asst.Assistant(settings, environment="Schoology - Example District")
    a.client = FakeOllama([
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_collections", "arguments": {}}}]},
        {"role": "assistant", "content": "<think>hmm</think>You have the example collections."},
    ])
    events = []
    answer = a.ask("Which collections do I have?", lambda kind, data: events.append(kind))
    assert answer == "You have the example collections."
    assert events.count("tool_call") == 1 and events.count("tool_result") == 1
    tool_msg = next(m for m in a.messages if m["role"] == "tool")
    assert tool_msg["tool_name"] == "list_collections" and "Schoology API" in tool_msg["content"]
    system = a.client.chats[0]["messages"][0]["content"]
    assert "Schoology - Example District" in system and "Read-only mode" in system
    assert "Users/Get user" in system  # saved request names are listed so the model doesn't invent them


def test_assistant_blocks_unknown_tools(tmp_path):
    a = asst.Assistant(Settings(knowledge_dir=str(tmp_path)))
    a.begin_turn("hi")
    name, _, result = a.call_tool({"function": {"name": "create_request", "arguments": {}}})
    assert "Unknown tool" in json.loads(result)["error"]


def test_request_labels_show_the_ids_a_request_needs():
    from restclient.models import KV, Request
    r = Request(name="Get user's sections", url="{{base_url}}/users/{{user_id}}/sections",
                params=[KV("limit", "{{limit}}"), KV("start", "{{start_index}}")])
    assert asst._request_label(["Users"], r) == "Users/Get user's sections (start_index, user_id)"
    assert asst._request_label([], Request(name="Make", method="POST", url="{{base_url}}/x")) == "[POST] Make"


def test_system_prompt_includes_the_app_guide(tmp_path):
    prompt = asst.Assistant(Settings(knowledge_dir=str(tmp_path))).system_prompt()
    assert "Bulk Runner" in prompt and "APP GUIDE" in prompt


def test_tools_are_only_offered_for_real_lookups():
    assert asst.needs_tools("Look up Schoology user 12345")
    assert asst.needs_tools("find user Maria Lopez")
    assert asst.needs_tools("Which sections is that user enrolled in?", had_tool_results=True)
    assert not asst.needs_tools("Which sections is that user enrolled in?")  # nothing looked up yet
    assert not asst.needs_tools("How do I run a CSV of user ids through a request?")
    assert not asst.needs_tools("What does error SYG-ROSTER-409 mean?")
    assert not asst.needs_tools("which collection env do i have")


def test_request_names_copied_with_their_label_are_cleaned():
    assert asst._clean_request_name("[POST] Users/Get user (user_id)") == "Users/Get user"
    assert asst._clean_request_name("Users/Get user") == "Users/Get user"


def test_prompt_lists_only_requests_for_the_selected_environment(tmp_path):
    from restclient.workspace import Workspace
    ws = Workspace()  # seeds the example collections/environments into the test home
    names = [c.name for c in asst.matching_collections(ws, "Schoology - Example District")]
    assert names == ["Schoology API"]
    assert {c.name for c in asst.matching_collections(ws, None)} == {c.name for c in ws.collections}
    prompt = asst.Assistant(Settings(knowledge_dir=str(tmp_path)), "Schoology - Example District").system_prompt()
    assert "Users/Get user's sections (user_id)" in prompt and "Enrollments for class" not in prompt
    assert "OneRoster 1.1" in prompt  # still named under "Collections"


def test_mixed_up_request_names_are_fixed_for_the_environment(tmp_path):
    from restclient.workspace import Workspace
    Workspace()
    a = asst.Assistant(Settings(knowledge_dir=str(tmp_path)), "Schoology - Example District")
    assert a._fix_request_args({"collection": "Users/Get user (user_id)"}) == \
        {"collection": "Schoology API", "request": "Users/Get user"}
    assert a._fix_request_args({"collection": "Schoology API", "request": "[GET] Users/Get user's sections (user_id)"}) \
        == {"collection": "Schoology API", "request": "Users/Get user's sections"}
    wrong = {"collection": "Schoology API", "request": "Enrollments/Enrollments for class (class_sourcedId)"}
    assert a._fix_request_args(dict(wrong)) == wrong  # a OneRoster request isn't moved to a Schoology district
