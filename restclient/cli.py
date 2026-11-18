"""Command-line access for admins / scheduled jobs.

  python -m restclient.cli list
  python -m restclient.cli send "Schoology API" "Users/Get user" --env "Schoology - Example District" --var user_id=123
  python -m restclient.cli run "OneRoster 1.1" "Find user by identifier" --csv ids.csv --env "PS ..." --expect 200
"""

from __future__ import annotations

import argparse
import sys

from . import engine, runner
from .workspace import Workspace


def _vars(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="restclient", description="Schoology API Client (command line)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list collections, requests and environments")

    s = sub.add_parser("send", help="send one saved request")
    s.add_argument("collection")
    s.add_argument("request", help="name or Folder/Name")
    s.add_argument("--env", help="environment name")
    s.add_argument("--var", action="append", default=[], help="name=value (repeatable)")
    s.add_argument("--all-pages", action="store_true", help="follow OneRoster paging")

    r = sub.add_parser("run", help="bulk run request(s) once per CSV row")
    r.add_argument("collection")
    r.add_argument("requests", nargs="+", help="request name(s), run in order for each row")
    r.add_argument("--csv", required=True)
    r.add_argument("--env")
    r.add_argument("--expect", default="", help='expected status, e.g. "200" or "2xx"')
    r.add_argument("--extract", default="", help='e.g. "id=id, name=name_display"')
    r.add_argument("--delay", type=int, default=0, help="milliseconds between requests")
    r.add_argument("--stop-on-error", action="store_true")
    r.add_argument("--out", help="results CSV path (default: results folder)")

    args = ap.parse_args(argv)
    ws = Workspace()

    if args.cmd == "list":
        for c in ws.collections:
            print(f"[{c.name}]  ({c.path})")
            for trail, req in c.walk():
                print(f"   {req.method:7} {'/'.join(trail + [req.name])}")
        print("\nEnvironments:")
        for e in ws.environments:
            print(f"   {e.name}")
        return 0

    c = ws.collection(args.collection)
    env = ws.environment(args.env)

    if args.cmd == "send":
        req = c.find_request(args.request)
        if req is None:
            print(f"No request '{args.request}' in {c.name}", file=sys.stderr)
            return 2
        rr, res = engine.execute(req, c, env, ws.settings, extra=_vars(args.var),
                                 all_pages=args.all_pages or None)
        print(rr.describe(), file=sys.stderr)
        print(res.summary() + (f"  {res.note}" if res.note else ""), file=sys.stderr)
        print(res.pretty_body())
        return 0 if res.ok else 1

    reqs = []
    for name in args.requests:
        req = c.find_request(name)
        if req is None:
            print(f"No request '{name}' in {c.name}", file=sys.stderr)
            return 2
        reqs.append(req)
    _, rows = runner.read_csv_file(args.csv)
    opts = runner.RunOptions(delay_ms=args.delay, expected_status=args.expect, extract=args.extract,
                             stop_on_error=args.stop_on_error)

    def progress(step: runner.StepResult):
        mark = "PASS" if step.passed else "FAIL"
        print(f"row {step.row:>5}  {mark}  {step.status or '-'}  {step.request}  {step.error[:120]}", file=sys.stderr)

    results = runner.run(c, reqs, env, rows, opts, ws.settings, on_result=progress)
    if args.out:
        with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(runner.results_to_csv(results))
        out = args.out
    else:
        out = runner.save_results(results, ws.settings, c.name)
    failed = sum(1 for x in results if not x.passed)
    print(f"\n{len(results) - failed} passed, {failed} failed. Results: {out}", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
