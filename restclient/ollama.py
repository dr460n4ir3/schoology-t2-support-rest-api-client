"""Tiny client for a local Ollama server (https://ollama.com). Plain HTTP, no extra packages.

Ollama runs on this computer, so questions, documents and API responses never leave it.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable

import requests

DOWNLOAD_URL = "https://ollama.com/download"


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 600):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ status
    def version(self) -> str | None:
        """Ollama's version, or None when it isn't installed / running."""
        try:
            r = requests.get(f"{self.base}/api/version", timeout=3)
            r.raise_for_status()
            return r.json().get("version", "?")
        except (requests.RequestException, ValueError):
            return None

    def models(self) -> list[str]:
        r = self._get("/api/tags")
        return sorted(m["name"] for m in r.get("models", []))

    def has_model(self, name: str, installed: Iterable[str] | None = None) -> bool:
        installed = list(installed if installed is not None else self.models())
        want = name if ":" in name else f"{name}:latest"
        return want in installed or name in installed

    def pull(self, name: str, progress: Callable[[str, float | None], None] | None = None) -> None:
        """Download a model. progress(status, fraction 0-1 or None)."""
        try:
            with requests.post(f"{self.base}/api/pull", json={"model": name, "stream": True},
                               stream=True, timeout=self.timeout) as r:
                self._raise(r)
                for line in r.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise OllamaError(data["error"])
                    if progress:
                        total, done = data.get("total"), data.get("completed")
                        progress(data.get("status", ""), (done / total) if total and done else None)
        except requests.RequestException as e:
            raise OllamaError(f"Could not reach Ollama at {self.base}: {e}") from e

    # -------------------------------------------------------------------- chat
    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             num_ctx: int | None = None, temperature: float = 0.2) -> dict:
        """One (non-streaming) chat call. Returns the assistant message dict."""
        payload: dict = {"model": model, "messages": messages, "stream": False,
                         "options": {"temperature": temperature}}
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx
        if tools:
            payload["tools"] = tools
        return self._post("/api/chat", payload).get("message", {})

    def embed(self, model: str, texts: list[str], batch: int = 32) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            data = self._post("/api/embed", {"model": model, "input": texts[i:i + batch]})
            out.extend(data.get("embeddings", []))
        if len(out) != len(texts):
            raise OllamaError(f"Embedding model '{model}' returned {len(out)} vectors for {len(texts)} texts.")
        return out

    # ----------------------------------------------------------------- helpers
    def _get(self, path: str) -> dict:
        try:
            r = requests.get(self.base + path, timeout=15)
        except requests.RequestException as e:
            raise OllamaError(f"Could not reach Ollama at {self.base}. Is it installed and running? ({e})") from e
        self._raise(r)
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = requests.post(self.base + path, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise OllamaError(f"Could not reach Ollama at {self.base}. Is it installed and running? ({e})") from e
        self._raise(r)
        return r.json()

    @staticmethod
    def _raise(r: requests.Response) -> None:
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            raise OllamaError(f"Ollama error {r.status_code}: {msg}")
