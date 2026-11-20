"""Everyday support helpers: plain-English error tips, "copy for ticket", connection tests,
recent lookups (kept on this computer only) and summarising a data file for the assistant.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import engine, redact
from .config import Settings, app_home
from .models import Collection, Environment, Request
from .variables import auth_strings, build_scope, effective_auth, find_vars, request_variables, substitute


# ================================================================== error tips ===
def error_tip(res: engine.Result, auth_type: str = "") -> str:
    """One line on what a failed call means and what to try next ('' for a success)."""
    if res.error:
        e = res.error.lower()
        if e.startswith("unknown variable"):
            return "Some {{variables}} have no value. Pick the right environment, or click a red variable to set it."
        if "ssl" in e or "certificate" in e:
            return ("Secure connection problem. On the company network turn on 'Use system certificates' in "
                    "Settings and restart the app.")
        if "timed out" in e:
            return "The server took too long. Try again; for big lists turn off 'All pages' or add a limit."
        if "oauth 2.0 token" in e:
            return ("Getting a token failed: the client ID/secret or token URL in this environment is wrong, or "
                    "the SIS hasn't enabled the API app.")
        if any(w in e for w in ("name or service not known", "nodename nor servname", "getaddrinfo",
                                "failed to resolve", "no such host")):
            return "The server name can't be found. Check the base URL in the environment for typos."
        if "connection" in e and ("refused" in e or "reset" in e or "aborted" in e):
            return "Couldn't connect. Check the base URL, your internet / VPN, and that the server is up."
        if "url is empty" in e:
            return "Type a URL, or pick a saved request on the left."
        return ""
    status = res.status or 0
    if status < 400:
        return ""
    signed = auth_type in ("oauth1", "oauth2")
    tips = {
        400: "The server didn't accept the request: check the ids, parameters and body.",
        401: ("The key/secret was rejected. Check you picked the right district environment and that its key "
              "and secret are for this server" + (" (OAuth 1.0 also fails if this computer's clock is wrong)."
                                                   if auth_type == "oauth1" else ".")),
        403: ("The credentials work but aren't allowed to do this. The API app may lack this permission, or "
              "the user is in a school the key can't see."),
        404: "Not found: that id doesn't exist here (or it belongs to another district), or the URL path is wrong.",
        405: "This server doesn't allow that method on this URL: check GET/POST/PUT.",
        409: "Conflict: it already exists or clashes with another record (e.g. a duplicate unique id).",
        415: "The server doesn't accept that body format: check the Content-Type and body type.",
        422: "The data was understood but rejected: a field is missing or has an invalid value.",
        429: "Too many requests: wait a minute, or add a delay in the Bulk Runner.",
        500: "The server had an error. Try again; if it keeps happening, copy for a ticket and escalate.",
        502: "The server (or a proxy) is having trouble. Try again in a few minutes.",
        503: "The server is busy or down for maintenance. Try again later.",
        504: "The server took too long to answer. Try again; for big lists add a limit.",
    }
    tip = tips.get(status) or ("The server had an error. Try again later." if status >= 500 else
                               "The server rejected the request: the response body usually says why.")
    if status in (401, 403) and not signed and auth_type in ("", "none"):
        tip += " This request sends no credentials at all: check its Auth tab."
    return f"{status}: {tip}"


# ============================================================ copy for ticket ===
def key_fields(data, limit: int = 15) -> list[tuple[str, str]]:
    """The interesting top-level values of a response, for a ticket."""
    if isinstance(data, dict) and len(data) == 1:
        (key, inner), = data.items()
        if isinstance(inner, (dict, list)):  # OneRoster wraps things: {"user": {...}} / {"users": [...]}
            data = inner
    if isinstance(data, list):
        out = [("items returned", str(len(data)))]
        for i, item in enumerate(data[:5], 1):
            if isinstance(item, dict):
                ident = item.get("sourcedId") or item.get("id") or item.get("uid") or ""
                name = item.get("name") or item.get("title") or item.get("name_display") or \
                    " ".join(str(item.get(k, "")) for k in ("givenName", "familyName")).strip() or \
                    " ".join(str(item.get(k, "")) for k in ("name_first", "name_last")).strip()
                out.append((f"item {i}", " · ".join(str(x) for x in (ident, name) if x)))
        return out
    if isinstance(data, dict):
        out = []
        for k, v in data.items():
            if isinstance(v, (dict, list)) or v in (None, ""):
                continue
            out.append((k, str(v)[:120]))
            if len(out) >= limit:
                break
        return out
    return []


def ticket_text(title: str, environment: str, rr: engine.ResolvedRequest, res: engine.Result,
                auth_type: str = "") -> str:
    lines = [f"Request: {res.method or rr.method} {title}",
             f"Environment: {environment or '(none)'}",
             f"URL: {res.url or rr.url}",
             f"When: {datetime.now():%Y-%m-%d %H:%M} (local time)"]
    if res.error:
        lines.append(f"Result: ERROR - {res.error}")
    else:
        lines.append(f"Result: {res.status} {res.reason} in {res.elapsed_ms} ms")
    tip = error_tip(res, auth_type)
    if tip:
        lines.append(f"Meaning: {tip}")
    if res.note:
        lines.append(f"Note: {res.note}")
    fields = key_fields(res.json()) if not res.error else []
    if fields:
        lines.append("Key fields:")
        lines += [f"  {k}: {v}" for k, v in fields]
    elif res.text and not res.error and res.status and res.status >= 400:
        lines.append("Response: " + " ".join(res.text.split())[:400])
    return redact.redact("\n".join(lines), rr.auth.secret_values())


# ============================================================ connection test ===
HEALTH_NAMES = re.compile(r"who am i|app-user-info|all orgs|all schools|list schools|academic sessions", re.I)


def pick_health_request(collections: list[Collection], environment: Environment) -> tuple[Collection, Request] | None:
    """A harmless GET that works with this environment and needs no ids (e.g. 'Who am I', 'All orgs')."""
    from .assistant import matching_collections  # shares the "which collections fit this district" logic

    class _WS:  # matching_collections only needs .collections and .environment()
        def __init__(self):
            self.collections = collections

        def environment(self, _):
            return environment

    fits = matching_collections(_WS(), environment.name)
    if len(fits) == len(collections) and len(collections) > 1:
        return None  # nothing in this environment points at a collection: can't tell what to test
    best = None
    for c in fits:
        for _, r in c.walk():
            if r.method != "GET" or request_missing(r, c, environment):
                continue
            if HEALTH_NAMES.search(r.name) or HEALTH_NAMES.search(r.url):
                return c, r
            best = best or (c, r)
    return best


def request_missing(r: Request, c: Collection, env: Environment) -> set[str]:
    from .variables import missing_variables

    return missing_variables(r, c, env)


@dataclass
class HealthResult:
    ok: bool
    summary: str
    tip: str = ""
    request: str = ""


def health_check(collections: list[Collection], environment: Environment, settings: Settings) -> HealthResult:
    picked = pick_health_request(collections, environment)
    if picked is None:
        return HealthResult(False, "Nothing to test with",
                            "Fill in this environment's base URL and keys first: nothing in it matches a collection yet.")
    c, r = picked
    probe = r.clone()
    if not any(p.key == "limit" for p in probe.params):
        from .models import KV

        probe.params.append(KV("limit", "1"))  # keep it tiny
    rr, res = engine.execute(probe, c, environment, settings, all_pages=False)
    label = f"{c.name}: {c.request_label(r)}"
    if res.ok:
        return HealthResult(True, f"Connected ({res.status} {res.reason}, {res.elapsed_ms} ms)", request=label)
    auth = effective_auth(r, c, environment).type
    summary = f"Failed: {res.status} {res.reason}".strip() if not res.error else "Failed: couldn't connect"
    return HealthResult(False, summary, redact.redact(error_tip(res, auth) or res.error or ""), label)


# ============================================================= recent lookups ===
RECENT_FILE = "recent_lookups.json"  # in the app folder (~/SchoologyAPIClient), never in the repo
RECENT_KEEP = 30
_recent_lock = threading.Lock()
_SECRET_NAMES = ("secret", "password", "token", "api_key", "apikey", "key")


def recent_path() -> Path:
    return app_home() / RECENT_FILE


def _connection_names(request: Request, c: Collection | None, env: Environment | None) -> set[str]:
    """Variables that belong to the district connection (URLs, keys), not to the lookup itself."""
    keep: set[str] = set()
    for text in auth_strings(effective_auth(request, c, env)):
        keep |= find_vars(text)
    for holder in (c, env):
        for v in (holder.variables if holder else []):
            if v.secret or v.value.strip().lower().startswith(("http://", "https://")) \
                    or any(h in v.key.lower() for h in _SECRET_NAMES):
                keep.add(v.key)
    return keep


def bake(request: Request, c: Collection | None, env: Environment | None) -> tuple[Request, dict]:
    """A copy of the request with the ids it used filled in (user_id -> 12345), keeping the base URL and
    credentials as {{variables}} so nothing secret is stored. Returns (copy, the ids used)."""
    keep = _connection_names(request, c, env)
    scope = build_scope(c, env)
    used = {n: scope[n] for n in sorted(request_variables(request, c, env)) if n in scope and n not in keep}
    baked = request.clone(new_identity=True)
    fill = lambda text: substitute(text or "", used)  # noqa: E731 - leaves every other {{variable}} alone
    baked.url, baked.body = fill(baked.url), fill(baked.body)
    for kv in baked.params + baked.headers + baked.form:
        kv.key, kv.value = fill(kv.key), fill(kv.value)
    if baked.auth.type not in ("inherit", "none"):
        from .models import Auth

        baked.auth = Auth(type="inherit")  # its own credentials aren't stored; re-runs use the saved request's
    return baked, used


def load_recent() -> list[dict]:
    try:
        data = json.loads(recent_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_lookup(request: Request, c: Collection | None, env: Environment | None, res: engine.Result,
                  previous: dict | None = None) -> None:
    """Remember a sent request under Recent. previous = the Recent entry it was re-run from (keeps its name/ids)."""
    baked, used = bake(request, c, env)
    data = baked.to_dict()
    same_lookup = previous is not None and previous.get("request", {}).get("url") == data["url"]
    entry = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "environment": env.name if env else "",
        "environment_id": env.id if env else "",
        "collection_id": c.id if c else "",
        "collection": c.name if c else "",
        "request_id": previous.get("request_id", request.id) if same_lookup else request.id,
        "label": previous.get("label") if same_lookup else
        c.request_label(request) if c and c.find_container(request.id) is not None else request.name,
        "method": request.method,
        "values": (previous.get("values") or {}) if same_lookup else {k: redact.redact(v) for k, v in used.items()},
        "status": res.status if not res.error else "error",
        "request": json.loads(redact.redact(json.dumps(data))),
    }
    key = (entry["environment_id"], entry["collection_id"], data["method"], data["url"],
           json.dumps(data["params"]), data["body"])
    with _recent_lock:
        items = [e for e in load_recent()
                 if (e.get("environment_id"), e.get("collection_id"), e.get("request", {}).get("method"),
                     e.get("request", {}).get("url"), json.dumps(e.get("request", {}).get("params")),
                     e.get("request", {}).get("body")) != key]
        items.insert(0, entry)
        path = recent_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items[:RECENT_KEEP], indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def clear_recent() -> None:
    with _recent_lock:
        recent_path().unlink(missing_ok=True)


def recent_title(entry: dict) -> str:
    values = ", ".join(f"{k}={v}" for k, v in (entry.get("values") or {}).items())
    return f"{entry.get('label', '?')}" + (f"  ({values})" if values else "")


def age_text(iso: str) -> str:
    try:
        seconds = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return ""
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


# ========================================================== data attachments ===
ATTACH_TYPES = (".csv", ".tsv", ".txt", ".json", ".md", ".log")
ATTACH_CHARS = 8000  # ~2-3k tokens: enough to reason about, small enough for a laptop model


def summarize_attachment(name: str, data: bytes, limit: int = ATTACH_CHARS) -> tuple[str, str]:
    """Turn an exported query result (CSV/JSON/text) into text for the assistant.
    Returns (text for the model, short description for the screen)."""
    lower = name.lower()
    if lower.endswith((".csv", ".tsv")):
        from .runner import read_csv_bytes

        columns, rows = read_csv_bytes(data)
        header = ",".join(columns)
        stats = column_summary(columns, rows)
        limit = max(limit - len(stats), limit // 2)
        body, shown = [], 0
        size = len(header)
        for row in rows:
            line = ",".join(_csv_cell(row.get(c, "")) for c in columns)
            if size + len(line) + 1 > limit:
                break
            body.append(line)
            size += len(line) + 1
            shown += 1
        note = f"{len(rows)} rows, {len(columns)} columns" + (f" (first {shown} rows shown to the AI)"
                                                              if shown < len(rows) else "")
        return (f"Columns: {header}\nRows: {len(rows)}\nCounted by the app (exact, use these numbers):\n{stats}\n\n"
                + "\n".join([header] + body)), note
    text = data.decode("utf-8-sig", errors="replace")
    if lower.endswith(".json"):
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    cut = len(text) > limit
    return text[:limit] + ("\n... [cut]" if cut else ""), f"{len(text):,} characters" + (" (shortened)" if cut else "")


def column_summary(columns: list[str], rows: list[dict], max_values: int = 12) -> str:
    """Exact counts small models get wrong: blanks per column, and how often each value appears in
    columns with few distinct values (role, school, status, ...)."""
    from collections import Counter

    lines = []
    for col in columns:
        values = [str(r.get(col, "")).strip() for r in rows]
        blank = sum(1 for v in values if not v)
        counts = Counter(v for v in values if v)
        parts = [f"{blank} rows have no {col}"] if blank else []
        if counts and len(counts) <= max_values and len(counts) < len(rows):
            parts += [f"{n} rows have {col} = {v}" for v, n in counts.most_common()]
        elif counts:
            parts.append(f"{len(counts)} different values")
        lines.append(f"- {col}: " + ("; ".join(parts) or "all blank"))
    return "\n".join(lines)


def _csv_cell(value: str) -> str:
    value = str(value)
    return '"' + value.replace('"', '""') + '"' if any(ch in value for ch in ',"\n') else value
