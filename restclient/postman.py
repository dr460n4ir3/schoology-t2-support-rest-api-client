"""Import from / export to Postman (collection v2.1 and environment files)."""

from __future__ import annotations

from .models import KV, Auth, Collection, Environment, Folder, Request, new_id

_POSTMAN_TO_OAUTH1 = {"consumerKey": "consumer_key", "consumerSecret": "consumer_secret", "token": "access_token",
                      "tokenSecret": "token_secret", "signatureMethod": "signature_method", "realm": "realm"}
_POSTMAN_TO_OAUTH2 = {"accessTokenUrl": "token_url", "clientId": "client_id", "clientSecret": "client_secret",
                      "scope": "scope", "client_authentication": "client_auth"}


class ImportReport:
    def __init__(self):
        self.requests = 0
        self.folders = 0
        self.scripts_skipped = 0
        self.notes: list[str] = []

    def __str__(self) -> str:
        msg = f"Imported {self.requests} request(s) in {self.folders} folder(s)."
        if self.scripts_skipped:
            msg += (f" {self.scripts_skipped} Postman pre-request/test script(s) were not imported "
                    f"(use the Bulk Runner's expected status / extract options instead).")
        if self.notes:
            msg += " " + " ".join(sorted(set(self.notes)))
        return msg


def detect(data: dict) -> str:
    if isinstance(data, dict):
        if data.get("format", "").startswith("restclient-collection"):
            return "restclient-collection"
        if data.get("format", "").startswith("restclient-environment"):
            return "restclient-environment"
        if "info" in data and "item" in data:
            return "postman-collection"
        if "values" in data and "name" in data:
            return "postman-environment"
    return "unknown"


# ------------------------------------------------------------------------ import -----
def _kv_from_postman(items) -> list[KV]:
    out = []
    for i in items or []:
        if isinstance(i, dict) and i.get("key") is not None:
            out.append(KV(key=str(i.get("key", "")), value="" if i.get("value") is None else str(i.get("value")),
                          enabled=not i.get("disabled", False)))
    return out


def _auth_from_postman(auth: dict | None, default: str) -> Auth:
    if not auth:
        return Auth(type=default)
    ptype = auth.get("type", "noauth")
    values = {}
    raw = auth.get(ptype)
    if isinstance(raw, list):
        values = {i.get("key"): i.get("value") for i in raw if isinstance(i, dict)}
    elif isinstance(raw, dict):
        values = raw

    def v(key):
        val = values.get(key)
        return "" if val is None else str(val)

    if ptype == "noauth":
        return Auth(type="none")
    if ptype == "inherit":
        return Auth(type=default)
    if ptype == "bearer":
        return Auth(type="bearer", token=v("token"))
    if ptype == "basic":
        return Auth(type="basic", username=v("username"), password=v("password"))
    if ptype == "apikey":
        return Auth(type="apikey", key_name=v("key"), key_value=v("value"), key_in=v("in") or "header")
    if ptype == "oauth1":
        a = Auth(type="oauth1", **{ours: v(theirs) for theirs, ours in _POSTMAN_TO_OAUTH1.items()})
        a.signature_method = a.signature_method or "HMAC-SHA1"
        return a
    if ptype == "oauth2":
        a = Auth(type="oauth2", **{ours: v(theirs) for theirs, ours in _POSTMAN_TO_OAUTH2.items()})
        a.client_auth = "body" if a.client_auth == "body" else "basic"
        if not a.token_url and v("accessToken"):  # Postman stored a token only
            return Auth(type="bearer", token=v("accessToken"))
        return a
    return Auth(type=default)


def _split_url(url) -> tuple[str, list[KV]]:
    if isinstance(url, str):
        raw, query = url, None
    elif isinstance(url, dict):
        raw, query = url.get("raw", ""), url.get("query")
    else:
        return "", []
    base, _, qs = raw.partition("?")
    if query is not None:
        return base, _kv_from_postman(query)
    params = []
    for part in qs.split("&") if qs else []:
        k, _, val = part.partition("=")
        params.append(KV(key=k, value=val))
    return base, params


def _request_from_postman(item: dict, report: ImportReport, inherited: Auth | None) -> Request:
    req = item.get("request") or {}
    if isinstance(req, str):
        req = {"url": req, "method": "GET"}
    url, params = _split_url(req.get("url", ""))
    r = Request(
        name=item.get("name", "Request"),
        method=str(req.get("method", "GET")).upper(),
        url=url,
        params=params,
        headers=_kv_from_postman(req.get("header") if isinstance(req.get("header"), list) else []),
        auth=_auth_from_postman(req.get("auth"), "inherit"),
        description=_text(req.get("description") or item.get("description")),
    )
    if r.auth.type == "inherit" and inherited is not None:
        r.auth = inherited  # folder-level auth from Postman
    body = req.get("body") or {}
    mode = body.get("mode")
    if mode == "raw":
        lang = ((body.get("options") or {}).get("raw") or {}).get("language", "")
        r.body_mode = "json" if lang == "json" or body.get("raw", "").lstrip()[:1] in "{[" and body.get("raw") else "text"
        r.body = body.get("raw", "")
    elif mode == "urlencoded":
        r.body_mode, r.form = "urlencoded", _kv_from_postman(body.get("urlencoded"))
    elif mode == "formdata":
        fields = [f for f in body.get("formdata") or [] if f.get("type", "text") == "text"]
        if len(fields) != len(body.get("formdata") or []):
            report.notes.append("File-upload form fields were skipped.")
        r.body_mode, r.form = "form", _kv_from_postman(fields)
    elif mode == "graphql":
        import json as _json
        r.body_mode, r.body = "json", _json.dumps(body.get("graphql") or {}, indent=2)
    if any(e.get("script", {}).get("exec") for e in item.get("event") or [] if isinstance(e, dict)):
        report.scripts_skipped += 1
    report.requests += 1
    return r


