"""Bulk runner: run one or more requests once per CSV row.

* Every CSV column becomes a {{variable}} (e.g. a column "user_id" fills {{user_id}}).
* Several requests can be chained per row; values extracted from one response
  (e.g. id=data.id) become variables for the next request in the same row.
* Results can be written to a CSV that opens cleanly in Excel.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import redact
from .config import Settings
from .engine import execute, new_session
from .models import Collection, Environment, Request


@dataclass
class RunOptions:
    delay_ms: int = 0
    expected_status: str = ""  # "", "200", "2xx", "200,201,204"
    extract: str = ""  # "name=path, other=data.items[0].id"
    stop_on_error: bool = False
    retry_on_429: bool = True
    max_retries: int = 3


@dataclass
class StepResult:
    row: int
    request: str
    method: str
    url: str
    status: int | None
    passed: bool
    elapsed_ms: int
    error: str = ""
    extracted: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    response: str = ""

    def as_table_row(self) -> dict:
        return {
            "id": f"{self.row}-{self.request}",
            "row": self.row,
            "request": self.request,
            "status": self.status if self.status is not None else "-",
            "result": "PASS" if self.passed else "FAIL",
            "time": self.elapsed_ms,
            "details": self.error or ", ".join(f"{k}={v}" for k, v in self.extracted.items()),
            "inputs": ", ".join(f"{k}={v}" for k, v in self.inputs.items()),
        }


# ---------------------------------------------------------------- CSV helpers ---------
def read_csv_text(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")  # Excel's UTF-8 BOM
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = [c.strip() for c in (reader.fieldnames or []) if c is not None]
    rows = []
    for raw in reader:
        row = {str(k).strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if any(row.values()):  # skip blank lines
            rows.append(row)
    return columns, rows


def read_csv_bytes(data: bytes) -> tuple[list[str], list[dict]]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return read_csv_text(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return read_csv_text(data.decode("utf-8", errors="replace"))


def read_csv_file(path: str | Path) -> tuple[list[str], list[dict]]:
    return read_csv_bytes(Path(path).read_bytes())


# ---------------------------------------------------------- response helpers ---------
def parse_extract(spec: str) -> list[tuple[str, str]]:
    """'id=data.id, name=users[0].name_display, status' -> [(id, data.id), ...]"""
    out = []
    for part in re.split(r"[,\n;]", spec or ""):
        part = part.strip()
        if not part:
            continue
        name, _, path = part.partition("=")
        name, path = name.strip(), (path.strip() or name.strip())
        out.append((name, path))
    return out


def get_path(data, path: str):
    """Very small JSON path: 'a.b[0].c', 'users.0.id', '$.a.b'. Returns None if missing."""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:].lstrip(".")
    tokens = [t for t in re.split(r"\.|\[|\]", path) if t != ""]
    cur = data
    for tok in tokens:
        if isinstance(cur, list):
            if not re.fullmatch(r"-?\d+", tok):
                return None
            idx = int(tok)
            if not -len(cur) <= idx < len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if tok not in cur:
                return None
            cur = cur[tok]
        else:
            return None
    return cur


def status_matches(status: int | None, expected: str) -> bool:
    if status is None:
        return False
    expected = (expected or "").strip()
    if not expected:
        return status < 400
    for part in re.split(r"[,\s]+", expected):
        part = part.strip().lower()
        if not part:
            continue
        if re.fullmatch(r"\dxx", part) and str(status)[0] == part[0]:
            return True
        if part.isdigit() and int(part) == status:
            return True
    return False


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------- the runner ---------
def run(collection: Collection, requests_to_run: list[Request], environment: Environment | None,
        rows: list[dict], options: RunOptions, settings: Settings,
        on_result: Callable[[StepResult], None] | None = None,
        cancel: threading.Event | None = None) -> list[StepResult]:
    extract = parse_extract(options.extract)
    session = new_session()
    results: list[StepResult] = []
    secrets = environment.secret_values() if environment else []
    stop = False

    for row_no, row in enumerate(rows, start=1):
        if stop or (cancel and cancel.is_set()):
            break
        variables = dict(row)
        for req in requests_to_run:
            if cancel and cancel.is_set():
                stop = True
                break
            attempt = 0
            while True:
                rr, res = execute(req, collection, environment, settings, extra=variables, session=session)
                if res.status == 429 and options.retry_on_429 and attempt < options.max_retries:
                    attempt += 1
                    wait = res.headers.get("Retry-After") or res.headers.get("retry-after")
                    try:
                        seconds = float(wait) if wait else 2 ** attempt
                    except ValueError:
                        seconds = 2 ** attempt
                    time.sleep(min(seconds, 60))
                    continue
                break

            extracted = {}
            data = res.json()
            for name, path in extract:
                value = get_path(data, path) if data is not None else None
                if value is not None:
                    extracted[name] = _as_text(value)
            variables.update(extracted)  # available to the next request in this row

            passed = res.error is None and status_matches(res.status, options.expected_status)
            error = res.error or ("" if passed else f"Expected {options.expected_status or 'success'}, got {res.status}")
            if not passed and res.text and not res.error:
                error += f": {res.text[:200]}"
            # results are shown, downloaded and saved to a CSV: never let a key, secret or token into them
            hide = secrets + rr.auth.secret_values()
            step = StepResult(
                row=row_no, request=req.name, method=rr.method, url=redact.redact(res.url or rr.url, hide),
                status=res.status, passed=passed, elapsed_ms=res.elapsed_ms, error=redact.redact(error, hide),
                extracted={k: redact.redact(v, hide) for k, v in extracted.items()}, inputs=row,
                response=redact.redact(res.text[:2000], hide),
            )
            results.append(step)
            if on_result:
                on_result(step)
            if options.delay_ms:
                time.sleep(options.delay_ms / 1000)
            if not passed:
                if options.stop_on_error:
                    stop = True
                break  # later requests in this row usually depend on this one
    return results


def results_to_csv(results: list[StepResult]) -> str:
    extracted_cols: list[str] = []
    input_cols: list[str] = []
    for r in results:
        extracted_cols += [k for k in r.extracted if k not in extracted_cols]
        input_cols += [k for k in r.inputs if k not in input_cols]
    header = ["row", "request", "method", "url", "status", "result", "time_ms", "error"] + \
             extracted_cols + [f"input:{c}" for c in input_cols] + ["response"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in results:
        writer.writerow([r.row, r.request, r.method, r.url, r.status if r.status is not None else "",
                         "PASS" if r.passed else "FAIL", r.elapsed_ms, r.error]
                        + [r.extracted.get(c, "") for c in extracted_cols]
                        + [r.inputs.get(c, "") for c in input_cols]
                        + [r.response[:1000].replace("\r", " ").replace("\n", " ")])
    return buf.getvalue()


def save_results(results: list[StepResult], settings: Settings, label: str = "run") -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:60] or "run"
    path = settings.results_path / f"{datetime.now():%Y-%m-%d_%H%M%S}_{safe}.csv"
    path.write_text(results_to_csv(results), encoding="utf-8-sig", newline="")
    return path
