"""Data model: collections, folders, requests, environments.

Everything serialises to plain JSON so collection files are easy to read, diff and share.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterator, Union

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
BODY_MODES = {
    "none": "None",
    "json": "JSON",
    "text": "Text / XML",
    "urlencoded": "Form URL-encoded",
    "form": "Multipart form",
}
AUTH_TYPES = {
    "inherit": "Inherit from parent",
    "none": "No auth",
    "oauth1": "OAuth 1.0 (Schoology, OneRoster 1.1)",
    "oauth2": "OAuth 2.0 client credentials (OneRoster 1.2, PowerSchool)",
    "bearer": "Bearer token",
    "basic": "Basic auth",
    "apikey": "API key",
}
OAUTH1_SIGNATURES = ["HMAC-SHA1", "HMAC-SHA256", "PLAINTEXT"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class KV:
    key: str = ""
    value: str = ""
    enabled: bool = True
    secret: bool = False  # only used by environments: masks the value in the UI

    @classmethod
    def from_dict(cls, d: dict) -> "KV":
        return cls(
            key=str(d.get("key", "")),
            value="" if d.get("value") is None else str(d.get("value")),
            enabled=bool(d.get("enabled", True)),
            secret=bool(d.get("secret", False)),
        )


def kv_list(items) -> list[KV]:
    return [KV.from_dict(i) for i in (items or []) if isinstance(i, dict)]


@dataclass
class Auth:
    type: str = "inherit"
    # bearer
    token: str = ""
    # basic
    username: str = ""
    password: str = ""
    # api key
    key_name: str = ""
    key_value: str = ""
    key_in: str = "header"  # header | query
    # oauth 1.0
    consumer_key: str = ""
    consumer_secret: str = ""
    access_token: str = ""
    token_secret: str = ""
    signature_method: str = "HMAC-SHA1"
    realm: str = ""
    # oauth 2.0 client credentials
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    client_auth: str = "basic"  # basic = HTTP Basic header, body = id/secret in the form body

    @classmethod
    def from_dict(cls, d: dict | None, default_type: str = "inherit") -> "Auth":
        d = d or {}
        known = {f.name for f in fields(cls)}
        auth = cls(**{k: str(v) for k, v in d.items() if k in known and v is not None})
        if not d.get("type"):
            auth.type = default_type
        return auth

    def to_dict(self) -> dict:
        """Only store the fields relevant to the chosen type (keeps files tidy)."""
        relevant = {
            "bearer": ["token"],
            "basic": ["username", "password"],
            "apikey": ["key_name", "key_value", "key_in"],
            "oauth1": ["consumer_key", "consumer_secret", "access_token", "token_secret", "signature_method", "realm"],
            "oauth2": ["token_url", "client_id", "client_secret", "scope", "client_auth"],
        }.get(self.type, [])
        return {"type": self.type, **{k: getattr(self, k) for k in relevant}}

    def secret_values(self) -> list[str]:
        return [v for v in (self.token, self.password, self.key_value, self.consumer_secret,
                            self.token_secret, self.client_secret) if v]


@dataclass
class Request:
    name: str = "New request"
    method: str = "GET"
    url: str = ""
    params: list[KV] = field(default_factory=list)
    headers: list[KV] = field(default_factory=list)
    body_mode: str = "none"
    body: str = ""
    form: list[KV] = field(default_factory=list)
    auth: Auth = field(default_factory=Auth)
    description: str = ""
    fetch_all_pages: bool = False
    id: str = field(default_factory=new_id)

    kind = "request"

    @classmethod
    def from_dict(cls, d: dict) -> "Request":
        return cls(
            id=d.get("id") or new_id(),
            name=d.get("name", "New request"),
            method=str(d.get("method", "GET")).upper(),
            url=d.get("url", ""),
            params=kv_list(d.get("params")),
            headers=kv_list(d.get("headers")),
            body_mode=d.get("body_mode", "none") if d.get("body_mode") in BODY_MODES else "none",
            body=d.get("body", ""),
            form=kv_list(d.get("form")),
            auth=Auth.from_dict(d.get("auth"), "inherit"),
            description=d.get("description", ""),
            fetch_all_pages=bool(d.get("fetch_all_pages", False)),
        )

    def to_dict(self) -> dict:
        d = {
            "type": "request",
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "url": self.url,
            "params": [asdict(p) for p in self.params],
            "headers": [asdict(h) for h in self.headers],
            "body_mode": self.body_mode,
            "body": self.body,
            "form": [asdict(f) for f in self.form],
            "auth": self.auth.to_dict(),
            "description": self.description,
        }
        if self.fetch_all_pages:
            d["fetch_all_pages"] = True
        return d

    def clone(self, new_identity: bool = False) -> "Request":
        twin = copy.deepcopy(self)
        if new_identity:
            twin.id = new_id()
        return twin


@dataclass
class Folder:
    name: str = "New folder"
    items: list["Item"] = field(default_factory=list)
    description: str = ""
    id: str = field(default_factory=new_id)

    kind = "folder"

    @classmethod
    def from_dict(cls, d: dict) -> "Folder":
        return cls(id=d.get("id") or new_id(), name=d.get("name", "Folder"),
                   description=d.get("description", ""), items=items_from_list(d.get("items")))

    def to_dict(self) -> dict:
        return {"type": "folder", "id": self.id, "name": self.name, "description": self.description,
                "items": [i.to_dict() for i in self.items]}


Item = Union[Request, Folder]


def items_from_list(raw) -> list[Item]:
    out: list[Item] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        out.append(Folder.from_dict(d) if d.get("type") == "folder" or "items" in d else Request.from_dict(d))
    return out


@dataclass
class Collection:
    name: str = "New collection"
    description: str = ""
    variables: list[KV] = field(default_factory=list)
    auth: Auth = field(default_factory=lambda: Auth(type="none"))
    items: list[Item] = field(default_factory=list)
    id: str = field(default_factory=new_id)
    path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "Collection":
        auth = Auth.from_dict(d.get("auth"), "none")
        if auth.type == "inherit":
            auth.type = "none"
        return cls(id=d.get("id") or new_id(), name=d.get("name", "Collection"),
                   description=d.get("description", ""), variables=kv_list(d.get("variables")),
                   auth=auth, items=items_from_list(d.get("items")), path=path)

    def to_dict(self) -> dict:
        return {"format": "restclient-collection/1", "id": self.id, "name": self.name,
                "description": self.description, "variables": [asdict(v) for v in self.variables],
                "auth": self.auth.to_dict(), "items": [i.to_dict() for i in self.items]}

    # ---- navigation helpers -------------------------------------------------
    def walk(self) -> Iterator[tuple[list[str], Request]]:
        """Yield (folder names, request) for every request, depth first."""

        def _walk(items: list[Item], trail: list[str]):
            for item in items:
                if isinstance(item, Folder):
                    yield from _walk(item.items, trail + [item.name])
                else:
                    yield trail, item

        yield from _walk(self.items, [])

    def folders(self) -> Iterator[tuple[list[str], Folder]]:
        def _walk(items: list[Item], trail: list[str]):
            for item in items:
                if isinstance(item, Folder):
                    yield trail + [item.name], item
                    yield from _walk(item.items, trail + [item.name])

        yield from _walk(self.items, [])

    def find_request(self, ref: str) -> Request | None:
        """Find by id, by 'Folder/Sub/Name' path, or by name (case-insensitive)."""
        ref_l = ref.strip().lower()
        by_name = None
        for trail, req in self.walk():
            if req.id == ref:
                return req
            if "/".join(trail + [req.name]).lower() == ref_l:
                return req
            if by_name is None and req.name.lower() == ref_l:
                by_name = req
        return by_name

    def request_label(self, req: Request) -> str:
        for trail, r in self.walk():
            if r.id == req.id:
                return " / ".join(trail + [r.name])
        return req.name

    def find_container(self, item_id: str) -> list[Item] | None:
        """Return the list that directly contains the item with this id."""

        def _find(items: list[Item]):
            for item in items:
                if item.id == item_id:
                    return items
                if isinstance(item, Folder):
                    found = _find(item.items)
                    if found is not None:
                        return found
            return None

        return _find(self.items)

    def find_folder(self, folder_id: str) -> Folder | None:
        for _, f in self.folders():
            if f.id == folder_id:
                return f
        return None

    def remove_item(self, item_id: str) -> bool:
        container = self.find_container(item_id)
        if container is None:
            return False
        container[:] = [i for i in container if i.id != item_id]
        return True

    def replace_request(self, req: Request) -> bool:
        container = self.find_container(req.id)
        if container is None:
            return False
        for idx, item in enumerate(container):
            if item.id == req.id:
                container[idx] = req
        return True


@dataclass
class Environment:
    name: str = "New environment"
    variables: list[KV] = field(default_factory=list)
    # Optional auth override: lets one environment (district / SIS) supply its own auth
    # method so the same collection works against PowerSchool, Infinite Campus, etc.
    auth: Auth = field(default_factory=Auth)
    description: str = ""
    id: str = field(default_factory=new_id)
    path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "Environment":
        return cls(id=d.get("id") or new_id(), name=d.get("name", "Environment"),
                   variables=kv_list(d.get("variables")), auth=Auth.from_dict(d.get("auth"), "inherit"),
                   description=d.get("description", ""), path=path)

    def to_dict(self) -> dict:
        return {"format": "restclient-environment/1", "id": self.id, "name": self.name,
                "description": self.description, "variables": [asdict(v) for v in self.variables],
                "auth": self.auth.to_dict()}

    def secret_values(self) -> list[str]:
        return [v.value for v in self.variables if v.secret and v.value] + self.auth.secret_values()
