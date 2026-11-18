"""Core tests against a local mock API server (no internet needed).

Run:  .venv\\Scripts\\python -m pytest -q      (Mac: .venv/bin/python -m pytest -q)
"""

import base64
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("RESTCLIENT_HOME", tempfile.mkdtemp(prefix="restclient_test_"))

from oauthlib.common import Request as OAuthRequest  # noqa: E402
from oauthlib.oauth1.rfc5849 import signature as oauth_sig  # noqa: E402

from restclient import engine, postman, runner  # noqa: E402
from restclient.config import Settings  # noqa: E402
from restclient.models import KV, Auth, Collection, Environment, Folder, Request  # noqa: E402
from restclient.variables import missing_variables, substitute  # noqa: E402

SECRET = "s3cr3t"
CALLS = {"token": 0, "rate": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, data, headers=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _verify_oauth1(self, body: bytes) -> bool:
        uri = f"http://{self.headers['Host']}{self.path}"
        req = OAuthRequest(uri, self.command, body.decode() or None, dict(self.headers))
        params = oauth_sig.collect_parameters(uri_query=urlparse(uri).query, body=req.body,
                                              headers=req.headers, exclude_oauth_signature=True)
        req.params = params
        sig = dict(oauth_sig.collect_parameters(headers=req.headers, exclude_oauth_signature=False))
        req.signature = sig.get("oauth_signature")
        method = sig.get("oauth_signature_method")
        if method == "HMAC-SHA256":
            return oauth_sig.verify_hmac_sha256(req, SECRET, None)
        if method == "PLAINTEXT":
            return oauth_sig.verify_plaintext(req, SECRET, None)
        return oauth_sig.verify_hmac_sha1(req, SECRET, None)

    def handle_any(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self._body()
        if u.path == "/token":
            CALLS["token"] += 1
            expected = "Basic " + base64.b64encode(b"cid:csecret").decode()
            if self.headers.get("Authorization") != expected:
                return self._json(401, {"error": "invalid_client"})
            return self._json(200, {"access_token": "tok123", "expires_in": 3600})
        if u.path == "/or/users":
            if self.headers.get("Authorization") != "Bearer tok123":
                return self._json(401, {"error": "unauthorized"})
            limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
            users = [{"sourcedId": f"u{i}"} for i in range(offset, min(offset + limit, 250))]
            return self._json(200, {"users": users}, {"X-Total-Count": "250"})
        if u.path == "/sig":
            ok = self._verify_oauth1(body)
            return self._json(200 if ok else 401, {"valid": ok, "method": self.command, "query": q})
        if u.path == "/me":
            self.send_response(303)
            self.send_header("Location", "/sig?redirected=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if u.path == "/rate":
            CALLS["rate"] += 1
            if CALLS["rate"] % 2 == 1:
                return self._json(429, {"error": "slow down"}, {"Retry-After": "0"})
            return self._json(200, {"id": q.get("id"), "data": {"name": f"user-{q.get('id')}"}})
        if u.path == "/echo":
            return self._json(200, {"method": self.command, "query": q, "body": body.decode(),
                                    "content_type": self.headers.get("Content-Type"),
                                    "auth": self.headers.get("Authorization")})
        return self._json(404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


SETTINGS = Settings(timeout_seconds=5)


def test_substitute_and_missing():
    scope = {"a": "1", "b": "{{a}}-2"}
    assert substitute("x/{{a}}/{{ b }}/{{nope}}", scope) == "x/1/1-2/{{nope}}"
    assert len(substitute("{{$guid}}", {})) == 36
    c = Collection(variables=[KV("base", "http://h")])
    r = Request(url="{{base}}/users/{{user_id}}")
    assert missing_variables(r, c, None) == {"user_id"}
    assert missing_variables(r, c, None, {"user_id": "5"}) == set()
    # a blank environment value counts as "not set" (avoids calling /users/ by accident)
    env = Environment(variables=[KV("user_id", "")])
    assert missing_variables(r, c, env) == {"user_id"}
    assert missing_variables(r, c, env, {"user_id": "7"}) == set()


@pytest.mark.parametrize("method", ["HMAC-SHA1", "HMAC-SHA256", "PLAINTEXT"])
def test_oauth1_signatures_valid(server, method):
    c = Collection(auth=Auth(type="oauth1", consumer_key="key", consumer_secret=SECRET, signature_method=method))
    r = Request(url=f"{server}/sig", params=[KV("filter", "status='active' AND role='student'")])
    _, res = engine.execute(r, c, None, SETTINGS)
    assert res.status == 200, res.text
    assert res.json()["valid"] is True


def test_oauth1_form_body_signed(server):
    c = Collection(auth=Auth(type="oauth1", consumer_key="key", consumer_secret=SECRET))
    r = Request(method="POST", url=f"{server}/sig", body_mode="urlencoded", form=[KV("a", "1 2")])
    _, res = engine.execute(r, c, None, SETTINGS)
    assert res.json()["valid"] is True


def test_oauth1_resigned_after_303_redirect(server):
    c = Collection(auth=Auth(type="oauth1", consumer_key="key", consumer_secret=SECRET))
    _, res = engine.execute(Request(url=f"{server}/me"), c, None, SETTINGS)
    assert res.status == 200 and res.json() == {"valid": True, "method": "GET", "query": {"redirected": "1"}}


def test_environment_auth_override(server):
    """Collection says OAuth 2, environment overrides with OAuth 1 (Infinite Campus 1.1 style)."""
    c = Collection(auth=Auth(type="oauth2", token_url="http://invalid"))
    env = Environment(variables=[KV("k", "key"), KV("s", SECRET)],
                      auth=Auth(type="oauth1", consumer_key="{{k}}", consumer_secret="{{s}}",
                                signature_method="HMAC-SHA256"))
    _, res = engine.execute(Request(url=f"{server}/sig"), c, env, SETTINGS)
    assert res.json()["valid"] is True


def test_oauth2_token_cached_and_all_pages(server):
    engine.clear_token_cache()
    CALLS["token"] = 0
    c = Collection(auth=Auth(type="oauth2", token_url=f"{server}/token", client_id="cid", client_secret="csecret"))
    r = Request(url=f"{server}/or/users", params=[KV("limit", "100")])
    _, one = engine.execute(r, c, None, SETTINGS)
    assert one.status == 200 and len(one.json()["users"]) == 100
    _, every = engine.execute(r, c, None, SETTINGS, all_pages=True)
    users = every.json()["users"]
    assert len(users) == 250 and users[-1]["sourcedId"] == "u249"
    assert "3 page(s)" in every.note
    assert CALLS["token"] == 1  # token re-used


def test_oauth2_bad_credentials_reported(server):
    engine.clear_token_cache()
    c = Collection(auth=Auth(type="oauth2", token_url=f"{server}/token", client_id="cid", client_secret="wrong"))
    _, res = engine.execute(Request(url=f"{server}/or/users"), c, None, SETTINGS)
    assert res.error and "token request failed" in res.error.lower()


@pytest.mark.parametrize("name", ["OneRoster 1.1.json", "OneRoster 1.2.json"])
def test_oneroster_get_token_debug_request(server, name):
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "examples" / "collections" / name
    c = Collection.from_dict(json.loads(path.read_text(encoding="utf-8")))
    r = c.find_request("Auth/Get access token (debug)")
    assert r is not None and r.method == "POST"
    env = Environment(variables=[KV("token_url", f"{server}/token"), KV("client_id", "cid"),
                                 KV("client_secret", "csecret", secret=True)])
    assert missing_variables(r, c, env) == set()  # disabled scope/body-credential rows don't count
    _, res = engine.execute(r, c, env, SETTINGS)
    assert res.status == 200 and res.json()["access_token"] == "tok123"


def test_unknown_variable_is_not_sent(server):
    _, res = engine.execute(Request(url=f"{server}/echo/{{{{missing}}}}"), None, None, SETTINGS)
    assert res.status is None and "{{missing}}" in res.error


def test_json_body_and_curl(server):
    r = Request(method="POST", url=f"{server}/echo", body_mode="json", body='{"x": "{{v}}"}',
                auth=Auth(type="bearer", token="abc"))
    rr, res = engine.execute(r, None, None, SETTINGS, extra={"v": "42"})
    data = res.json()
    assert data["body"] == '{"x": "42"}' and data["content_type"] == "application/json"
    assert data["auth"] == "Bearer abc"
    curl = engine.to_curl(rr, SETTINGS)
    assert curl.startswith("curl -X POST") and "Bearer abc" in curl


def test_bulk_runner_csv_extract_and_429_retry(server):
    CALLS["rate"] = 0
    cols, rows = runner.read_csv_bytes("﻿id,note\n1,a\n2,b\n\n".encode("utf-8"))
    assert cols == ["id", "note"] and len(rows) == 2
    get = Request(name="get", url=f"{server}/rate", params=[KV("id", "{{id}}")])
    echo = Request(name="echo", url=f"{server}/echo", params=[KV("n", "{{name}}")])
    opts = runner.RunOptions(expected_status="2xx", extract="name=data.name")
    results = runner.run(Collection(), [get, echo], None, rows, opts, SETTINGS)
    assert [r.passed for r in results] == [True, True, True, True]
    assert results[1].response and json.loads(results[1].response)["query"]["n"] == "user-1"
    out = runner.results_to_csv(results)
    assert out.splitlines()[0].startswith("row,request,method,url,status,result")


def test_bulk_runner_stops_row_on_failure(server):
    bad = Request(name="bad", url=f"{server}/nothing-here")
    nxt = Request(name="next", url=f"{server}/echo")
    results = runner.run(Collection(), [bad, nxt], None, [{"a": "1"}, {"a": "2"}],
                         runner.RunOptions(), SETTINGS)
    assert [(r.row, r.request, r.passed) for r in results] == [(1, "bad", False), (2, "bad", False)]


def test_get_path_and_status():
    data = {"a": {"b": [{"c": 5}]}, "users": [{"id": "x"}]}
    assert runner.get_path(data, "a.b[0].c") == 5
    assert runner.get_path(data, "$.users.0.id") == "x"
    assert runner.get_path(data, "a.zzz") is None
    assert runner.status_matches(204, "2xx") and not runner.status_matches(404, "")
    assert runner.status_matches(201, "200, 201")


def test_postman_round_trip():
    pm = {
        "info": {"name": "PM", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "auth": {"type": "oauth1", "oauth1": [{"key": "consumerKey", "value": "{{ck}}"},
                                              {"key": "signatureMethod", "value": "HMAC-SHA256"}]},
        "variable": [{"key": "base", "value": "https://x"}],
        "item": [{"name": "F", "item": [{
            "name": "Get",
            "event": [{"listen": "test", "script": {"exec": ["pm.test()"]}}],
            "request": {"method": "GET", "header": [{"key": "Accept", "value": "application/json"}],
                        "url": {"raw": "{{base}}/u?limit=5", "query": [{"key": "limit", "value": "5"},
                                                                     {"key": "off", "value": "1", "disabled": True}]}},
        }]}],
    }
    assert postman.detect(pm) == "postman-collection"
    c, report = postman.import_collection(pm)
    assert report.requests == 1 and report.scripts_skipped == 1
    assert c.auth.type == "oauth1" and c.auth.signature_method == "HMAC-SHA256"
    folder = c.items[0]
    assert isinstance(folder, Folder)
    req = folder.items[0]
    assert req.url == "{{base}}/u" and [(p.key, p.enabled) for p in req.params] == [("limit", True), ("off", False)]
    assert c.find_request("f/get") is req
    again, _ = postman.import_collection(postman.export_collection(c))
    assert again.items[0].items[0].url == "{{base}}/u"
    # our own format round-trips exactly
    assert Collection.from_dict(c.to_dict()).to_dict() == c.to_dict()


def test_storage_round_trip(tmp_path):
    from restclient.storage import Store

    s = Settings(collections_dir=str(tmp_path / "c"), environments_dir=str(tmp_path / "e"))
    store = Store(s)
    c = Collection(name="My: Coll", items=[Request(name="r")])
    store.save_collection(c)
    assert c.path.name == "My_ Coll.json"
    c.name = "Renamed"
    store.save_collection(c)
    assert [p.name for p in (tmp_path / "c").glob("*.json")] == ["Renamed.json"]
    loaded = store.load_collections()
    assert loaded[0].items[0].name == "r"
    store.delete_collection(loaded[0])
    assert (tmp_path / "c" / ".trash" / "Renamed.json").exists()


def test_bundled_examples_load():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "examples"
    for f in (root / "collections").glob("*.json"):
        c = Collection.from_dict(json.loads(f.read_text(encoding="utf-8")))
        assert any(True for _ in c.walk())
    for f in (root / "environments").glob("*.json"):
        Environment.from_dict(json.loads(f.read_text(encoding="utf-8")))
