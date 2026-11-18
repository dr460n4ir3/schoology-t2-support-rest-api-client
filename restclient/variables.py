"""{{variable}} substitution, Postman style.

Precedence (later wins): collection variables < environment variables < run-time values
(CSV row columns, values passed by the AI / CLI, values extracted from earlier responses).
"""

from __future__ import annotations

import random
import re
import uuid
from datetime import datetime, timezone

from .models import Auth, Collection, Environment, Request

VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _dynamic(name: str) -> str | None:
    if name == "$guid" or name == "$randomUUID":
        return str(uuid.uuid4())
    if name == "$timestamp":
        return str(int(datetime.now(timezone.utc).timestamp()))
    if name == "$isoTimestamp":
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if name == "$randomInt":
        return str(random.randint(0, 1000))
    return None


def is_unset(value: str) -> bool:
    """Blank values and the example placeholders ("PASTE-CONSUMER-KEY", ...) count as not set."""
    v = (value or "").strip()
    return v == "" or v.upper().startswith("PASTE-")


def build_scope(collection: Collection | None, environment: Environment | None,
                extra: dict | None = None) -> dict[str, str]:
    # Blank/placeholder collection/environment values count as "not set": sending /users/ with an empty
    # id, or a key that still says PASTE-..., is always a mistake, so we would rather warn.
    # (A blank CSV cell is still used as-is.)
    scope: dict[str, str] = {}
    if collection:
        scope.update({v.key: v.value for v in collection.variables if v.enabled and v.key and not is_unset(v.value)})
    if environment:
        scope.update({v.key: v.value for v in environment.variables
                      if v.enabled and v.key and not is_unset(v.value)})
    if extra:
        scope.update({str(k).strip(): "" if v is None else str(v) for k, v in extra.items() if str(k).strip()})
    return scope


def substitute(text: str, scope: dict[str, str]) -> str:
    """Replace {{name}} with its value. Unknown names are left untouched so they are easy to spot."""
    if not text or "{{" not in text:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in scope:
            return scope[name]
        dyn = _dynamic(name)
        return dyn if dyn is not None else m.group(0)

    for _ in range(5):  # allow variables that reference other variables
        new = VAR_RE.sub(repl, text)
        if new == text:
            break
        text = new
    return text


def find_vars(text: str) -> set[str]:
    return {m.group(1) for m in VAR_RE.finditer(text or "")}


def auth_strings(auth: Auth) -> list[str]:
    return [str(v) for k, v in auth.to_dict().items() if k != "type"]


def effective_auth(request: Request, collection: Collection | None, environment: Environment | None) -> Auth:
    """Request auth wins; 'inherit' falls back to the environment override, then the collection."""
    if request.auth.type != "inherit":
        return request.auth
    if environment and environment.auth.type != "inherit":
        return environment.auth
    if collection:
        return collection.auth
    return Auth(type="none")


def request_variables(request: Request, collection: Collection | None = None,
                      environment: Environment | None = None) -> set[str]:
    """Every {{variable}} a request needs, including ones used by its (inherited) auth."""
    texts = [request.url, request.body]
    for kv in request.params + request.headers + request.form:
        if kv.enabled:
            texts += [kv.key, kv.value]
    texts += auth_strings(effective_auth(request, collection, environment))
    found: set[str] = set()
    for t in texts:
        found |= find_vars(t)
    return {v for v in found if not v.startswith("$")}


def missing_variables(request: Request, collection: Collection | None, environment: Environment | None,
                      extra: dict | None = None) -> set[str]:
    scope = build_scope(collection, environment, extra)
    needed = request_variables(request, collection, environment)
    # also catch variables that are defined but whose value references an undefined variable
    unresolved = set()
    for name in needed:
        if name not in scope:
            unresolved.add(name)
        else:
            unresolved |= {v for v in find_vars(substitute(scope[name], scope)) if not v.startswith("$")}
    return unresolved
