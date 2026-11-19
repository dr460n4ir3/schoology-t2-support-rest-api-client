"""Knowledge base for the local AI assistant (retrieval-augmented generation, "RAG").

Upload PDFs, Word files, text/markdown/HTML (runbooks, KB articles, Jira exports, API docs). They are
split into chunks, turned into vectors with a local Ollama embedding model and stored in
<app home>/knowledge (or Settings -> knowledge_dir, e.g. a shared drive):

    knowledge/files/<original files>     kept so the index can be rebuilt
    knowledge/index.json                 document list + chunk text
    knowledge/vectors.bin                float32 vectors, one row per chunk (normalised)

No vector database needed: a support team's documents are a few thousand chunks at most, which a plain
dot product searches in milliseconds.
"""

from __future__ import annotations

import io
import json
import math
import operator
import re
import shutil
import threading
import zipfile
from array import array
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from .ollama import Ollama, OllamaError

SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".json", ".log", ".xml"}
CHUNK_CHARS = 1200
OVERLAP_CHARS = 200


# ================================================================ text extraction ===
class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _docx_text(data: bytes) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in p.iter(f"{ns}t"))
        if text.strip():
            paras.append(text)
    return "\n\n".join(paras)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ValueError("Reading PDFs needs the 'pypdf' package (run the launcher again to install it).") from e
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED))}")
    if ext == ".pdf":
        return _pdf_text(data)
    if ext == ".docx":
        return _docx_text(data)
    text = data.decode("utf-8-sig", errors="replace")
    if ext in (".html", ".htm"):
        parser = _HTMLText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split on blank lines, pack paragraphs up to `size` chars, carry a little overlap between chunks."""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:  # hard-split paragraphs that are too long on their own
        while len(p) > size:
            cut = p.rfind(" ", size // 2, size)
            cut = cut if cut > 0 else size
            pieces.append(p[:cut].strip())
            p = p[max(cut - overlap, 0):].strip()
        if p:
            pieces.append(p)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > size:
            chunks.append(current)
            tail = current[-overlap:]
            current = tail[tail.find(" ") + 1:] if " " in tail else ""
        current = f"{current}\n\n{piece}" if current else piece
    if current.strip():
        chunks.append(current)
    return chunks


# ================================================================= the index ========
def _prefixes(embed_model: str) -> tuple[str, str]:
    """Some embedding models expect task prefixes on documents / queries."""
    m = embed_model.lower()
    if "nomic" in m:
        return "search_document: ", "search_query: "
    if "mxbai" in m:
        return "", "Represent this sentence for searching relevant passages: "
    return "", ""


def _normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class KnowledgeBase:
    def __init__(self, folder: Path, client: Ollama, embed_model: str):
        self.folder = Path(folder)
        self.files = self.folder / "files"
        self.client = client
        self.embed_model = embed_model
        self.lock = threading.RLock()
        self.docs: list[dict] = []
        self.chunks: list[dict] = []  # {"doc": name, "text": ...}
        self.vectors: list[array] = []
        self.index_model = ""
        self._mtime = None
        self.load()

    # ------------------------------------------------------------- persistence
    @property
    def index_file(self) -> Path:
        return self.folder / "index.json"

    @property
    def vector_file(self) -> Path:
        return self.folder / "vectors.bin"

    def load(self) -> None:
        with self.lock:
            self.docs, self.chunks, self.vectors, self.index_model = [], [], [], ""
            if not self.index_file.exists():
                self._mtime = None
                return
            try:
                meta = json.loads(self.index_file.read_text(encoding="utf-8"))
                raw = array("f")
                raw.frombytes(self.vector_file.read_bytes())
            except (OSError, ValueError):
                return
            dim = int(meta.get("dim") or 0)
            chunks = meta.get("chunks", [])
            if not dim or len(raw) != dim * len(chunks):
                return  # corrupt / half-written: treat as empty, a rebuild fixes it
            self.docs, self.chunks = meta.get("docs", []), chunks
            self.index_model = meta.get("embed_model", "")
            self.vectors = [raw[i * dim:(i + 1) * dim] for i in range(len(chunks))]
            self._mtime = self.index_file.stat().st_mtime

    def reload_if_changed(self) -> None:
        """Another process (the MCP server, a teammate on a shared drive) may have updated the index."""
        try:
            mtime = self.index_file.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self.load()

    def save(self) -> None:
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            dim = len(self.vectors[0]) if self.vectors else 0
            raw = array("f")
            for v in self.vectors:
                raw.extend(v)
            tmp_vec, tmp_idx = self.vector_file.with_suffix(".tmp"), self.index_file.with_suffix(".tmp")
            tmp_vec.write_bytes(raw.tobytes())
            tmp_idx.write_text(json.dumps({"embed_model": self.embed_model, "dim": dim, "docs": self.docs,
                                           "chunks": self.chunks}, ensure_ascii=False), encoding="utf-8")
            tmp_vec.replace(self.vector_file)
            tmp_idx.replace(self.index_file)
            self.index_model = self.embed_model
            self._mtime = self.index_file.stat().st_mtime

    # ----------------------------------------------------------------- editing
    def documents(self) -> list[dict]:
        return list(self.docs)

    @property
    def needs_rebuild(self) -> bool:
        """The index was built with a different embedding model than the one now selected."""
        return bool(self.chunks) and self.index_model != self.embed_model

    def add(self, name: str, data: bytes) -> int:
        """Add (or replace) a document. Returns the number of chunks."""
        name = Path(name).name
        text = extract_text(name, data)
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError(f"No text found in '{name}' (scanned PDFs need OCR first).")
        doc_prefix, _ = _prefixes(self.embed_model)
        vectors = self.client.embed(self.embed_model, [doc_prefix + f"{name}\n{c}" for c in chunks])
        with self.lock:
            self.reload_if_changed()
            if self.needs_rebuild:
                raise OllamaError(f"The knowledge base was built with '{self.index_model}' but the embedding model "
                                  f"is now '{self.embed_model}'. Click Rebuild first.")
            self._drop(name)
            self.files.mkdir(parents=True, exist_ok=True)
            (self.files / name).write_bytes(data)
            self.docs.append({"name": name, "chunks": len(chunks), "chars": len(text),
                              "added": datetime.now().isoformat(timespec="seconds")})
            self.chunks += [{"doc": name, "text": c} for c in chunks]
            self.vectors += [array("f", _normalise(v)) for v in vectors]
            self.save()
        return len(chunks)

    def remove(self, name: str) -> None:
        with self.lock:
            self.reload_if_changed()
            self._drop(name)
            (self.files / name).unlink(missing_ok=True)
            self.save()

    def _drop(self, name: str) -> None:
        keep = [i for i, c in enumerate(self.chunks) if c["doc"] != name]
        self.chunks = [self.chunks[i] for i in keep]
        self.vectors = [self.vectors[i] for i in keep]
        self.docs = [d for d in self.docs if d["name"] != name]

    def rebuild(self, progress=None) -> int:
        """Re-read every stored file with the current embedding model."""
        with self.lock:
            files = sorted(p for p in self.files.glob("*") if p.is_file()) if self.files.exists() else []
            self.docs, self.chunks, self.vectors = [], [], []
            self.save()
            for i, p in enumerate(files):
                if progress:
                    progress(p.name, i / max(len(files), 1))
                self.add(p.name, p.read_bytes())
            return len(self.chunks)

    def clear(self) -> None:
        with self.lock:
            shutil.rmtree(self.files, ignore_errors=True)
            self.docs, self.chunks, self.vectors = [], [], []
            self.save()

    # ------------------------------------------------------------------ search
    def search(self, query: str, k: int = 5, min_score: float = 0.45) -> list[dict]:
        self.reload_if_changed()
        if not self.chunks or not query.strip():
            return []
        if self.needs_rebuild:
            raise OllamaError(f"The knowledge base was built with '{self.index_model}', not '{self.embed_model}'. "
                              "Open the Assistant tab and click Rebuild.")
        _, query_prefix = _prefixes(self.embed_model)
        q = _normalise(self.client.embed(self.embed_model, [query_prefix + query])[0])
        with self.lock:
            scored = sorted(((sum(map(operator.mul, q, v)), i) for i, v in enumerate(self.vectors)), reverse=True)
            return [{"source": self.chunks[i]["doc"], "score": round(s, 3), "text": self.chunks[i]["text"]}
                    for s, i in scored[:k] if s >= min_score]


_cache: dict[tuple, KnowledgeBase] = {}


def open_kb(settings) -> KnowledgeBase:
    """Shared instance per (folder, url, model) so the index is only read from disk when it changes."""
    key = (str(settings.knowledge_path), settings.ollama_url, settings.ollama_embed_model)
    if key not in _cache:
        _cache[key] = KnowledgeBase(settings.knowledge_path, Ollama(settings.ollama_url), settings.ollama_embed_model)
    return _cache[key]