def _text(desc) -> str:
    if isinstance(desc, dict):
        return desc.get("content", "")
    return desc or ""


def _items_from_postman(items, report: ImportReport, inherited: Auth | None):
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "item" in item:
            folder_auth = _auth_from_postman(item.get("auth"), "inherit") if item.get("auth") else None
            if folder_auth is not None and folder_auth.type == "inherit":
                folder_auth = None
            report.folders += 1
            out.append(Folder(name=item.get("name", "Folder"), description=_text(item.get("description")),
                              items=_items_from_postman(item["item"], report, folder_auth or inherited)))
        else:
            out.append(_request_from_postman(item, report, inherited))
    return out


def import_collection(data: dict) -> tuple[Collection, ImportReport]:
    report = ImportReport()
    info = data.get("info") or {}
    if any(e.get("script", {}).get("exec") for e in data.get("event") or [] if isinstance(e, dict)):
        report.scripts_skipped += 1
    auth = _auth_from_postman(data.get("auth"), "none")
    if auth.type == "inherit":
        auth.type = "none"
    c = Collection(
        id=new_id(),
        name=info.get("name", "Imported collection"),
        description=_text(info.get("description")),
        variables=_kv_from_postman(data.get("variable")),
        auth=auth,
        items=_items_from_postman(data.get("item"), report, None),
    )
    return c, report


def import_environment(data: dict) -> Environment:
    variables = []
    for v in data.get("values") or []:
        if isinstance(v, dict) and v.get("key"):
            variables.append(KV(key=v["key"], value="" if v.get("value") is None else str(v.get("value")),
                                enabled=v.get("enabled", True), secret=v.get("type") == "secret"))
    return Environment(id=new_id(), name=data.get("name", "Imported environment"), variables=variables)


# ------------------------------------------------------------------------ export -----
def _kv_to_postman(items: list[KV]) -> list[dict]:
    return [{"key": i.key, "value": i.value, **({"disabled": True} if not i.enabled else {})} for i in items]


def _auth_to_postman(a: Auth) -> dict | None:
    if a.type == "inherit":
        return None
    if a.type == "none":
        return {"type": "noauth"}

    def pairs(mapping: dict) -> list[dict]:
        return [{"key": k, "value": v, "type": "string"} for k, v in mapping.items()]

    if a.type == "bearer":
        return {"type": "bearer", "bearer": pairs({"token": a.token})}
    if a.type == "basic":
        return {"type": "basic", "basic": pairs({"username": a.username, "password": a.password})}
    if a.type == "apikey":
        return {"type": "apikey", "apikey": pairs({"key": a.key_name, "value": a.key_value, "in": a.key_in})}
    if a.type == "oauth1":
        return {"type": "oauth1", "oauth1": pairs({theirs: getattr(a, ours) for theirs, ours in _POSTMAN_TO_OAUTH1.items()}
                                                  | {"addParamsToHeader": True, "version": "1.0"})}
    if a.type == "oauth2":
        return {"type": "oauth2", "oauth2": pairs({theirs: getattr(a, ours) for theirs, ours in _POSTMAN_TO_OAUTH2.items()}
                                                  | {"grant_type": "client_credentials"})}
    return None


def _request_to_postman(r: Request) -> dict:
    query = "&".join(f"{p.key}={p.value}" for p in r.params if p.enabled)
    req: dict = {
        "method": r.method,
        "header": _kv_to_postman(r.headers),
        "url": {"raw": r.url + (f"?{query}" if query else ""), "query": _kv_to_postman(r.params)} if r.params else r.url,
        "description": r.description,
    }
    auth = _auth_to_postman(r.auth)
    if auth:
        req["auth"] = auth
    if r.body_mode in ("json", "text"):
        req["body"] = {"mode": "raw", "raw": r.body,
                       "options": {"raw": {"language": "json" if r.body_mode == "json" else "text"}}}
    elif r.body_mode == "urlencoded":
        req["body"] = {"mode": "urlencoded", "urlencoded": _kv_to_postman(r.form)}
    elif r.body_mode == "form":
        req["body"] = {"mode": "formdata", "formdata": [{**d, "type": "text"} for d in _kv_to_postman(r.form)]}
    return {"name": r.name, "request": req}


def _items_to_postman(items) -> list[dict]:
    out = []
    for item in items:
        if isinstance(item, Folder):
            out.append({"name": item.name, "description": item.description, "item": _items_to_postman(item.items)})
        else:
            out.append(_request_to_postman(item))
    return out


def export_collection(c: Collection) -> dict:
    data = {
        "info": {"_postman_id": c.id, "name": c.name, "description": c.description,
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": _items_to_postman(c.items),
        "variable": _kv_to_postman(c.variables),
    }
    auth = _auth_to_postman(c.auth)
    if auth:
        data["auth"] = auth
    return data


def export_environment(e: Environment) -> dict:
    return {"name": e.name, "values": [{"key": v.key, "value": v.value, "enabled": v.enabled,
                                        "type": "secret" if v.secret else "default"} for v in e.variables],
            "_postman_variable_scope": "environment"}
