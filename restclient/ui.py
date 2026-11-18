"""The web UI (NiceGUI). Runs locally and opens in the browser at http://127.0.0.1:<port>.

Layout:  header (environment picker, settings)  |  left: collections tree
         main tabs: Request · Bulk Runner · Environments · AI Assistant
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from nicegui import app, run, ui

from . import APP_NAME, __version__, engine, postman, runner
from .config import app_home, save_settings
from .models import (AUTH_TYPES, BODY_MODES, METHODS, OAUTH1_SIGNATURES, KV, Auth, Collection, Environment,
                     Folder, Request)
from .variables import missing_variables, request_variables
from .workspace import Workspace

log = logging.getLogger(__name__)

WS: Workspace = None  # type: ignore[assignment]  # created in start()

METHOD_COLORS = {"GET": "text-green-8", "POST": "text-orange-9", "PUT": "text-blue-8", "PATCH": "text-purple-7",
                 "DELETE": "text-red-8", "HEAD": "text-grey-7", "OPTIONS": "text-grey-7"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ===================================================================== small helpers ===
def ui_state_file() -> Path:
    return app_home() / "ui_state.json"


def load_ui_state() -> dict:
    try:
        return json.loads(ui_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ui_state(**changes) -> None:
    state = load_ui_state()
    state.update(changes)
    try:
        ui_state_file().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def method_label(method: str, cls: str = "") -> None:
    ui.label(method).classes(f"{METHOD_COLORS.get(method, '')} text-xs font-bold font-mono {cls}")


class KVEditor:
    """Editable table of key/value rows (params, headers, form fields, variables)."""

    def __init__(self, items: list[KV], on_change=None, secrets: bool = False,
                 key_label: str = "Key", value_label: str = "Value", add_label: str = "Add row"):
        self.items = items
        self.on_change = on_change or (lambda: None)
        self.secrets = secrets
        self.key_label, self.value_label, self.add_label = key_label, value_label, add_label
        self.container = ui.column().classes("w-full gap-1")
        self.render()

    def set_items(self, items: list[KV]) -> None:
        self.items = items
        self.render()

    def _changed(self, kv: KV, attr: str, value) -> None:
        setattr(kv, attr, value)
        self.on_change()

    def render(self) -> None:
        self.container.clear()
        with self.container:
            if self.items:
                with ui.row().classes("w-full no-wrap text-xs text-grey-7 gap-2 pl-10"):
                    ui.label(self.key_label).classes("w-1/3")
                    ui.label(self.value_label)
            for kv in self.items:
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.checkbox(value=kv.enabled, on_change=lambda e, kv=kv: self._changed(kv, "enabled", e.value)) \
                        .props("dense").tooltip("Include this row")
                    ui.input(value=kv.key, placeholder=self.key_label,
                             on_change=lambda e, kv=kv: self._changed(kv, "key", e.value)) \
                        .props("dense outlined").classes("w-1/3 font-mono")
                    ui.input(value=kv.value, placeholder=self.value_label, password=kv.secret,
                             password_toggle_button=kv.secret,
                             on_change=lambda e, kv=kv: self._changed(kv, "value", e.value)) \
                        .props("dense outlined").classes("flex-grow font-mono")
                    if self.secrets:
                        ui.button(icon="lock" if kv.secret else "lock_open",
                                  on_click=lambda kv=kv: self._toggle_secret(kv)) \
                            .props(f"flat dense round {'color=orange' if kv.secret else 'color=grey'}") \
                            .tooltip("Secret: hidden on screen and never shown to the AI")
                    ui.button(icon="close", on_click=lambda kv=kv: self._remove(kv)) \
                        .props("flat dense round color=grey").tooltip("Remove")
            ui.button(self.add_label, icon="add", on_click=self._add).props("flat dense no-caps color=primary")

    def _toggle_secret(self, kv: KV) -> None:
        kv.secret = not kv.secret
        self.on_change()
        self.render()

    def _add(self) -> None:
        self.items.append(KV())
        self.on_change()
        self.render()

    def _remove(self, kv: KV) -> None:
        self.items.remove(kv)
        self.on_change()
        self.render()


class AuthEditor:
    """Pick an auth type and fill in its fields. {{variables}} are allowed everywhere."""

    def __init__(self, auth: Auth, on_change=None, allowed: list[str] | None = None,
                 inherit_label: str = "Inherit from parent", inherit_hint=None):
        self.auth = auth
        self.on_change = on_change or (lambda: None)
        self.allowed = allowed or list(AUTH_TYPES)
        self.inherit_label = inherit_label
        self.inherit_hint = inherit_hint
        self.container = ui.column().classes("w-full gap-2")
        self.render()

    def set_auth(self, auth: Auth) -> None:
        self.auth = auth
        self.render()

    def _field(self, attr: str, label: str, secret: bool = False, hint: str = "") -> None:
        inp = ui.input(label, value=getattr(self.auth, attr), password=secret, password_toggle_button=secret,
                       on_change=lambda e: self._set(attr, e.value)).props("dense outlined").classes("w-full font-mono")
        if hint:
            inp.props(f'hint="{hint}"')

    def _set(self, attr: str, value) -> None:
        setattr(self.auth, attr, value)
        self.on_change()

    def _set_type(self, value: str) -> None:
        self.auth.type = value
        self.on_change()
        self.render()

    def render(self) -> None:
        self.container.clear()
        options = {k: (self.inherit_label if k == "inherit" else v) for k, v in AUTH_TYPES.items() if k in self.allowed}
        with self.container:
            ui.select(options, value=self.auth.type if self.auth.type in options else self.allowed[0],
                      label="Auth type", on_change=lambda e: self._set_type(e.value)) \
                .props("dense outlined").classes("w-full max-w-xl")
            a = self.auth
            with ui.column().classes("w-full max-w-xl gap-2"):
                if a.type == "inherit":
                    ui.label(self.inherit_hint() if self.inherit_hint else "Uses the parent's auth.") \
                        .classes("text-grey-7 text-sm")
                elif a.type == "none":
                    ui.label("No Authorization header is added.").classes("text-grey-7 text-sm")
                elif a.type == "bearer":
                    self._field("token", "Token", secret=True)
                elif a.type == "basic":
                    self._field("username", "Username")
                    self._field("password", "Password", secret=True)
                elif a.type == "apikey":
                    self._field("key_name", "Key name", hint="e.g. X-API-Key")
                    self._field("key_value", "Key value", secret=True)
                    ui.select({"header": "Header", "query": "Query parameter"}, value=a.key_in, label="Send in",
                              on_change=lambda e: self._set("key_in", e.value)).props("dense outlined")
                elif a.type == "oauth1":
                    ui.label("Two-legged OAuth 1.0. Schoology: HMAC-SHA1 (or PLAINTEXT). "
                             "OneRoster 1.1 (e.g. Infinite Campus): HMAC-SHA256.").classes("text-grey-7 text-sm")
                    self._field("consumer_key", "Consumer key", hint="e.g. {{schoology_consumer_key}}")
                    self._field("consumer_secret", "Consumer secret", secret=True)
                    ui.select(OAUTH1_SIGNATURES, value=a.signature_method or "HMAC-SHA1", label="Signature method",
                              on_change=lambda e: self._set("signature_method", e.value)).props("dense outlined")
                    with ui.expansion("Advanced (rarely needed)").classes("w-full text-sm"):
                        self._field("access_token", "Access token (three-legged only)")
                        self._field("token_secret", "Token secret", secret=True)
                        self._field("realm", "Realm")
                elif a.type == "oauth2":
                    ui.label("Client-credentials grant. The token is fetched automatically, cached, and "
                             "refreshed when it expires.").classes("text-grey-7 text-sm")
                    self._field("token_url", "Token URL", hint="e.g. {{token_url}}")
                    self._field("client_id", "Client ID")
                    self._field("client_secret", "Client secret", secret=True)
                    self._field("scope", "Scope (optional)")
                    ui.select({"basic": "Send as Basic auth header (most servers)",
                               "body": "Send client id/secret in the request body"},
                              value=a.client_auth or "basic", label="Client authentication",
                              on_change=lambda e: self._set("client_auth", e.value)).props("dense outlined")
                    ui.button("Forget cached tokens", icon="refresh",
                              on_click=lambda: (engine.clear_token_cache(), ui.notify("Token cache cleared"))) \
                        .props("flat dense no-caps")


# ======================================================================= the page ======
class ClientPage:
    def __init__(self):
        state = load_ui_state()
        self.env_id: str | None = state.get("environment") if WS.environment_by_id(state.get("environment")) else None
        self.expanded: set[str] = set(state.get("expanded", []))
        self.filter = ""
        self.current = Request(name="New request")
        self.current_cid: str | None = None
        self.dirty = False
        self._loading = False
        self.last_result: engine.Result | None = None
        self.last_resolved: engine.ResolvedRequest | None = None
        # runner state
        self.run_rows: list[dict] = []
        self.run_columns: list[str] = []
        self.run_file = ""
        self.run_results: list[runner.StepResult] = []
        self.run_cancel: threading.Event | None = None
        self.running = False
        self._shown_results = -1
        self.env_edit: Environment | None = None
        self._ready = False  # ignore change events fired while the page is still being built

        self.build()
        self._ready = True

    # ------------------------------------------------------------------ helpers
    def env(self) -> Environment | None:
        return WS.environment_by_id(self.env_id)

    def collection(self) -> Collection | None:
        return WS.collection_by_id(self.current_cid)

    def mark_dirty(self) -> None:
        if self._loading:
            return
        self.dirty = True
        self.dirty_badge.set_visibility(True)
        self.update_var_warning()

    async def prompt(self, title: str, label: str, value: str = "") -> str | None:
        with self.dialog_host, ui.dialog() as d, ui.card().classes("min-w-[400px]"):
            ui.label(title).classes("text-lg font-medium")
            inp = ui.input(label, value=value).props("autofocus outlined dense").classes("w-full")
            inp.on("keydown.enter", lambda: d.submit(inp.value))
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: d.submit(None)).props("flat no-caps")
                ui.button("OK", on_click=lambda: d.submit(inp.value)).props("no-caps")
        result = await d
        d.delete()
        return result.strip() if isinstance(result, str) and result.strip() else None

    async def confirm(self, title: str, message: str, ok: str = "OK", color: str = "primary") -> bool:
        with self.dialog_host, ui.dialog() as d, ui.card().classes("max-w-xl"):
            ui.label(title).classes("text-lg font-medium")
            ui.markdown(message)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: d.submit(False)).props("flat no-caps")
                ui.button(ok, on_click=lambda: d.submit(True)).props(f"no-caps color={color}")
        result = await d
        d.delete()
        return bool(result)

    def show_text(self, title: str, text: str, language: str | None = None) -> None:
        with self.dialog_host, ui.dialog() as d, ui.card().classes("w-[900px] max-w-full"):
            with ui.row().classes("w-full items-center"):
                ui.label(title).classes("text-lg font-medium")
                ui.space()
                ui.button(icon="content_copy", on_click=lambda: (ui.clipboard.write(text), ui.notify("Copied"))) \
                    .props("flat round").tooltip("Copy")
                ui.button(icon="close", on_click=d.close).props("flat round")
            cm = ui.codemirror(text, language=language, line_wrapping=True).classes("w-full").style("height: 60vh")
            cm.disable()
        d.on("hide", d.delete)
        d.open()

    # =================================================================== layout
    def build(self) -> None:
        ui.colors(primary="#1f6feb")
        ui.add_css("""
            :root { color-scheme: light; }
            body { background: #f7f8fa !important; color: #1f2328; }
            .nicegui-content { padding: 12px 16px; }
            .cm-editor { height: 100%; font-size: 13px; }
            .req-item { min-height: 28px !important; padding: 0 8px !important; border-radius: 6px; }
            .req-item.active { background: #e8f0fe; }
            .q-expansion-item__container > .q-item { min-height: 32px; padding: 0 4px; }
            .q-drawer .q-scrollarea__container { overflow-x: hidden !important; }
            .q-drawer .q-scrollarea__content { width: 100%; }
        """)
        self.dialog_host = ui.element("div")

        with ui.header(elevated=False).classes("items-center bg-white text-black border-b px-3 py-1 gap-2"):
            ui.button(icon="menu", on_click=lambda: self.drawer.toggle()).props("flat round dense")
            ui.icon("api", size="sm", color="primary")
            ui.label(APP_NAME).classes("text-lg font-semibold")
            ui.space()
            ui.label("Environment").classes("text-sm text-grey-7")
            self.env_select = ui.select({}, value=None, with_input=True, clearable=True,
                                        on_change=self.on_env_change) \
                .props('dense outlined options-dense placeholder="No environment"').classes("w-80")
            self.refresh_env_options()
            ui.button(icon="settings", on_click=self.open_settings).props("flat round dense").tooltip("Settings")
            ui.button(icon="help_outline", on_click=lambda: self.tabs.set_value("AI & Help")) \
                .props("flat round dense").tooltip("Help")
            ui.button(icon="power_settings_new", on_click=self.quit_app).props("flat round dense color=red") \
                .tooltip("Quit the app (closes the background server)")

        with ui.left_drawer(value=True, bordered=True).props("width=330").classes("bg-grey-1 p-2") as self.drawer:
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Collections").classes("text-base font-semibold")
                ui.space()
                ui.button(icon="note_add", on_click=self.new_collection).props("flat round dense") \
                    .tooltip("New collection")
                ui.button(icon="upload_file", on_click=self.open_import).props("flat round dense") \
                    .tooltip("Import Postman / exported file")
                ui.button(icon="refresh", on_click=self.reload_all).props("flat round dense") \
                    .tooltip("Reload from disk (picks up changes made by teammates)")
            ui.input(placeholder="Search requests…", on_change=self.on_filter) \
                .props("dense outlined clearable").classes("w-full mb-1")
            with ui.scroll_area().classes("w-full flex-grow").style("height: calc(100vh - 150px)"):
                self.render_tree()

        with ui.tabs().classes("w-full").props("align=left no-caps") as self.tabs:
            ui.tab("Request", icon="send")
            ui.tab("Bulk Runner", icon="playlist_play")
            ui.tab("Environments", icon="tune")
            ui.tab("AI & Help", icon="smart_toy")
        with ui.tab_panels(self.tabs, value="Request").classes("w-full bg-transparent") \
                .props("keep-alive animated=false"):
            with ui.tab_panel("Request").classes("p-0 pt-2"):
                self.build_request_panel()
            with ui.tab_panel("Bulk Runner").classes("p-0 pt-2"):
                self.build_runner_panel()
            with ui.tab_panel("Environments").classes("p-0 pt-2"):
                self.build_env_panel()
            with ui.tab_panel("AI & Help").classes("p-0 pt-2"):
                self.build_ai_panel()

        ui.timer(0.5, self.poll_runner)
        if WS.store.load_errors:
            ui.notify("Some files could not be loaded: " + "; ".join(WS.store.load_errors), type="warning",
                      multi_line=True, timeout=10000)
        self.load_request(self.current, None)

    # ============================================================== environments
    def refresh_env_options(self) -> None:
        self.env_select.options = {e.id: e.name for e in WS.environments}
        if self.env_id not in self.env_select.options:
            self.env_id = None
        self.env_select.value = self.env_id
        self.env_select.update()

    def on_env_change(self, e) -> None:
        if not self._ready:
            return
        self.env_id = e.value
        save_ui_state(environment=self.env_id)
        if self.current.auth.type == "inherit":
            self.auth_editor.render()  # refresh the "uses ... auth" hint
        self.update_var_warning()
        self.update_runner_check()

    # ======================================================================= tree
    def on_filter(self, e) -> None:
        self.filter = (e.value or "").strip().lower()
        self.render_tree.refresh()

    def _toggle_expanded(self, item_id: str, value: bool) -> None:
        (self.expanded.add if value else self.expanded.discard)(item_id)
        save_ui_state(expanded=sorted(self.expanded))

    def _matches(self, req: Request) -> bool:
        return not self.filter or self.filter in req.name.lower() or self.filter in req.url.lower()

    def _has_match(self, items) -> bool:
        return any(self._has_match(i.items) if isinstance(i, Folder) else self._matches(i) for i in items)

    @ui.refreshable_method
    def render_tree(self) -> None:
        if not WS.collections:
            ui.label("No collections yet. Create one or import a Postman collection.").classes("text-grey-7 text-sm p-2")
        for c in WS.collections:
            if self.filter and not self._has_match(c.items) and self.filter not in c.name.lower():
                continue
            exp = ui.expansion(value=c.id in self.expanded or bool(self.filter),
                               on_value_change=lambda e, cid=c.id: self._toggle_expanded(cid, e.value)) \
                .classes("w-full").props("dense")
            with exp.add_slot("header"):
                with ui.row().classes("w-full items-center no-wrap gap-1"):
                    ui.icon("inventory_2", color="primary", size="xs")
                    ui.label(c.name).classes("font-medium ellipsis")
                    ui.space()
                    with ui.button(icon="more_horiz").props("flat round dense size=sm").on("click.stop", lambda: None):
                        with ui.menu():
                            ui.menu_item("Add request", lambda c=c: self.add_request(c, c.items))
                            ui.menu_item("Add folder", lambda c=c: self.add_folder(c, c.items))
                            ui.menu_item("Variables, auth & description", lambda c=c: self.edit_collection(c))
                            ui.separator()
                            ui.menu_item("Rename", lambda c=c: self.rename_collection(c))
                            ui.menu_item("Duplicate", lambda c=c: self.duplicate_collection(c))
                            ui.menu_item("Export for Postman", lambda c=c: self.export_collection(c, True))
                            ui.menu_item("Export (this app's format)", lambda c=c: self.export_collection(c, False))
                            ui.separator()
                            ui.menu_item("Delete", lambda c=c: self.delete_collection(c)).classes("text-red-8")
            with exp:
                self.render_items(c, c.items)

    def render_items(self, c: Collection, items: list) -> None:
        for item in items:
            if isinstance(item, Folder):
                if self.filter and not self._has_match(item.items):
                    continue
                exp = ui.expansion(value=item.id in self.expanded or bool(self.filter),
                                   on_value_change=lambda e, fid=item.id: self._toggle_expanded(fid, e.value)) \
                    .classes("w-full pl-3").props("dense")
                with exp.add_slot("header"):
                    with ui.row().classes("w-full items-center no-wrap gap-1"):
                        ui.icon("folder", color="amber-8", size="xs")
                        ui.label(item.name).classes("ellipsis")
                        ui.space()
                        with ui.button(icon="more_horiz").props("flat round dense size=sm").on("click.stop", lambda: None):
                            with ui.menu():
                                ui.menu_item("Add request", lambda f=item: self.add_request(c, f.items))
                                ui.menu_item("Add folder", lambda f=item: self.add_folder(c, f.items))
                                ui.menu_item("Rename", lambda f=item: self.rename_item(c, f))
                                ui.menu_item("Delete", lambda f=item: self.delete_item(c, f)).classes("text-red-8")
                with exp:
                    self.render_items(c, item.items)
            else:
                if not self._matches(item):
                    continue
                active = item.id == self.current.id and c.id == self.current_cid
                with ui.item(on_click=lambda r=item: self.open_request(c, r)) \
                        .classes(f"req-item pl-6 {'active' if active else ''}").props("dense clickable"):
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        method_label(item.method, "w-12 text-right")
                        ui.label(item.name).classes("ellipsis text-sm")
                    with ui.context_menu():
                        ui.menu_item("Open", lambda r=item: self.open_request(c, r))
                        ui.menu_item("Duplicate", lambda r=item: self.duplicate_request(c, r))
                        ui.menu_item("Rename", lambda r=item: self.rename_item(c, r))
                        ui.menu_item("Delete", lambda r=item: self.delete_item(c, r)).classes("text-red-8")

    # ---------------------------------------------------------- tree actions
    async def new_collection(self) -> None:
        name = await self.prompt("New collection", "Name")
        if name:
            c = Collection(name=name)
            WS.save_collection(c)
            self.expanded.add(c.id)
            self.render_tree.refresh()
            self.refresh_collection_options()

    async def add_request(self, c: Collection, target: list) -> None:
        name = await self.prompt("New request", "Request name", "New request")
        if name:
            req = Request(name=name)
            target.append(req)
            WS.save_collection(c)
            self.expanded.add(c.id)
            await self.open_request(c, req)

    async def add_folder(self, c: Collection, target: list) -> None:
        name = await self.prompt("New folder", "Folder name")
        if name:
            f = Folder(name=name)
            target.append(f)
            WS.save_collection(c)
            self.expanded.update({c.id, f.id})
            self.render_tree.refresh()

    async def rename_collection(self, c: Collection) -> None:
        name = await self.prompt("Rename collection", "Name", c.name)
        if name:
            c.name = name
            WS.save_collection(c)
            self.render_tree.refresh()
            self.refresh_collection_options()
            self.update_breadcrumb()

    async def duplicate_collection(self, c: Collection) -> None:
        twin = Collection.from_dict(copy.deepcopy(c.to_dict()))
        twin.id, twin.name = Collection().id, c.name + " (copy)"
        for _, r in twin.walk():
            r.id = Request().id
        WS.save_collection(twin)
        self.render_tree.refresh()
        self.refresh_collection_options()

    def export_collection(self, c: Collection, for_postman: bool) -> None:
        if for_postman:
            data, fname = postman.export_collection(c), f"{c.name}.postman_collection.json"
        else:
            data, fname = c.to_dict(), f"{c.name}.json"
        ui.download.content(json.dumps(data, indent=2).encode("utf-8"), fname)

    async def delete_collection(self, c: Collection) -> None:
        if await self.confirm("Delete collection?", f"Delete **{c.name}** and all its requests? "
                              f"(A backup is kept in the collections folder's `.trash`.)", "Delete", "negative"):
            WS.delete_collection(c)
            if self.current_cid == c.id:
                self.current_cid = None
                self.update_breadcrumb()
            self.render_tree.refresh()
            self.refresh_collection_options()

    async def rename_item(self, c: Collection, item) -> None:
        name = await self.prompt("Rename", "Name", item.name)
        if name:
            item.name = name
            WS.save_collection(c)
            if item.id == self.current.id:
                self._loading = True
                self.current.name = name
                self.name_input.value = name
                self._loading = False
            self.render_tree.refresh()
            self.refresh_request_options()

    async def duplicate_request(self, c: Collection, r: Request) -> None:
        twin = r.clone(new_identity=True)
        twin.name = r.name + " (copy)"
        container = c.find_container(r.id)
        container.insert(container.index(r) + 1, twin)
        WS.save_collection(c)
        await self.open_request(c, twin)

    async def delete_item(self, c: Collection, item) -> None:
        what = "folder and everything in it" if isinstance(item, Folder) else "request"
        if await self.confirm("Delete?", f"Delete the {what} **{item.name}**?", "Delete", "negative"):
            c.remove_item(item.id)
            WS.save_collection(c)
            if item.id == self.current.id:
                self.dirty = True
                self.dirty_badge.set_visibility(True)
            self.render_tree.refresh()
            self.refresh_request_options()

    async def edit_collection(self, c: Collection) -> None:
        work = Collection.from_dict(copy.deepcopy(c.to_dict()))
        with self.dialog_host, ui.dialog() as d, ui.card().classes("w-[860px] max-w-full"):
            ui.label(f"Collection: {c.name}").classes("text-lg font-medium")
            with ui.tabs().props("align=left no-caps dense") as t:
                ui.tab("Variables")
                ui.tab("Auth")
                ui.tab("Description")
            with ui.tab_panels(t, value="Variables").classes("w-full"):
                with ui.tab_panel("Variables"):
                    ui.label("Defaults shared by every request in this collection. An environment variable with "
                             "the same name wins.").classes("text-sm text-grey-7")
                    KVEditor(work.variables, add_label="Add variable")
                with ui.tab_panel("Auth"):
                    ui.label("Requests set to 'Inherit' use this, unless the selected environment has an auth "
                             "override.").classes("text-sm text-grey-7")
                    AuthEditor(work.auth, allowed=[k for k in AUTH_TYPES if k != "inherit"])
                with ui.tab_panel("Description"):
                    ui.textarea(value=work.description, on_change=lambda e: setattr(work, "description", e.value)) \
                        .props("outlined autogrow").classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: d.submit(False)).props("flat no-caps")
                ui.button("Save", on_click=lambda: d.submit(True)).props("no-caps")
        if await d:
            c.variables, c.auth, c.description = work.variables, work.auth, work.description
            WS.save_collection(c)
            ui.notify(f"Saved {c.name}", type="positive")
            self.update_var_warning()
            self.update_runner_check()
        d.delete()

    async def open_import(self) -> None:
        with self.dialog_host, ui.dialog() as d, ui.card().classes("w-[560px]"):
            ui.label("Import").classes("text-lg font-medium")
            ui.markdown("Drop in **Postman collections** (v2.1 export), **Postman environments**, or files "
                        "exported from this app. You can select several at once.")
            ui.upload(multiple=True, auto_upload=True, on_upload=self.handle_import) \
                .props('accept=".json" flat bordered').classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Done", on_click=d.close).props("no-caps")
        d.on("hide", d.delete)
        d.open()

    async def handle_import(self, e) -> None:
        try:
            data = json.loads((await e.file.read()).decode("utf-8-sig"))
        except Exception as exc:
            ui.notify(f"{e.file.name}: not a valid JSON file ({exc})", type="negative")
            return
        kind = postman.detect(data)
        if kind == "postman-collection":
            c, report = postman.import_collection(data)
            WS.save_collection(c)
            ui.notify(f"{c.name}: {report}", type="positive", multi_line=True, timeout=10000)
        elif kind == "restclient-collection":
            c = Collection.from_dict(data)
            if WS.collection_by_id(c.id):
                c.id = Collection().id
            WS.save_collection(c)
            ui.notify(f"Imported collection {c.name}", type="positive")
        elif kind == "postman-environment":
            env = postman.import_environment(data)
            WS.save_environment(env)
            ui.notify(f"Imported environment {env.name}", type="positive")
        elif kind == "restclient-environment":
            env = Environment.from_dict(data)
            if WS.environment_by_id(env.id):
                env.id = Environment().id
            WS.save_environment(env)
            ui.notify(f"Imported environment {env.name}", type="positive")
        else:
            ui.notify(f"{e.file.name}: not a recognised collection or environment file", type="warning")
            return
        self.render_tree.refresh()
        self.refresh_env_options()
        self.refresh_collection_options()
        self.render_env_list.refresh()

    async def reload_all(self) -> None:
        if self.dirty and not await self.confirm("Reload?", "You have unsaved changes to the open request. "
                                                 "Reloading keeps them on screen but they are still unsaved."):
            return
        WS.reload()
        self.render_tree.refresh()
        self.refresh_env_options()
        self.refresh_collection_options()
        self.render_env_list.refresh()
        ui.notify("Reloaded collections and environments from disk")

    # ============================================================ request panel
    def build_request_panel(self) -> None:
        with ui.row().classes("w-full items-center no-wrap gap-1"):
            self.breadcrumb = ui.label().classes("text-grey-7 text-sm whitespace-nowrap")
            self.name_input = ui.input(on_change=self.on_name_change) \
                .props("dense borderless input-class='text-lg font-medium'").classes("flex-grow")
            self.dirty_badge = ui.badge("unsaved", color="orange").props("outline")
            ui.button("New", icon="add", on_click=self.new_request).props("flat no-caps")
            ui.button("Save", icon="save", on_click=self.save_current).props("no-caps unelevated")
            with ui.button(icon="more_vert").props("flat round"):
                with ui.menu():
                    ui.menu_item("Save as…", self.save_as)
                    ui.menu_item("Preview (what will be sent)", self.preview_current)
                    ui.menu_item("Copy as cURL", self.copy_curl)
        # Ctrl+S / Ctrl+Enter: handled in the browser so the browser's own "Save page" dialog is suppressed
        ui.add_body_html("""<script>
            document.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); emitEvent('shortcut_save'); }
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); emitEvent('shortcut_send'); }
            });
        </script>""")
        ui.on("shortcut_save", self.save_current)
        ui.on("shortcut_send", self.send_current)

        with ui.row().classes("w-full items-center no-wrap gap-2"):
            self.method_select = ui.select(METHODS, value="GET", on_change=self.on_method_change) \
                .props("dense outlined options-dense").classes("w-32 font-bold")
            self.url_input = ui.input(placeholder="{{base_url}}/users/{{user_id}}", on_change=self.on_url_change) \
                .props("dense outlined").classes("flex-grow font-mono")
            self.url_input.on("keydown.enter", self.send_current)
            self.url_input.on("blur", self.split_query_into_params)
            self.pages_switch = ui.switch("All pages", on_change=self.on_pages_change) \
                .tooltip("Follow OneRoster paging (limit/offset, Link: next) and merge every page")
            self.send_btn = ui.button("Send", icon="send", on_click=self.send_current) \
                .props("no-caps unelevated").classes("px-6")
        self.var_warning = ui.label().classes("text-orange-9 text-sm")

        with ui.tabs().props("align=left no-caps dense").classes("w-full") as rt:
            self.tabs_request = rt
            self.params_tab = ui.tab("Params")
            self.headers_tab = ui.tab("Headers")
            self.body_tab = ui.tab("Body")
            ui.tab("Auth")
            ui.tab("Notes")
        with ui.tab_panels(rt, value="Params").classes("w-full border rounded").props("animated=false"):
            with ui.tab_panel("Params").classes("p-3"):
                self.params_editor = KVEditor([], self.mark_dirty, add_label="Add query parameter")
            with ui.tab_panel("Headers").classes("p-3"):
                self.headers_editor = KVEditor([], self.mark_dirty, add_label="Add header")
            with ui.tab_panel("Body").classes("p-3"):
                self.body_mode = ui.toggle(BODY_MODES, value="none", on_change=self.on_body_mode) \
                    .props("no-caps dense unelevated toggle-color=primary")
                self.body_editor = ui.codemirror("", language="JSON", on_change=self.on_body_change,
                                                 line_wrapping=True) \
                    .classes("w-full border rounded mt-2").style("height: 220px")
                self.body_tools = ui.row().classes("gap-1")
                with self.body_tools:
                    ui.button("Format JSON", icon="auto_fix_high", on_click=self.format_json) \
                        .props("flat dense no-caps")
                self.form_holder = ui.column().classes("w-full mt-2")
                with self.form_holder:
                    self.form_editor = KVEditor([], self.mark_dirty, add_label="Add field")
            with ui.tab_panel("Auth").classes("p-3"):
                self.auth_editor = AuthEditor(self.current.auth, self.mark_dirty, inherit_hint=self.inherit_hint)
            with ui.tab_panel("Notes").classes("p-3"):
                self.notes = ui.textarea(placeholder="What this request is for, gotchas, ticket links…",
                                         on_change=self.on_notes_change).props("outlined autogrow").classes("w-full")

        with ui.card().classes("w-full mt-3").props("flat bordered"):
            with ui.row().classes("w-full items-center gap-3"):
                ui.label("Response").classes("font-semibold")
                self.status_badge = ui.badge("").classes("text-sm px-2")
                self.status_badge.set_visibility(False)
                self.meta_label = ui.label("Send a request to see the response here.").classes("text-sm text-grey-7")
                ui.space()
                ui.button(icon="content_copy", on_click=self.copy_response).props("flat round dense") \
                    .tooltip("Copy response body")
                ui.button(icon="download", on_click=self.download_response).props("flat round dense") \
                    .tooltip("Save response to a file")
            self.note_label = ui.label().classes("text-sm text-blue-8")
            with ui.tabs().props("align=left no-caps dense") as rtabs:
                ui.tab("Body", label="Body")
                self.resp_headers_tab = ui.tab("Headers", label="Headers")
                ui.tab("Sent", label="Request sent")
            with ui.tab_panels(rtabs, value="Body").classes("w-full").props("animated=false"):
                with ui.tab_panel("Body").classes("p-0"):
                    self.resp_body = ui.codemirror("", language="JSON", line_wrapping=True) \
                        .classes("w-full border rounded").style("height: 45vh")
                    self.resp_body.disable()
                with ui.tab_panel("Headers").classes("p-0"):
                    self.resp_headers = ui.table(rows=[], columns=[
                        {"name": "name", "label": "Header", "field": "name", "align": "left"},
                        {"name": "value", "label": "Value", "field": "value", "align": "left",
                         "style": "white-space: normal; word-break: break-all"}],
                        row_key="name").props("dense flat").classes("w-full")
                with ui.tab_panel("Sent").classes("p-0"):
                    self.sent_view = ui.codemirror("", line_wrapping=True).classes("w-full border rounded") \
                        .style("height: 30vh")
                    self.sent_view.disable()

    def inherit_hint(self) -> str:
        env = self.env()
        if env and env.auth.type != "inherit":
            return f"Uses the auth override from environment '{env.name}': {AUTH_TYPES[env.auth.type]}."
        c = self.collection()
        if c:
            return f"Uses the collection '{c.name}' auth: {AUTH_TYPES.get(c.auth.type, c.auth.type)}."
        return "Not in a collection, so no auth is added. Save the request into a collection or pick a type."

    # ---------------------------------------------------------- field handlers
    def on_name_change(self, e) -> None:
        self.current.name = e.value
        self.mark_dirty()

    def on_method_change(self, e) -> None:
        self.current.method = e.value
        self.mark_dirty()

    def on_url_change(self, e) -> None:
        self.current.url = e.value or ""
        self.mark_dirty()

    def split_query_into_params(self) -> None:
        """When the user leaves the URL box, move a pasted ?a=1&b=2 into the Params table."""
        base, sep, qs = self.current.url.partition("?")
        if not sep or not qs:
            return
        for part in qs.split("&"):
            k, _, v = part.partition("=")
            if k:
                self.current.params.append(KV(k, v))
        self.url_input.value = base  # triggers on_url_change -> marks dirty
        self.params_editor.set_items(self.current.params)
        self.tabs_request.set_value("Params")
        ui.notify("Moved the query string into the Params tab", timeout=2000)

    def on_pages_change(self, e) -> None:
        self.current.fetch_all_pages = bool(e.value)
        self.mark_dirty()

    def on_body_mode(self, e) -> None:
        self.current.body_mode = e.value
        self.update_body_visibility()
        self.mark_dirty()

    def on_body_change(self, e) -> None:
        self.current.body = e.value or ""
        self.mark_dirty()

    def on_notes_change(self, e) -> None:
        self.current.description = e.value
        self.mark_dirty()

    def update_body_visibility(self) -> None:
        mode = self.current.body_mode
        self.body_editor.set_visibility(mode in ("json", "text"))
        self.body_tools.set_visibility(mode == "json")
        self.form_holder.set_visibility(mode in ("urlencoded", "form"))
        self.body_editor.language = "JSON" if mode == "json" else None
        if mode in ("urlencoded", "form"):
            self.form_editor.set_items(self.current.form)

    def format_json(self) -> None:
        try:
            self.body_editor.value = json.dumps(json.loads(self.current.body), indent=2, ensure_ascii=False)
        except ValueError as exc:
            ui.notify(f"Body is not valid JSON: {exc}", type="warning")

    # ------------------------------------------------------- load / save
    def update_breadcrumb(self) -> None:
        c = self.collection()
        if not c:
            self.breadcrumb.text = "Scratch /"
            return
        label = c.request_label(self.current) if c.find_container(self.current.id) is not None else ""
        trail = label.split(" / ")[:-1] if label else ["(not saved yet)"]
        self.breadcrumb.text = " / ".join([c.name] + trail) + " /"

    def load_request(self, req: Request, cid: str | None) -> None:
        self._loading = True
        self.current, self.current_cid = req, cid
        self.name_input.value = req.name
        self.method_select.value = req.method
        self.url_input.value = req.url
        self.pages_switch.value = req.fetch_all_pages
        self.params_editor.set_items(req.params)
        self.headers_editor.set_items(req.headers)
        self.body_mode.value = req.body_mode
        self.body_editor.value = req.body
        self.form_editor.set_items(req.form)
        self.auth_editor.set_auth(req.auth)
        self.notes.value = req.description
        self.update_body_visibility()
        self._loading = False
        self.dirty = False
        self.dirty_badge.set_visibility(False)
        self.update_breadcrumb()
        self.update_var_warning()
        self.render_tree.refresh()

    async def open_request(self, c: Collection, r: Request) -> None:
        if self.dirty and r.id != self.current.id and not await self.confirm(
                "Discard changes?", f"**{self.current.name}** has unsaved changes. Open another request anyway?",
                "Discard changes", "negative"):
            return
        self.load_request(r.clone(), c.id)
        self.tabs.set_value("Request")

    async def new_request(self) -> None:
        if self.dirty and not await self.confirm("Discard changes?", "The open request has unsaved changes.",
                                                 "Discard changes", "negative"):
            return
        self.load_request(Request(name="New request"), self.current_cid)

    async def save_current(self) -> None:
        c = self.collection()
        if c and c.find_container(self.current.id) is not None:
            c.replace_request(self.current.clone())
            WS.save_collection(c)
            self.dirty = False
            self.dirty_badge.set_visibility(False)
            self.render_tree.refresh()
            self.refresh_request_options()
            ui.notify("Saved", type="positive", timeout=1200)
        else:
            await self.save_as()

    async def save_as(self) -> None:
        if not WS.collections:
            ui.notify("Create a collection first (left panel, 'New collection').", type="warning")
            return
        choice = {"cid": self.current_cid if self.collection() else WS.collections[0].id, "fid": None,
                  "name": self.current.name}

        def folder_options(cid):
            c = WS.collection_by_id(cid)
            opts = {None: "(top level)"}
            if c:
                opts.update({f.id: " / ".join(trail) for trail, f in c.folders()})
            return opts

        with self.dialog_host, ui.dialog() as d, ui.card().classes("w-[480px]"):
            ui.label("Save request").classes("text-lg font-medium")
            ui.input("Name", value=choice["name"], on_change=lambda e: choice.update(name=e.value)) \
                .props("outlined dense").classes("w-full")

            def pick_collection(e):
                choice.update(cid=e.value, fid=None)
                folder_sel.options = folder_options(e.value)
                folder_sel.value = None
                folder_sel.update()

            ui.select({c.id: c.name for c in WS.collections}, value=choice["cid"], label="Collection",
                      on_change=pick_collection).props("outlined dense").classes("w-full")
            folder_sel = ui.select(folder_options(choice["cid"]), value=None, label="Folder",
                                   on_change=lambda e: choice.update(fid=e.value)).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: d.submit(False)).props("flat no-caps")
                ui.button("Save", on_click=lambda: d.submit(True)).props("no-caps")
        ok = await d
        d.delete()
        if not ok:
            return
        c = WS.collection_by_id(choice["cid"])
        target = c.find_folder(choice["fid"]).items if choice["fid"] else c.items
        saved = self.current.clone(new_identity=c.find_request(self.current.id) is not None)
        saved.name = (choice["name"] or "").strip() or "New request"
        target.append(saved)
        WS.save_collection(c)
        self.expanded.add(c.id)
        if choice["fid"]:
            self.expanded.add(choice["fid"])
        self.load_request(saved.clone(), c.id)
        self.refresh_request_options()
        ui.notify(f"Saved to {c.name}", type="positive")

    # ------------------------------------------------------- variables
    def update_var_warning(self) -> None:
        missing = sorted(missing_variables(self.current, self.collection(), self.env()))
        if missing:
            where = "Pick an environment (top right) or add them" if not self.env() else \
                f"Add them to '{self.env().name}' or the collection variables"
            self.var_warning.text = "⚠ Not set (or blank): " + ", ".join("{{%s}}" % m for m in missing) + f". {where}."
        else:
            self.var_warning.text = ""

    # ------------------------------------------------------- sending
    async def send_current(self) -> None:
        if not self.current.url.strip():
            ui.notify("Enter a URL first", type="warning")
            return
        req = self.current.clone()
        c, env = self.collection(), self.env()
        self.send_btn.props("loading")
        try:
            rr, res = await run.io_bound(engine.execute, req, c, env, WS.settings)
        finally:
            self.send_btn.props(remove="loading")
        self.show_result(rr, res)

    def show_result(self, rr: engine.ResolvedRequest, res: engine.Result) -> None:
        self.last_result, self.last_resolved = res, rr
        self.status_badge.set_visibility(True)
        if res.error:
            self.status_badge.text = "Error"
            self.status_badge.props("color=red")
            self.meta_label.text = f"{res.elapsed_ms} ms"
            self.resp_body.language = None
            self.resp_body.value = res.error
        else:
            color = "green" if res.status < 300 else "blue" if res.status < 400 else "orange" if res.status < 500 else "red"
            self.status_badge.text = f"{res.status} {res.reason}"
            self.status_badge.props(f"color={color}")
            self.meta_label.text = f"{res.elapsed_ms} ms · {engine.human_size(res.size)} · {res.method} {res.url}"
            body = res.pretty_body()
            is_json = res.json() is not None
            self.resp_body.language = "JSON" if is_json else ("XML" if body.lstrip().startswith("<") else None)
            self.resp_body.value = body if len(body) < 5_000_000 else body[:5_000_000] + "\n… (truncated; download for full)"
        self.note_label.text = res.note
        self.resp_headers.rows = [{"name": k, "value": v} for k, v in res.headers.items()]
        self.resp_headers.update()
        self.resp_headers_tab.props(f'label="Headers ({len(res.headers)})"')
        sent = [f"{res.method or rr.method} {res.url or rr.url}"] + [f"{k}: {v}" for k, v in res.request_headers.items()]
        if rr.body:
            sent += ["", rr.body]
        self.sent_view.value = "\n".join(sent)

    def preview_current(self) -> None:
        rr = engine.resolve(self.current, self.collection(), self.env())
        extra = ""
        if rr.unresolved():
            extra = "\n\n⚠ Not set: " + ", ".join("{{%s}}" % v for v in sorted(rr.unresolved()))
        self.show_text("Preview — nothing has been sent", rr.describe() + extra)

    def copy_curl(self) -> None:
        rr = engine.resolve(self.current, self.collection(), self.env())
        try:
            curl = engine.to_curl(rr, WS.settings)
        except Exception as exc:
            ui.notify(f"Could not build cURL: {exc}", type="negative")
            return
        self.show_text("cURL (includes credentials, so share it carefully)", curl, "Shell")

    def copy_response(self) -> None:
        if self.last_result:
            ui.clipboard.write(self.last_result.pretty_body() or self.last_result.error or "")
            ui.notify("Response copied", timeout=1000)

    def download_response(self) -> None:
        if not self.last_result:
            return
        is_json = self.last_result.json() is not None
        name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in self.current.name).strip() or "response"
        ui.download.content(self.last_result.pretty_body().encode("utf-8"), f"{name}.{'json' if is_json else 'txt'}")

    # ============================================================ bulk runner
    def build_runner_panel(self) -> None:
        ui.markdown("Run one or more saved requests **once per row** of a CSV file. Each CSV column fills the "
                    "`{{variable}}` with the same name, e.g. a column `user_id` fills `{{user_id}}`.") \
            .classes("text-sm text-grey-8")
        with ui.row().classes("w-full gap-4 items-stretch"):
            with ui.card().classes("flex-1 min-w-[320px]").props("flat bordered"):
                ui.label("1. What to run").classes("font-semibold")
                self.run_collection = ui.select({}, label="Collection", on_change=self.on_run_collection) \
                    .props("outlined dense").classes("w-full")
                self.run_requests = ui.select({}, label="Request(s), run in this order for each row", multiple=True,
                                              on_change=lambda: self.update_runner_check()) \
                    .props("outlined dense use-chips").classes("w-full")
                ui.label("Uses the environment selected at the top of the page.").classes("text-xs text-grey-7")
            with ui.card().classes("flex-1 min-w-[320px]").props("flat bordered"):
                ui.label("2. Data file (CSV)").classes("font-semibold")
                self.run_upload = ui.upload(auto_upload=True, on_upload=self.on_csv_upload, max_files=1) \
                    .props('accept=".csv,.txt" flat bordered label="Choose or drop a CSV file"').classes("w-full")
                self.csv_info = ui.label("No file loaded.").classes("text-sm text-grey-7")
            with ui.card().classes("flex-1 min-w-[320px]").props("flat bordered"):
                ui.label("3. Options").classes("font-semibold")
                with ui.row().classes("w-full no-wrap gap-2"):
                    self.opt_expect = ui.input("Expected status", placeholder="blank = any success") \
                        .props("outlined dense").classes("flex-1").tooltip('e.g. 200, 2xx or "200, 201"')
                    self.opt_delay = ui.number("Delay between calls (ms)", value=250, min=0, step=50) \
                        .props("outlined dense").classes("flex-1")
                self.opt_extract = ui.input("Save from response (optional)", placeholder="id=id, name=name_display") \
                    .props("outlined dense").classes("w-full") \
                    .tooltip("name=path pairs. Values become columns in the results and {{variables}} for the "
                             "next request in the same row.")
                with ui.row().classes("gap-4"):
                    self.opt_stop = ui.switch("Stop on first failure")
                    self.opt_retry = ui.switch("Retry when rate-limited (429)", value=True)

        self.csv_check = ui.column().classes("w-full gap-1 mt-2")
        self.csv_preview = ui.table(rows=[], columns=[], row_key="_n").props("dense flat bordered") \
            .classes("w-full mt-1")
        self.csv_preview.set_visibility(False)

        with ui.row().classes("w-full items-center gap-2 mt-3"):
            ui.button("Preview first row", icon="visibility", on_click=self.preview_run).props("outline no-caps")
            self.run_btn = ui.button("Run", icon="play_arrow", on_click=self.start_run).props("no-caps unelevated")
            self.stop_btn = ui.button("Stop", icon="stop", on_click=self.stop_run).props("no-caps color=red outline")
            self.stop_btn.set_visibility(False)
            ui.space()
            self.only_failures = ui.switch("Show failures only", on_change=lambda: self._render_results(force=True))
            ui.button("Download results", icon="download", on_click=self.download_results).props("flat no-caps")
            ui.button("Open results folder", icon="folder_open",
                      on_click=lambda: self.open_folder(WS.settings.results_path)).props("flat no-caps")
        self.progress = ui.linear_progress(value=0, show_value=False).classes("mt-2")
        self.progress_label = ui.label("").classes("text-sm")
        self.results_table = ui.table(rows=[], columns=[
            {"name": "row", "label": "Row", "field": "row", "align": "left", "sortable": True},
            {"name": "request", "label": "Request", "field": "request", "align": "left"},
            {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
            {"name": "result", "label": "Result", "field": "result", "align": "left", "sortable": True},
            {"name": "time", "label": "ms", "field": "time", "align": "right", "sortable": True},
            {"name": "inputs", "label": "Inputs", "field": "inputs", "align": "left"},
            {"name": "details", "label": "Details (click a row for the full response)", "field": "details",
             "align": "left", "style": "max-width: 480px; white-space: normal; word-break: break-word"},
        ], row_key="idx", pagination=50).props("dense flat bordered").classes("w-full")
        self.results_table.add_slot("body-cell-result", """
            <q-td :props="props"><q-badge :color="props.value === 'PASS' ? 'green' : 'red'" :label="props.value"/></q-td>
        """)
        self.results_table.on("rowClick", self.on_result_click)
        self.refresh_collection_options()

    def refresh_collection_options(self) -> None:
        self.run_collection.options = {c.id: c.name for c in WS.collections}
        if self.run_collection.value not in self.run_collection.options:
            self.run_collection.value = None
        self.run_collection.update()
        self.refresh_request_options()

    def refresh_request_options(self) -> None:
        c = WS.collection_by_id(self.run_collection.value)
        opts = {r.id: f"{r.method}  {' / '.join(t + [r.name])}" for t, r in c.walk()} if c else {}
        self.run_requests.options = opts
        self.run_requests.value = [v for v in (self.run_requests.value or []) if v in opts]
        self.run_requests.update()
        self.update_runner_check()

    def on_run_collection(self, e) -> None:
        self.run_requests.value = []
        self.refresh_request_options()

    def selected_run_requests(self) -> list[Request]:
        c = WS.collection_by_id(self.run_collection.value)
        if not c:
            return []
        by_id = {r.id: r for _, r in c.walk()}
        return [by_id[i] for i in (self.run_requests.value or []) if i in by_id]

    async def on_csv_upload(self, e) -> None:
        data = await e.file.read()
        self.run_columns, self.run_rows = runner.read_csv_bytes(data)
        self.run_file = e.file.name
        self.run_upload.reset()
        self.csv_info.text = f"{self.run_file}: {len(self.run_rows)} row(s), columns: {', '.join(self.run_columns)}"
        preview = [{"_n": i, **row} for i, row in enumerate(self.run_rows[:5], start=1)]
        self.csv_preview.columns = [{"name": "_n", "label": "#", "field": "_n", "align": "left"}] + \
            [{"name": c, "label": c, "field": c, "align": "left"} for c in self.run_columns]
        self.csv_preview.rows = preview
        self.csv_preview.set_visibility(bool(preview))
        self.csv_preview.update()
        self.update_runner_check()

    def update_runner_check(self) -> None:
        if not hasattr(self, "csv_check"):
            return
        self.csv_check.clear()
        reqs = self.selected_run_requests()
        if not reqs:
            return
        c = WS.collection_by_id(self.run_collection.value)
        extract_names = {n for n, _ in runner.parse_extract(self.opt_extract.value or "")}
        sample = self.run_rows[0] if self.run_rows else {c_: "x" for c_ in self.run_columns}
        needed, missing = set(), set()
        for r in reqs:
            needed |= request_variables(r, c, self.env())
            missing |= missing_variables(r, c, self.env(), sample) - extract_names
        with self.csv_check:
            with ui.row().classes("items-center gap-1"):
                ui.label("Variables used:").classes("text-sm")
                for v in sorted(needed):
                    source = "CSV" if v in self.run_columns else "extracted" if v in extract_names else \
                        "missing" if v in missing else "environment/collection"
                    color = {"CSV": "green", "missing": "red", "extracted": "purple"}.get(source, "blue-grey")
                    ui.chip(v, color=color, text_color="white").props("dense square").tooltip(f"from {source}")
            if missing:
                ui.label("Red variables are not in the CSV, the environment or the collection; those requests "
                         "will fail.").classes("text-sm text-red-8")

    def _run_options(self) -> runner.RunOptions:
        return runner.RunOptions(delay_ms=int(self.opt_delay.value or 0), expected_status=self.opt_expect.value or "",
                                 extract=self.opt_extract.value or "", stop_on_error=self.opt_stop.value,
                                 retry_on_429=self.opt_retry.value)

    def preview_run(self) -> None:
        reqs = self.selected_run_requests()
        if not reqs or not self.run_rows:
            ui.notify("Pick request(s) and load a CSV first", type="warning")
            return
        c = WS.collection_by_id(self.run_collection.value)
        parts = [f"Row 1 of {len(self.run_rows)}: {self.run_rows[0]}", ""]
        for r in reqs:
            rr = engine.resolve(r, c, self.env(), self.run_rows[0])
            parts += [f"--- {r.name} ---", rr.describe(), ""]
        self.show_text("Preview — nothing has been sent", "\n".join(parts))

    async def start_run(self) -> None:
        reqs = self.selected_run_requests()
        c = WS.collection_by_id(self.run_collection.value)
        if not c or not reqs:
            ui.notify("Pick a collection and at least one request", type="warning")
            return
        if not self.run_rows:
            ui.notify("Load a CSV file first", type="warning")
            return
        env = self.env()
        writes = sorted({r.method for r in reqs} & WRITE_METHODS)
        total = len(self.run_rows) * len(reqs)
        if writes:
            ok = await self.confirm(
                "This run will change data",
                f"You are about to send up to **{total}** request(s), including **{', '.join(writes)}**, using "
                f"environment **{env.name if env else '(none)'}**.\n\nTip: use *Preview first row* and try a CSV "
                f"with 1–2 rows first.", "Run it", "negative")
            if not ok:
                return
        self.run_results = []
        self._shown_results = -1
        self._run_total = total
        self.run_cancel = threading.Event()
        self.running = True
        self.run_btn.disable()
        self.stop_btn.set_visibility(True)
        try:
            await run.io_bound(runner.run, c, reqs, env, self.run_rows, self._run_options(), WS.settings,
                               self.run_results.append, self.run_cancel)
        finally:
            self.running = False
            self.run_btn.enable()
            self.stop_btn.set_visibility(False)
        self._render_results(force=True)
        if self.run_results:
            path = runner.save_results(self.run_results, WS.settings, f"{c.name}_{reqs[0].name}")
            failed = sum(1 for r in self.run_results if not r.passed)
            stopped = " (stopped early)" if self.run_cancel.is_set() else ""
            ui.notify(f"Run finished{stopped}: {len(self.run_results) - failed} passed, {failed} failed. "
                      f"Saved {path.name}", type="positive" if not failed else "warning", timeout=8000)

    def stop_run(self) -> None:
        if self.run_cancel:
            self.run_cancel.set()
            ui.notify("Stopping after the current request…")

    def poll_runner(self) -> None:
        if self.running:
            self._render_results()

    def _render_results(self, force: bool = False) -> None:
        n = len(self.run_results)
        if n == self._shown_results and not force:
            return
        self._shown_results = n
        total = getattr(self, "_run_total", n) or 1
        failed = sum(1 for r in self.run_results if not r.passed)
        self.progress.value = min(n / total, 1.0)
        state = "Running" if self.running else "Done"
        self.progress_label.text = f"{state}: {n} of up to {total} request(s) · {n - failed} passed · {failed} failed"
        rows = []
        for idx, r in enumerate(self.run_results):
            if self.only_failures.value and r.passed:
                continue
            rows.append({**r.as_table_row(), "idx": idx})
        self.results_table.rows = rows
        self.results_table.update()

    def on_result_click(self, e) -> None:
        try:
            row = e.args[1]
            step = self.run_results[row["idx"]]
        except (IndexError, KeyError, TypeError):
            return
        try:
            body = json.dumps(json.loads(step.response), indent=2)
        except ValueError:
            body = step.response
        text = (f"Row {step.row} · {step.request}\n{step.method} {step.url}\nStatus: {step.status}  "
                f"Result: {'PASS' if step.passed else 'FAIL'}  ({step.elapsed_ms} ms)\n"
                f"{('Error: ' + step.error) if step.error else ''}\nInputs: {step.inputs}\n"
                f"Extracted: {step.extracted}\n\n{body}")
        self.show_text(f"Row {step.row} details", text)

    def download_results(self) -> None:
        if not self.run_results:
            ui.notify("No results yet", type="warning")
            return
        ui.download.content(runner.results_to_csv(self.run_results).encode("utf-8-sig"), "bulk_run_results.csv")

    def open_folder(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - opens Explorer on the user's own machine
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])  # noqa: S603,S607 - opens Finder
            else:
                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        except OSError as exc:
            ui.notify(f"{path} ({exc})")

    # ============================================================ environments
    def build_env_panel(self) -> None:
        with ui.row().classes("w-full no-wrap gap-4 items-start"):
            with ui.card().classes("w-80 shrink-0").props("flat bordered"):
                with ui.row().classes("w-full items-center"):
                    ui.label("Environments").classes("font-semibold")
                    ui.space()
                    ui.button(icon="add", on_click=self.new_environment).props("flat round dense") \
                        .tooltip("New environment")
                    ui.button(icon="upload_file", on_click=self.open_import).props("flat round dense") \
                        .tooltip("Import")
                ui.label("One per district / SIS connection. Holds base URLs, ids and credentials.") \
                    .classes("text-xs text-grey-7")
                self.render_env_list()
            self.env_editor = ui.column().classes("flex-grow")
            with self.env_editor:
                ui.label("Pick an environment on the left to edit it.").classes("text-grey-7")

    @ui.refreshable_method
    def render_env_list(self) -> None:
        with ui.list().props("dense separator").classes("w-full"):
            for env in WS.environments:
                active = self.env_edit is not None and self.env_edit.id == env.id
                with ui.item(on_click=lambda env=env: self.edit_environment(env)) \
                        .classes(f"req-item {'active' if active else ''}").props("clickable"):
                    with ui.item_section():
                        ui.item_label(env.name)
                        if env.auth.type != "inherit":
                            ui.item_label(f"auth: {env.auth.type}").props("caption")
                    if env.id == self.env_id:
                        with ui.item_section().props("side"):
                            ui.icon("check_circle", color="green", size="xs").tooltip("In use")

    async def new_environment(self) -> None:
        name = await self.prompt("New environment", "Name (e.g. 'Springfield USD - PowerSchool OneRoster 1.1')")
        if name:
            env = Environment(name=name)
            WS.save_environment(env)
            self.refresh_env_options()
            self.render_env_list.refresh()
            self.edit_environment(env)

    def edit_environment(self, env: Environment) -> None:
        work = Environment.from_dict(copy.deepcopy(env.to_dict()), env.path)
        self.env_edit = work
        self.render_env_list.refresh()
        self.env_editor.clear()
        with self.env_editor:
            with ui.row().classes("w-full items-center gap-2"):
                ui.input("Name", value=work.name, on_change=lambda e: setattr(work, "name", e.value)) \
                    .props("outlined dense").classes("flex-grow text-lg")
                ui.button("Use this environment", icon="check",
                          on_click=lambda: (self.env_select.set_value(work.id))).props("outline no-caps")
                ui.button("Save", icon="save", on_click=lambda: self.save_environment(work)).props("no-caps unelevated")
                with ui.button(icon="more_vert").props("flat round"):
                    with ui.menu():
                        ui.menu_item("Duplicate (e.g. for another district)", lambda: self.duplicate_environment(work))
                        ui.menu_item("Export for Postman", lambda: ui.download.content(
                            json.dumps(postman.export_environment(work), indent=2).encode(),
                            f"{work.name}.postman_environment.json"))
                        ui.menu_item("Delete", lambda: self.delete_environment(work)).classes("text-red-8")
            ui.textarea("Notes", value=work.description, on_change=lambda e: setattr(work, "description", e.value)) \
                .props("outlined dense autogrow").classes("w-full")
            ui.label("Variables").classes("font-semibold mt-2")
            ui.label("Use them anywhere as {{name}}. Click the lock to mark a value as secret.") \
                .classes("text-xs text-grey-7")
            KVEditor(work.variables, secrets=True, key_label="Variable", add_label="Add variable")
            ui.label("Auth override (optional)").classes("font-semibold mt-4")
            ui.label("Use this when a district's SIS needs a different auth method than the collection default, "
                     "e.g. Infinite Campus OneRoster 1.1 = OAuth 1.0 HMAC-SHA256 while PowerSchool uses OAuth 2.0.") \
                .classes("text-xs text-grey-7")
            AuthEditor(work.auth, inherit_label="No override (use the collection's auth)",
                       inherit_hint=lambda: "Requests use their collection's auth.")

    def save_environment(self, work: Environment) -> None:
        original = WS.environment_by_id(work.id)
        if original:
            original.name, original.description = work.name, work.description
            original.variables, original.auth = copy.deepcopy(work.variables), copy.deepcopy(work.auth)
            WS.save_environment(original)
            work.path = original.path
        else:
            WS.save_environment(work)
        engine.clear_token_cache()
        self.refresh_env_options()
        self.render_env_list.refresh()
        self.update_var_warning()
        self.update_runner_check()
        ui.notify(f"Saved {work.name}", type="positive")

    async def duplicate_environment(self, work: Environment) -> None:
        name = await self.prompt("Duplicate environment", "New name", work.name + " (copy)")
        if name:
            twin = Environment.from_dict(copy.deepcopy(work.to_dict()))
            twin.id, twin.name = Environment().id, name
            WS.save_environment(twin)
            self.refresh_env_options()
            self.render_env_list.refresh()
            self.edit_environment(twin)

    async def delete_environment(self, work: Environment) -> None:
        original = WS.environment_by_id(work.id)
        if original and await self.confirm("Delete environment?", f"Delete **{original.name}**?", "Delete", "negative"):
            WS.delete_environment(original)
            self.env_edit = None
            self.refresh_env_options()
            self.render_env_list.refresh()
            self.env_editor.clear()

    # ============================================================== AI & help
    def build_ai_panel(self) -> None:
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        repo = Path(__file__).resolve().parent.parent
        frozen = getattr(sys, "frozen", False)
        vscode = json.dumps({"servers": {"schoology-api-client": {
            "type": "stdio", "command": str(py), "args": ["-m", "restclient.mcp_server"],
            "cwd": str(repo)}}}, indent=2)
        mcpo = f'uvx mcpo --port 8000 --api-key "CHANGE-ME" -- "{py}" -m restclient.mcp_server'

        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("flex-1 min-w-[420px]").props("flat bordered"):
                ui.label("Connect an AI assistant").classes("text-lg font-semibold")
                ui.markdown(
                    "The app includes an **MCP server** so an AI chat can list your collections, preview and "
                    "send saved requests, and run bulk jobs, using the same environments and credentials as this "
                    "app. Credentials marked secret are never shown to the AI.")
                if frozen:
                    ui.label("Note: the AI connection needs the Python (source) install of this app, not the stand-alone build.") \
                        .classes("text-orange-9 text-sm")
                ui.markdown("**GitHub Copilot (VS Code, Agent mode)**: save as `.vscode/mcp.json` in any folder "
                            "you open in VS Code (or add to your user MCP settings):")
                ui.code(vscode, language="json").classes("w-full")
                ui.markdown("**Open WebUI / other chatbots via mcpo** (turns the MCP server into an OpenAPI "
                            "tool server). Run this, then add `http://localhost:8000` as a *Tool Server*:")
                ui.code(mcpo, language="bash").classes("w-full")
                ui.markdown("**Claude Desktop / Cursor / others**: use the same command and arguments as the "
                            "Copilot example.")
            with ui.card().classes("w-96").props("flat bordered"):
                ui.label("AI safety switches").classes("text-lg font-semibold")
                ui.label("By default the AI can only read (GET) using saved requests.").classes("text-sm text-grey-7")
                ui.switch("Allow AI to change data (POST/PUT/PATCH/DELETE)",
                          value=WS.settings.mcp_allow_write_methods,
                          on_change=lambda e: self._set_setting("mcp_allow_write_methods", e.value))
                ui.switch("Allow AI to call URLs that aren't saved in a collection",
                          value=WS.settings.mcp_allow_adhoc_requests,
                          on_change=lambda e: self._set_setting("mcp_allow_adhoc_requests", e.value))
                ui.label("Changes apply to the AI's next tool call.").classes("text-xs text-grey-7")
                ui.separator().classes("my-2")
                ui.label("Quick help").classes("text-lg font-semibold")
                ui.markdown(
                    "- **Pick an environment** (top right) for the district/SIS you're working on.\n"
                    "- Click a request on the left, fill in ids (or set them in the environment), press **Send**.\n"
                    "- `{{name}}` is a variable. Orange warnings list ones that aren't set.\n"
                    "- **Ctrl+S** saves, **Ctrl+Enter** sends (**⌘S** / **⌘Enter** on a Mac).\n"
                    "- **All pages** pulls every page of a OneRoster list.\n"
                    "- **Bulk Runner**: pick requests, load a CSV, preview, run, download results.\n"
                    f"- Your files live in `{app_home()}`.")
                with ui.row():
                    ui.button("Open data folder", icon="folder_open",
                              on_click=lambda: self.open_folder(app_home())).props("flat no-caps")
                ui.label(f"Version {__version__}").classes("text-xs text-grey-6")

    def _set_setting(self, name: str, value) -> None:
        setattr(WS.settings, name, value)
        save_settings(WS.settings)
        ui.notify("Saved", timeout=800)

    # ================================================================ settings
    async def open_settings(self) -> None:
        s = copy.deepcopy(WS.settings)
        with self.dialog_host, ui.dialog() as d, ui.card().classes("w-[640px] max-w-full"):
            ui.label("Settings").classes("text-lg font-medium")
            ui.label("Folders (leave blank for the default). Point Collections at a shared drive so the whole "
                     "team uses the same collections; keep Environments local because they hold credentials.") \
                .classes("text-sm text-grey-7")
            for attr, label in (("collections_dir", "Collections folder"), ("environments_dir", "Environments folder"),
                                ("results_dir", "Bulk run results folder")):
                default = {"collections_dir": WS.settings.collections_path, "environments_dir": WS.settings.environments_path,
                           "results_dir": WS.settings.results_path}[attr]
                ui.input(label, value=getattr(s, attr), placeholder=str(default),
                         on_change=lambda e, attr=attr: setattr(s, attr, e.value)).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full no-wrap gap-2"):
                ui.number("Timeout (seconds)", value=s.timeout_seconds, min=1, max=600,
                          on_change=lambda e: setattr(s, "timeout_seconds", float(e.value or 30))) \
                    .props("outlined dense").classes("flex-1")
                ui.number("Port", value=s.port, min=1024, max=65535, precision=0,
                          on_change=lambda e: setattr(s, "port", int(e.value or 8765))) \
                    .props("outlined dense").classes("flex-1").tooltip("Takes effect after restart")
            ui.switch("Use system certificates - Windows store / macOS Keychain (needed on most company networks; restart to apply)",
                      value=s.use_system_certificates, on_change=lambda e: setattr(s, "use_system_certificates", e.value))
            ui.switch("Verify SSL certificates (only turn off for testing)", value=s.verify_ssl,
                      on_change=lambda e: setattr(s, "verify_ssl", e.value))
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: d.submit(False)).props("flat no-caps")
                ui.button("Save", on_click=lambda: d.submit(True)).props("no-caps")
        ok = await d
        d.delete()
        if ok:
            save_settings(s)
            WS.settings = s
            WS.reload()
            self.render_tree.refresh()
            self.refresh_env_options()
            self.refresh_collection_options()
            self.render_env_list.refresh()
            ui.notify("Settings saved", type="positive")

    async def quit_app(self) -> None:
        if self.dirty and not await self.confirm("Quit?", "The open request has unsaved changes.", "Quit anyway",
                                                 "negative"):
            return
        if self.running:
            ui.notify("A bulk run is in progress. Stop it first.", type="warning")
            return
        ui.notify("Closing… you can close this browser tab.")
        ui.run_javascript("setTimeout(() => window.close(), 500)")
        ui.timer(0.8, app.shutdown, once=True)


# ====================================================================== startup ======
def start(port: int, show: bool = True) -> None:
    global WS
    WS = Workspace()

    @app.get("/healthz")
    def healthz():
        return {"app": "schoology-api-client", "version": __version__}

    @ui.page("/")
    def index():
        ClientPage()

    ui.run(host="127.0.0.1", port=port, title=APP_NAME, favicon="🛰️", reload=False, show=show,
           show_welcome_message=False, dark=False, reconnect_timeout=30)
