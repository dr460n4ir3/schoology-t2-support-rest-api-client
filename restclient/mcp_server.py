"""MCP server: lets an AI assistant use your collections.

Works with:
  * GitHub Copilot (VS Code agent mode)   -> .vscode/mcp.json  (see README)
  * mcpo -> Open WebUI / any OpenAPI-tool chatbot:
        uvx mcpo --port 8000 -- <path>\\.venv\\Scripts\\python.exe -m restclient.mcp_server
  * Claude Desktop, Cursor, etc. (stdio)

Safety defaults (change in Settings or settings.json):
  * mcp_allow_write_methods = false  -> the AI can only send GET/HEAD/OPTIONS
  * mcp_allow_adhoc_requests = false -> the AI can only call requests saved in a collection
Secrets in environments are never returned to the AI.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import engine, runner
from .models import KV, Folder, Request
from .variables import request_variables
from .workspace import Workspace

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

mcp = MCPServer(
    name="schoology-api-client",
    instructions=(
        "Tools for the Schoology T2 support team's REST API client. Use list_collections / list_requests "
        "to discover saved requests (Schoology API, OneRoster 1.1/1.2 for PowerSchool SIS, Infinite Campus, ...), "
        "list_environments to see which district/SIS environments exist, then send_saved_request. "
        "Pass values for {{variables}} via the 'variables' argument. Prefer preview_request before anything "
        "that changes data. Never guess credentials."
    ),
)


def _ws() -> Workspace:
    return Workspace(seed=False)  # re-read files every call so edits made in the app are picked up


def _check_method(ws: Workspace, method: str) -> str | None:
    if method.upper() not in READ_ONLY_METHODS and not ws.settings.mcp_allow_write_methods:
        return (f"Blocked: {method.upper()} requests from the AI are disabled (read-only mode). A human can run "
                f"this in the API Client app, or an admin can enable 'Allow AI to change data' in Settings.")
    return None


def _result_dict(ws: Workspace, rr: engine.ResolvedRequest, res: engine.Result) -> dict:
    limit = ws.settings.mcp_max_response_chars
    body = res.pretty_body()
    truncated = len(body) > limit
    keep_headers = {k: v for k, v in res.headers.items()
                    if k.lower() in ("content-type", "x-total-count", "link", "location", "retry-after",
                                     "x-ratelimit-remaining", "x-ratelimit-limit")}
    return {
        "request": rr.describe(mask_secrets=True),
        "status": res.status,
        "reason": res.reason,
        "error": res.error,
        "elapsed_ms": res.elapsed_ms,
        "note": res.note,
        "headers": keep_headers,
        "body": body[:limit] + (f"\n... [truncated, {len(body)} characters total]" if truncated else ""),
    }


@mcp.tool()
def list_collections() -> list[dict]:
    """List the saved API collections (e.g. Schoology API, OneRoster 1.1, OneRoster 1.2)."""
    ws = _ws()
    return [{"name": c.name, "description": c.description, "requests": sum(1 for _ in c.walk()),
             "default_auth": c.auth.type} for c in ws.collections]


@mcp.tool()
def list_requests(collection: str) -> list[dict]:
    """List the saved requests in a collection, with the {{variables}} each one needs."""
    ws = _ws()
    c = ws.collection(collection)
    return [{"request": "/".join(trail + [r.name]), "method": r.method, "url": r.url,
             "description": r.description, "variables": sorted(request_variables(r, c))}
            for trail, r in c.walk()]


@mcp.tool()
def list_environments() -> list[dict]:
    """List environments (one per district / SIS connection). Secret values are hidden."""
    ws = _ws()
    return [{"name": e.name, "description": e.description, "auth_override": e.auth.type,
             "variables": {v.key: ("<secret>" if v.secret else v.value) for v in e.variables if v.enabled}}
            for e in ws.environments]


@mcp.tool()
def preview_request(collection: str, request: str, environment: str | None = None,
                    variables: dict[str, str] | None = None) -> dict:
    """Show exactly what a saved request would send (URL, params, headers, body) WITHOUT sending it."""
    ws = _ws()
    c = ws.collection(collection)
    req = c.find_request(request)
    if req is None:
        raise LookupError(f"No request '{request}' in '{c.name}'. Use list_requests.")
    rr = engine.resolve(req, c, ws.environment(environment), variables)
    return {"preview": rr.describe(mask_secrets=True),
            "unresolved_variables": sorted(rr.unresolved())}


@mcp.tool()
def send_saved_request(collection: str, request: str, environment: str | None = None,
                       variables: dict[str, str] | None = None, all_pages: bool = False) -> dict:
    """Send a request saved in a collection.

    request: the request name or 'Folder/Name' path (see list_requests).
    environment: environment name (district / SIS). Needed for credentials and base URLs.
    variables: values for {{variables}}, e.g. {"user_id": "12345"}.
    all_pages: follow OneRoster paging (limit/offset, Link next) and merge every page.
    """
    ws = _ws()
    c = ws.collection(collection)
    req = c.find_request(request)
    if req is None:
        raise LookupError(f"No request '{request}' in '{c.name}'. Use list_requests.")
    blocked = _check_method(ws, req.method)
    if blocked:
        return {"error": blocked}
    rr, res = engine.execute(req, c, ws.environment(environment), ws.settings, extra=variables,
                             all_pages=all_pages or None)
    return _result_dict(ws, rr, res)


@mcp.tool()
def send_http_request(method: str, url: str, environment: str | None = None,
                      auth_from_collection: str | None = None, headers: dict[str, str] | None = None,
                      params: dict[str, str] | None = None, json_body: str | None = None) -> dict:
    """Send a one-off request that is not saved in a collection (disabled unless an admin allows it).

    {{variables}} in url/headers/params/body are filled from the environment.
    auth_from_collection: reuse a collection's auth (e.g. "Schoology API" for OAuth 1.0 signing).
    """
    ws = _ws()
    if not ws.settings.mcp_allow_adhoc_requests:
        return {"error": "Blocked: ad-hoc requests from the AI are disabled. Use send_saved_request, or ask an "
                         "admin to enable 'Allow AI to call unsaved URLs' in Settings."}
    blocked = _check_method(ws, method)
    if blocked:
        return {"error": blocked}
    c = ws.collection(auth_from_collection) if auth_from_collection else None
    req = Request(name="AI request", method=method.upper(), url=url,
                  headers=[KV(k, v) for k, v in (headers or {}).items()],
                  params=[KV(k, v) for k, v in (params or {}).items()],
                  body_mode="json" if json_body else "none", body=json_body or "")
    rr, res = engine.execute(req, c, ws.environment(environment), ws.settings)
    return _result_dict(ws, rr, res)


@mcp.tool()
def run_bulk(collection: str, requests: list[str], csv_path: str, environment: str | None = None,
             expected_status: str = "", extract: str = "", delay_ms: int = 250) -> dict:
    """Run saved request(s) once per row of a CSV file; each CSV column fills the matching {{variable}}.

    requests: request names, run in order for every row.
    expected_status: e.g. "200" or "2xx" (blank = any success).
    extract: values to pull from responses, e.g. "id=id, name=name_display".
    Results are saved to a CSV in the results folder; a summary is returned.
    """
    ws = _ws()
    c = ws.collection(collection)
    reqs = []
    for name in requests:
        r = c.find_request(name)
        if r is None:
            raise LookupError(f"No request '{name}' in '{c.name}'.")
        blocked = _check_method(ws, r.method)
        if blocked:
            return {"error": blocked}
        reqs.append(r)
    _, rows = runner.read_csv_file(csv_path)
    opts = runner.RunOptions(delay_ms=delay_ms, expected_status=expected_status, extract=extract)
    results = runner.run(c, reqs, ws.environment(environment), rows, opts, ws.settings)
    path = runner.save_results(results, ws.settings, f"ai_{c.name}")
    failures = [r for r in results if not r.passed]
    return {"rows": len(rows), "requests_sent": len(results), "passed": len(results) - len(failures),
            "failed": len(failures), "results_file": str(path),
            "first_failures": [{"row": f.row, "request": f.request, "status": f.status, "error": f.error[:300]}
                               for f in failures[:10]]}


@mcp.tool()
def search_knowledge_base(query: str, top_k: int = 5) -> list[dict]:
    """Search the team's uploaded documents (runbooks, KB articles, API docs) for passages about `query`.

    Returns the best-matching excerpts with their source file. Needs the optional local AI (Ollama).
    """
    from .knowledge import open_kb  # imported lazily: only needed when the knowledge base is used

    ws = _ws()
    return open_kb(ws.settings).search(query, k=max(1, min(int(top_k), 12)))


@mcp.tool()
def create_request(collection: str, name: str, method: str, url: str, folder: str | None = None,
                   params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
                   json_body: str | None = None, description: str = "") -> dict:
    """Save a new request into a collection (it is NOT sent). Use {{variables}} for ids and base URLs."""
    ws = _ws()
    c = ws.collection(collection)
    req = Request(name=name, method=method.upper(), url=url, description=description,
                  params=[KV(k, v) for k, v in (params or {}).items()],
                  headers=[KV(k, v) for k, v in (headers or {}).items()],
                  body_mode="json" if json_body else "none", body=json_body or "")
    target = c.items
    if folder:
        match = next((f for trail, f in c.folders() if "/".join(trail).lower() == folder.lower()), None)
        if match is None:
            match = Folder(name=folder)
            c.items.append(match)
        target = match.items
    target.append(req)
    ws.save_collection(c)
    return {"saved": c.request_label(req), "collection": c.name}


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
