"""Turns a saved Request into a real HTTP call using `requests`.

Used by the UI, the bulk runner, the CLI and the MCP (AI) server, so all of them behave
the same way.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from requests.auth import HTTPBasicAuth
from requests.structures import CaseInsensitiveDict
from requests_oauthlib import OAuth1

from . import __version__, redact
from .config import Settings
from .models import Auth, Collection, Environment, Request
from .variables import VAR_RE, build_scope, effective_auth, substitute

USER_AGENT = f"SchoologyAPIClient/{__version__} (python-requests)"


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Resolution: apply {{variables}} and pick the effective auth
# --------------------------------------------------------------------------------------
@dataclass
class ResolvedRequest:
    method: str
    url: str
    params: list[tuple[str, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    body_mode: str = "none"
    body: str = ""
    form: list[tuple[str, str]] = field(default_factory=list)
    auth: Auth = field(default_factory=lambda: Auth(type="none"))
    name: str = ""

    def unresolved(self) -> set[str]:
        texts = [self.url, self.body] + [x for kv in self.params + self.headers + self.form for x in kv]
        texts += [str(v) for k, v in self.auth.to_dict().items() if k != "type"]
        return {m.group(1) for t in texts for m in VAR_RE.finditer(t or "")}

    def describe(self, mask_secrets: bool = True) -> str:
        """Human-readable preview (used by 'Preview' buttons and the AI)."""
        lines = [f"{self.method} {self.url}"]
        for k, v in self.params:
            lines.append(f"  param  {k} = {v}")
        for k, v in self.headers:
            lines.append(f"  header {k}: {mask(v) if mask_secrets and k.lower() == 'authorization' else v}")
        a = self.auth
        if a.type == "oauth1":
            lines.append(f"  auth   OAuth 1.0 ({a.signature_method}), consumer key {mask(a.consumer_key, 6)}")
        elif a.type == "oauth2":
            lines.append(f"  auth   OAuth 2.0 client credentials, client id {mask(a.client_id, 6)}, token URL {a.token_url}")
        elif a.type == "bearer":
            lines.append(f"  auth   Bearer {mask(a.token)}")
        elif a.type == "basic":
            lines.append(f"  auth   Basic {a.username} / ****")
        elif a.type == "apikey":
            lines.append(f"  auth   API key {a.key_name} in {a.key_in}")
        if self.body_mode in ("json", "text") and self.body:
            lines += ["", self.body]
        elif self.body_mode in ("urlencoded", "form"):
            lines += [""] + [f"  {k} = {v}" for k, v in self.form]
        return "\n".join(lines)


def mask(value: str, keep: int = 10) -> str:
    value = value or ""
    return value if len(value) <= keep else value[:keep] + "…"


def resolve(request: Request, collection: Collection | None = None, environment: Environment | None = None,
            extra: dict | None = None) -> ResolvedRequest:
    scope = build_scope(collection, environment, extra)

    def s(text: str) -> str:
        return substitute(text or "", scope)

    url = s(request.url).strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url

    auth = effective_auth(request, collection, environment)
    # Credentials never contain leading/trailing spaces, but copy-pasted keys often do (-> 401)
    auth_data = {k: (s(v).strip() if k != "type" else v) for k, v in auth.to_dict().items()}
    resolved_auth = Auth.from_dict(auth_data, auth.type)
    redact.register(*resolved_auth.secret_values())

    def pairs(items):
        return [(s(kv.key), s(kv.value)) for kv in items if kv.enabled and kv.key.strip()]

    return ResolvedRequest(
        method=request.method.upper(),
        url=url,
        params=pairs(request.params),
        headers=pairs(request.headers),
        body_mode=request.body_mode,
        body=s(request.body) if request.body_mode in ("json", "text") else "",
        form=pairs(request.form) if request.body_mode in ("urlencoded", "form") else [],
        auth=resolved_auth,
        name=request.name,
    )


# --------------------------------------------------------------------------------------
# OAuth 2.0 client-credentials tokens (cached until shortly before they expire)
# --------------------------------------------------------------------------------------
_token_cache: dict[tuple, tuple[str, float]] = {}
_token_lock = threading.Lock()


def _token_key(a: Auth) -> tuple:
    return (a.token_url, a.client_id, a.client_secret, a.scope, a.client_auth)


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


def get_oauth2_token(a: Auth, settings: Settings, session: requests.Session, force: bool = False) -> str:
    if not a.token_url:
        raise AuthError("OAuth 2.0: the Token URL is empty.")
    key = _token_key(a)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        data = {"grant_type": "client_credentials"}
        if a.scope:
            data["scope"] = a.scope
        basic = None
        if a.client_auth == "body":
            data.update(client_id=a.client_id, client_secret=a.client_secret)
        else:
            basic = HTTPBasicAuth(a.client_id, a.client_secret)
        try:
            resp = session.post(a.token_url, data=data, auth=basic, headers={"Accept": "application/json"},
                                timeout=settings.timeout_seconds, verify=settings.verify_ssl)
        except requests.RequestException as exc:
            raise AuthError(f"OAuth 2.0 token request to {a.token_url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise AuthError(f"OAuth 2.0 token request failed: HTTP {resp.status_code} {resp.text[:500]}")
        try:
            payload = resp.json()
            token = payload["access_token"]
        except (ValueError, KeyError) as exc:
            raise AuthError(f"OAuth 2.0 token response had no access_token: {resp.text[:500]}") from exc
        expires_in = float(payload.get("expires_in") or 3600)
        _token_cache[key] = (token, time.time() + max(expires_in - 60, 30))
        redact.register(token)
        return token


# --------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------
@dataclass
class Result:
    method: str = ""
    url: str = ""
    status: int | None = None
    reason: str = ""
    elapsed_ms: int = 0
    size: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    error: str | None = None
    note: str = ""  # e.g. "Fetched 4 pages"
    links: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    def pretty_body(self) -> str:
        data = self.json()
        if data is not None and self.text.strip()[:1] in "[{":
            return json.dumps(data, indent=2, ensure_ascii=False)
        return self.text

    def summary(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        return f"{self.status} {self.reason} · {self.elapsed_ms} ms · {human_size(self.size)}"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def prepare(rr: ResolvedRequest, settings: Settings, session: requests.Session,
            force_new_token: bool = False) -> requests.PreparedRequest:
    """Build the exact request that will go on the wire (auth applied, body encoded)."""
    headers = CaseInsensitiveDict()
    for k, v in rr.headers:
        headers[k] = v
    params = list(rr.params)
    data = files = None

    if rr.body_mode == "json" and rr.body.strip():
        data = rr.body.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif rr.body_mode == "text" and rr.body:
        data = rr.body.encode("utf-8")
        headers.setdefault("Content-Type", "application/xml" if rr.body.lstrip().startswith("<") else "text/plain")
    elif rr.body_mode == "urlencoded":
        data = rr.form
    elif rr.body_mode == "form":
        files = [(k, (None, v)) for k, v in rr.form]
    headers.setdefault("Accept", "application/json, */*;q=0.8")

    a = rr.auth
    auth_obj = None
    if a.type == "bearer" and a.token:
        headers.setdefault("Authorization", f"Bearer {a.token}")
    elif a.type == "basic":
        auth_obj = HTTPBasicAuth(a.username, a.password)
    elif a.type == "apikey" and a.key_name:
        if a.key_in == "query":
            params.append((a.key_name, a.key_value))
        else:
            headers.setdefault(a.key_name, a.key_value)
    elif a.type == "oauth1":
        auth_obj = OAuth1(
            client_key=a.consumer_key,
            client_secret=a.consumer_secret or None,
            resource_owner_key=a.access_token or None,
            resource_owner_secret=a.token_secret or None,
            signature_method=a.signature_method or "HMAC-SHA1",
            realm=a.realm or None,
        )
    elif a.type == "oauth2":
        token = get_oauth2_token(a, settings, session, force=force_new_token)
        headers.setdefault("Authorization", f"Bearer {token}")

    req = requests.Request(rr.method, rr.url, headers=dict(headers), params=params,
                           data=data, files=files, auth=auth_obj)
    return session.prepare_request(req)


def _redirect_target(rr: ResolvedRequest, resp: requests.Response) -> ResolvedRequest:
    location = urljoin(resp.url, resp.headers["Location"])
    method = rr.method
    keep_body = True
    if resp.status_code == 303 or (resp.status_code in (301, 302) and rr.method == "POST"):
        method, keep_body = ("GET" if rr.method != "HEAD" else "HEAD"), False
    same_host = urlparse(location).netloc == urlparse(rr.url).netloc
    headers = [(k, v) for k, v in rr.headers
               if (keep_body or k.lower() != "content-type") and (same_host or k.lower() != "authorization")]
    return ResolvedRequest(
        method=method, url=location, params=[], headers=headers,
        body_mode=rr.body_mode if keep_body else "none",
        body=rr.body if keep_body else "", form=rr.form if keep_body else [],
        auth=rr.auth if same_host else Auth(type="none"), name=rr.name,
    )


def send(rr: ResolvedRequest, settings: Settings, session: requests.Session | None = None,
         max_redirects: int = 10) -> Result:
    """Send one request. Never raises: problems are reported in Result.error."""
    session = session or new_session()
    result = Result(method=rr.method, url=rr.url)
    if not rr.url:
        result.error = "The URL is empty."
        return result
    leftover = sorted(rr.unresolved())
    if leftover:
        result.error = "Unknown variable(s): " + ", ".join("{{%s}}" % v for v in leftover) + \
                       ". Pick an environment or add them to the environment/collection/CSV."
        return result

    start = time.perf_counter()
    current = rr
    retried_token = False
    try:
        for _ in range(max_redirects + 1):
            prep = prepare(current, settings, session)
            env = session.merge_environment_settings(prep.url, {}, None, settings.verify_ssl, None)
            resp = session.send(prep, timeout=settings.timeout_seconds, allow_redirects=False, **env)
            if resp.status_code == 401 and current.auth.type == "oauth2" and not retried_token:
                retried_token = True  # token may have been revoked/expired early: get a fresh one once
                prep = prepare(current, settings, session, force_new_token=True)
                resp = session.send(prep, timeout=settings.timeout_seconds, allow_redirects=False, **env)
            if resp.is_redirect and "Location" in resp.headers:
                # Follow redirects ourselves so OAuth signatures are re-generated for the new URL
                # (Schoology answers some calls, e.g. /users/me, with a 303 redirect).
                current = _redirect_target(current, resp)
                continue
            break
    except AuthError as exc:
        result.error = str(exc)
        return result
    except requests.exceptions.SSLError as exc:
        result.error = (f"SSL certificate problem: {exc}. If you are on the company network, make sure "
                        f"'Use system certificates' is on in Settings (or, as a last resort, turn off SSL verification).")
        return result
    except requests.exceptions.Timeout:
        result.error = f"Timed out after {settings.timeout_seconds} seconds."
        return result
    except requests.RequestException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.elapsed_ms = int((time.perf_counter() - start) * 1000)

    result.url = resp.url
    result.method = prep.method or rr.method
    result.status = resp.status_code
    result.reason = resp.reason or ""
    result.headers = dict(resp.headers)
    result.request_headers = {}
    for k, v in prep.headers.items():
        k = k.decode("latin-1") if isinstance(k, bytes) else k
        v = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        result.request_headers[k] = mask(v, 24) if k.lower() == "authorization" else v
    result.size = len(resp.content)
    result.links = {k: v.get("url") for k, v in (resp.links or {}).items()}
    result.text = resp.content.decode(resp.encoding or "utf-8", errors="replace") if resp.content else ""
    return result


# --------------------------------------------------------------------------------------
# "Fetch all pages" (OneRoster limit/offset paging, Link: rel="next", X-Total-Count)
# --------------------------------------------------------------------------------------
def _list_key(data) -> str | None:
    if isinstance(data, dict):
        keys = [k for k, v in data.items() if isinstance(v, list)]
        if len(keys) == 1:
            return keys[0]
    return None


def _with_param(rr: ResolvedRequest, name: str, value: str) -> ResolvedRequest:
    params = [(k, v) for k, v in rr.params if k != name] + [(name, value)]
    return ResolvedRequest(**{**rr.__dict__, "params": params})


def send_all_pages(rr: ResolvedRequest, settings: Settings, session: requests.Session | None = None,
                   max_pages: int = 200, page_size: int = 100) -> Result:
    session = session or new_session()
    if not any(k == "limit" for k, _ in rr.params) and "limit=" not in rr.url:
        rr = _with_param(rr, "limit", str(page_size))
    first = send(rr, settings, session)
    if not first.ok:
        return first
    data = first.json()
    key = _list_key(data)
    if key is None:
        first.note = "Fetch all pages: response is not a paged list, showing the single response."
        return first

    items = list(data[key])
    limit = int(dict(rr.params).get("limit", page_size) or page_size)
    total = int(first.headers.get("X-Total-Count") or first.headers.get("x-total-count") or 0) or None
    pages, elapsed, size, page, problem = 1, first.elapsed_ms, first.size, first, ""
    last_batch = items
    while pages < max_pages:
        next_url = page.links.get("next")
        if next_url:
            nxt = ResolvedRequest(**{**rr.__dict__, "url": next_url, "params": []})
        elif (total and len(items) < total) or (not total and len(last_batch) >= limit):
            nxt = _with_param(rr, "offset", str(len(items)))
        else:
            break
        page = send(nxt, settings, session)
        elapsed += page.elapsed_ms
        size += page.size
        if not page.ok:
            problem = f" Stopped early: page {pages + 1} returned {page.summary()}"
            break
        batch = (page.json() or {}).get(key) or []
        if not batch or batch == last_batch:
            break
        items.extend(batch)
        last_batch = batch
        pages += 1
    else:
        problem = f" Stopped at the {max_pages}-page safety limit."

    first.text = json.dumps({key: items}, indent=2, ensure_ascii=False)
    first.elapsed_ms, first.size = elapsed, size
    first.note = f"Fetched {pages} page(s), {len(items)} {key}" + (f" (server reports {total} total)" if total else "") + "." + problem
    return first


def execute(request: Request, collection: Collection | None, environment: Environment | None, settings: Settings,
            extra: dict | None = None, session: requests.Session | None = None,
            all_pages: bool | None = None) -> tuple[ResolvedRequest, Result]:
    rr = resolve(request, collection, environment, extra)
    paged = request.fetch_all_pages if all_pages is None else all_pages
    result = send_all_pages(rr, settings, session) if paged else send(rr, settings, session)
    return rr, result


# --------------------------------------------------------------------------------------
# cURL export (handy for handing a reproducible call to engineering / a vendor)
# --------------------------------------------------------------------------------------
def to_curl(rr: ResolvedRequest, settings: Settings, include_credentials: bool = False) -> str:
    """The request as a curl command. Credentials are hidden unless include_credentials is True."""
    prep = prepare(rr, settings, new_session())
    parts = ["curl", "-X", prep.method or rr.method, shlex.quote(prep.url or rr.url)]
    for k, v in prep.headers.items():
        k = k.decode("latin-1") if isinstance(k, bytes) else k
        v = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        if k.lower() in ("content-length", "accept-encoding", "connection"):
            continue
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    body = prep.body
    if body:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        parts += ["--data-raw", shlex.quote(body)]
    if not settings.verify_ssl:
        parts.append("-k")
    text = " ".join(parts)
    return text if include_credentials else redact.redact(text, rr.auth.secret_values())
