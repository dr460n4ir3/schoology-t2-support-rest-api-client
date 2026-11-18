"""In-memory view of all collections + environments, with name lookups."""

from __future__ import annotations

from .config import Settings, load_settings
from .models import Collection, Environment
from .storage import Store


class Workspace:
    def __init__(self, settings: Settings | None = None, seed: bool = True):
        self.settings = settings or load_settings()
        self.store = Store(self.settings)
        if seed:
            self.store.seed_examples()
        self.collections: list[Collection] = []
        self.environments: list[Environment] = []
        self.reload()

    def reload(self) -> None:
        self.store = Store(self.settings)
        self.collections = self.store.load_collections()
        self.environments = self.store.load_environments()

    # ------------------------------------------------------------------ lookups
    def collection(self, ref: str) -> Collection:
        ref_l = ref.strip().lower()
        for c in self.collections:
            if c.id == ref or c.name.lower() == ref_l:
                return c
        names = ", ".join(c.name for c in self.collections) or "(none)"
        raise LookupError(f"No collection named '{ref}'. Available: {names}")

    def collection_by_id(self, cid: str | None) -> Collection | None:
        return next((c for c in self.collections if c.id == cid), None)

    def environment(self, ref: str | None) -> Environment | None:
        if not ref:
            return None
        ref_l = ref.strip().lower()
        for e in self.environments:
            if e.id == ref or e.name.lower() == ref_l:
                return e
        names = ", ".join(e.name for e in self.environments) or "(none)"
        raise LookupError(f"No environment named '{ref}'. Available: {names}")

    def environment_by_id(self, eid: str | None) -> Environment | None:
        return next((e for e in self.environments if e.id == eid), None)

    # ------------------------------------------------------------------ changes
    def save_collection(self, c: Collection) -> None:
        self.store.save_collection(c)
        if c not in self.collections:
            self.collections.append(c)
        self.collections.sort(key=lambda x: x.name.lower())

    def delete_collection(self, c: Collection) -> None:
        self.store.delete_collection(c)
        self.collections = [x for x in self.collections if x.id != c.id]

    def save_environment(self, e: Environment) -> None:
        self.store.save_environment(e)
        if e not in self.environments:
            self.environments.append(e)
        self.environments.sort(key=lambda x: x.name.lower())

    def delete_environment(self, e: Environment) -> None:
        self.store.delete_environment(e)
        self.environments = [x for x in self.environments if x.id != e.id]
