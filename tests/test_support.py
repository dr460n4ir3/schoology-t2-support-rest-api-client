"""Keychain storage, redaction, error tips, copy-for-ticket, connection test, recent lookups, attachments,
updates and streaming. Nothing here touches the real keychain, GitHub or a real API."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring
import pytest
from keyring.backend import KeyringBackend

from restclient import engine, redact, secret_store, support, updater
from restclient.config import Settings
from restclient.models import KV, Auth, Collection, Environment, Folder, Request
from restclient.ollama import Ollama
from restclient.storage import Store


# ------------------------------------------------------------------ keychain ---
class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture
def memory_keyring(monkeypatch):
    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    monkeypatch.setenv("RESTCLIENT_KEYCHAIN", "1")
    secret_store.reset_cache()
    yield ring
    keyring.set_keyring(previous)
    secret_store.reset_cache()


def _settings(tmp_path, **kw):
    return Settings(collections_dir=str(tmp_path / "c"), environments_dir=str(tmp_path / "e"), **kw)


def _district():
    return Environment(name="District", variables=[KV("base_url", "https://x.test"),
                                                   KV("schoology_consumer_secret", "s3cr3t-value", secret=True),
                                                   KV("user_id", "12345")],
                       auth=Auth(type="oauth2", client_id="cid", client_secret="client-s3cret"))


def test_secrets_go_to_keychain_not_the_file(tmp_path, memory_keyring):
    store = Store(_settings(tmp_path))
    env = _district()
    path = store.save_environment(env)
    text = path.read_text(encoding="utf-8")
    assert "s3cr3t-value" not in text and "client-s3cret" not in text
    assert "12345" in text and '"keychain": true' in text
    assert set(memory_keyring.data.values()) == {"s3cr3t-value", "client-s3cret"}
    loaded = Store(_settings(tmp_path)).load_environments()[0]
    assert {v.key: v.value for v in loaded.variables}["schoology_consumer_secret"] == "s3cr3t-value"
    assert loaded.auth.client_secret == "client-s3cret"


def test_old_plain_text_secrets_are_moved(tmp_path, memory_keyring, monkeypatch):
    monkeypatch.setenv("RESTCLIENT_KEYCHAIN", "0")
    secret_store.reset_cache()
    path = Store(_settings(tmp_path)).save_environment(_district())
    assert "s3cr3t-value" in path.read_text(encoding="utf-8")  # the old way: secrets in the file
    monkeypatch.setenv("RESTCLIENT_KEYCHAIN", "1")
    secret_store.reset_cache()
    env = Store(_settings(tmp_path)).load_environments()[0]  # loading moves them
    assert "s3cr3t-value" not in path.read_text(encoding="utf-8")
    assert {v.key: v.value for v in env.variables}["schoology_consumer_secret"] == "s3cr3t-value"


def test_placeholders_and_variables_stay_in_the_file(tmp_path, memory_keyring):
    env = Environment(name="Example", variables=[KV("client_secret", "PASTE-CLIENT-SECRET", secret=True)],
                      auth=Auth(type="oauth2", client_secret="{{client_secret}}"))
    text = Store(_settings(tmp_path)).save_environment(env).read_text(encoding="utf-8")
    assert "PASTE-CLIENT-SECRET" in text and "{{client_secret}}" in text and not memory_keyring.data


def test_missing_keychain_entry_is_reported(tmp_path, memory_keyring):
    store = Store(_settings(tmp_path))
    store.save_environment(_district())
    memory_keyring.data.clear()  # e.g. the file was copied to another computer
    fresh = Store(_settings(tmp_path))
    env = fresh.load_environments()[0]
    assert {v.key: v.value for v in env.variables}["schoology_consumer_secret"] == ""
    assert any("not found" in e for e in fresh.load_errors)


def test_keychain_off_setting_keeps_files(tmp_path, memory_keyring):
    path = Store(_settings(tmp_path, use_keychain=False)).save_environment(_district())
    assert "s3cr3t-value" in path.read_text(encoding="utf-8") and not memory_keyring.data


# ------------------------------------------------------------------- redact ---
def test_redact_known_values_and_patterns():
    redact.register("super-secret-123", "ab", "{{x}}")
    text = ('{"access_token": "eyJabc.def", "Authorization": "Bearer zzz999", "note": "super-secret-123", '
            '"name": "ab"} oauth_signature=QWE%3D&x=1')
    out = redact.redact(text)
    for gone in ("eyJabc.def", "zzz999", "super-secret-123", "QWE%3D"):
        assert gone not in out
    assert '"name": "ab"' in out and "x=1" in out  # short values are left alone


def test_bulk_results_never_contain_secrets(tmp_path):
    from restclient import runner

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"access_token": "tok-from-server-999", "echo": "client-s3cret"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = Environment(variables=[KV("key", "client-s3cret", secret=True)])
        req = Request(method="GET", url=f"http://127.0.0.1:{srv.server_port}/x?k={{{{key}}}}")
        results = runner.run(Collection(), [req], env, [{"a": "1"}], runner.RunOptions(), Settings())
    finally:
        srv.shutdown()
    csv_text = runner.results_to_csv(results)
    assert "client-s3cret" not in csv_text and "tok-from-server-999" not in csv_text


# --------------------------------------------------------------- error tips ---
@pytest.mark.parametrize("status,word", [(401, "key/secret"), (403, "allowed"), (404, "Not found"),
                                         (429, "Too many"), (500, "server had an error"), (418, "rejected")])
def test_error_tips(status, word):
    assert word in support.error_tip(engine.Result(status=status), "oauth1")


def test_error_tips_for_connection_problems():
    assert "certificates" in support.error_tip(engine.Result(error="SSL certificate problem: x"))
    assert "base URL" in support.error_tip(engine.Result(error="ConnectionError: getaddrinfo failed"))
    assert support.error_tip(engine.Result(status=200)) == ""


# ---------------------------------------------------------- copy for ticket ---
def test_ticket_text_has_key_fields_and_no_secrets():
    rr = engine.ResolvedRequest(method="GET", url="https://api.test/v1/users/12345",
                                auth=Auth(type="bearer", token="bearer-tok-777"))
    res = engine.Result(method="GET", url=rr.url, status=200, reason="OK", elapsed_ms=120,
                        text=json.dumps({"id": 12345, "name_first": "Maria", "token": "bearer-tok-777",
                                         "roles": [1, 2]}))
    text = support.ticket_text("Users / Get user", "Example District", rr, res)
    assert "Environment: Example District" in text and "200 OK" in text and "name_first: Maria" in text
    assert "bearer-tok-777" not in text and "roles" not in text


def test_key_fields_unwraps_oneroster_lists():
    data = {"users": [{"sourcedId": "u1", "givenName": "Ana", "familyName": "Li"}, {"sourcedId": "u2"}]}
    fields = dict(support.key_fields(data))
    assert fields["items returned"] == "2" and fields["item 1"] == "u1 · Ana Li"


# ----------------------------------------------------------- connection test ---
def _schoology():
    c = Collection(name="Schoology API", variables=[KV("base_url", "")],
                   auth=Auth(type="oauth1", consumer_key="{{schoology_consumer_key}}",
                             consumer_secret="{{schoology_consumer_secret}}"))
    c.items = [Folder(name="Users", items=[
        Request(name="Get user", url="{{base_url}}/users/{{user_id}}"),
        Request(name="Who am I (app-user-info)", url="{{base_url}}/app-user-info"),
        Request(name="Update user", method="PUT", url="{{base_url}}/users/{{user_id}}")])]
    return c


def test_health_check_picks_a_harmless_request():
    oneroster = Collection(name="OneRoster", auth=Auth(type="oauth2", client_id="{{client_id}}"),
                           items=[Request(name="All orgs", url="{{oneroster_base_url}}/orgs")])
    env = Environment(name="D", variables=[KV("base_url", "https://x.test"), KV("schoology_consumer_key", "k"),
                                           KV("schoology_consumer_secret", "s")])
    c, r = support.pick_health_request([_schoology(), oneroster], env)
    assert c.name == "Schoology API" and r.name.startswith("Who am I")


def test_health_check_reports_unconfigured_environment():
    env = Environment(name="Empty")
    result = support.health_check([_schoology()], env, Settings())
    assert not result.ok


# ------------------------------------------------------------ recent lookups ---
def test_recent_lookup_bakes_ids_but_not_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "recent_path", lambda: tmp_path / "recent.json")
    c = _schoology()
    env = Environment(name="District", variables=[KV("base_url", "https://x.test"), KV("user_id", "12345"),
                                                  KV("schoology_consumer_key", "ck-value"),
                                                  KV("schoology_consumer_secret", "cs-value-999", secret=True)])
    req = c.find_request("Users/Get user")
    support.record_lookup(req, c, env, engine.Result(status=200))
    support.record_lookup(req, c, env, engine.Result(status=200))  # same lookup again: not duplicated
    items = support.load_recent()
    assert len(items) == 1
    entry = items[0]
    assert entry["request"]["url"] == "{{base_url}}/users/12345" and entry["values"] == {"user_id": "12345"}
    text = (tmp_path / "recent.json").read_text(encoding="utf-8")
    assert "cs-value-999" not in text and "ck-value" not in text
    assert entry["label"] == "Users / Get user" and support.recent_title(entry).endswith("(user_id=12345)")


def test_rerun_keeps_its_name_and_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "recent_path", lambda: tmp_path / "recent.json")
    c = _schoology()
    env = Environment(name="D", variables=[KV("base_url", "https://x.test"), KV("user_id", "12345")])
    support.record_lookup(c.find_request("Users/Get user"), c, env, engine.Result(status=200))
    entry = support.load_recent()[0]
    rerun = Request.from_dict(entry["request"])  # what Recent loads: ids baked in, not in the collection
    support.record_lookup(rerun, c, env, engine.Result(status=200), previous=entry)
    items = support.load_recent()
    assert len(items) == 1 and items[0]["label"] == "Users / Get user" and items[0]["values"] == {"user_id": "12345"}


def test_recent_file_is_outside_the_repo():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    assert repo not in support.recent_path().resolve().parents


# --------------------------------------------------------------- attachments ---
def test_csv_attachment_summary_is_capped():
    rows = "\n".join(f"{i},user{i}@x.org,\"Smith, J\"" for i in range(2000))
    text, note = support.summarize_attachment("q.csv", f"id,email,name\n{rows}".encode(), limit=2000)
    assert text.startswith("Columns: id,email,name\nRows: 2000") and len(text) < 2200
    assert "2000 rows" in note and "first" in note and '"Smith, J"' in text
    text, _ = support.summarize_attachment("q.csv", b"id,email,role\n1,a@x,student\n2,,student\n3,,teacher\n")
    assert "2 rows have no email" in text and "2 rows have role = student; 1 rows have role = teacher" in text


# -------------------------------------------------------------------- updates ---
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(updater.git_exe() is None, reason="git not installed")
def test_update_check_and_pull(tmp_path, monkeypatch):
    origin, app = tmp_path / "origin.git", tmp_path / "app"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("clone", str(origin), str(app), cwd=tmp_path)
    for repo in (app,):
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
    (app / "requirements.txt").write_text("requests\n")
    _git("add", ".", cwd=app)
    _git("commit", "-m", "first", cwd=app)
    _git("push", "origin", "HEAD:main", cwd=app)
    _git("branch", "--set-upstream-to=origin/main", cwd=app)
    other = tmp_path / "other"  # a teammate pushes a new version
    _git("clone", str(origin), str(other), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=other)
    _git("config", "user.name", "t", cwd=other)
    (other / "new_feature.py").write_text("x = 1\n")
    _git("add", ".", cwd=other)
    _git("commit", "-m", "Add a new feature", cwd=other)
    _git("push", "origin", "HEAD:main", cwd=other)

    monkeypatch.setattr(updater, "source_folder", lambda: app)
    status = updater.check()
    assert status.kind == "git" and status.available and status.can_apply
    assert status.changes == ["Add a new feature"]
    assert updater.apply() == "Updated."
    assert (app / "new_feature.py").exists()
    assert not updater.check().available


def test_release_check_compares_versions(monkeypatch):
    import requests

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"tag_name": "v99.0.0", "html_url": "https://example.test/r", "body": "- Faster\n- Safer"}

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    status = updater._check_release()
    assert status.available and status.latest == "99.0.0" and status.changes == ["Faster", "Safer"]


# ------------------------------------------------------------------ streaming ---
def test_chat_streams_tokens_and_tool_calls():
    lines = [{"message": {"content": "Hel"}}, {"message": {"content": "lo"}},
             {"message": {"content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]}},
             {"done": True}]

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in lines:
                self.wfile.write((json.dumps(line) + "\n").encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        seen = []
        msg = Ollama(f"http://127.0.0.1:{srv.server_port}").chat("m", [], on_token=seen.append)
    finally:
        srv.shutdown()
    assert seen == ["Hel", "lo"] and msg["content"] == "Hello" and msg["tool_calls"][0]["function"]["name"] == "x"
