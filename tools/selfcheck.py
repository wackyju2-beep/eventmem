#!/usr/bin/env python3
"""eventmem self-check — is the daemon up, does the database open, is the vector arm on?

    python tools/selfcheck.py [--url http://127.0.0.1:9123] [--db PATH] [--json]

Exit code 0 when the daemon answers, 1 otherwise. The encoder is reported, not required:
without it recall falls back to FTS5. Output is deliberately plain ASCII so it survives a
cmd.exe console with any code page.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from urllib.request import pathname2url

ROOT = Path(__file__).resolve().parents[1]


def http_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def local_db(path: str) -> dict:
    """What the database says when the daemon is not answering."""
    out: dict = {}
    try:
        con = sqlite3.connect("file:" + pathname2url(path) + "?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            out["schema"] = int(row[0]) if row else None
            out["events"] = con.execute("SELECT count(*) FROM events").fetchone()[0]
            out["edges"] = con.execute("SELECT count(*) FROM edges").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - a broken DB is the answer, not a crash
        out["db_error"] = f"{type(exc).__name__}: {exc}"
    return out


def encoder_status() -> dict:
    try:
        sys.path.insert(0, str(ROOT / "core"))
        from eventmem_core import embed

        enc = embed.Encoder()
        return {
            "available": bool(enc.available()),
            "reason": enc.reason(),
            "root": str(embed._models_root()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "root": ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report what this eventmem install can do.")
    parser.add_argument("--url", default=os.environ.get("EVENTMEM_URL", "http://127.0.0.1:9123"))
    parser.add_argument("--db", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    url = args.url.rstrip("/")
    report: dict = {"url": url, "daemon": False, "db": args.db, "backends": []}

    try:
        health = http_json(url + "/api/health")
        report["daemon"] = health.get("status") == "ok"
        report["db"] = health.get("db") or args.db
        report["schema"] = health.get("schema")
        report["uptime"] = health.get("uptime")
        report["backends"] = list(health.get("backends") or [])
        stats = http_json(url + "/api/stats")
        report["events"] = stats.get("events")
        report["edges"] = stats.get("edges")
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, OSError):
            report["daemon_error"] = "daemon not running"
        else:
            report["daemon_error"] = f"{type(exc).__name__}: {exc}"

    if not report["daemon"] and report["db"] and os.path.exists(report["db"]):
        report.update(local_db(report["db"]))

    report["encoder"] = encoder_status()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["daemon"] else 1

    if report["daemon"]:
        print(f"daemon    {url}  ok (schema {report.get('schema')})")
        print(f"db        {report.get('db')}  {report.get('events')} events / {report.get('edges')} edges")
        backends = report.get("backends") or []
        print("backends  " + (", ".join(backends) if backends else "none - relation edges need one (console -> Backends)"))
    else:
        print(f"daemon    {url}  DOWN ({report.get('daemon_error', 'no answer')})")
        if report.get("schema") is not None:
            print(f"db        {report.get('db')}  {report.get('events')} events / {report.get('edges')} edges (opened directly)")
        elif report.get("db_error"):
            print(f"db        {report.get('db')}  {report['db_error']}")

    enc = report["encoder"]
    if enc["available"]:
        print(f"encoder   ready ({enc['root']})")
    else:
        print(f"encoder   off - recall is FTS5 only ({enc['reason']})")
        if enc["root"]:
            print(f"          put tokenizer.json + model_int8.onnx in {enc['root']}")

    return 0 if report["daemon"] else 1


if __name__ == "__main__":
    sys.exit(main())
