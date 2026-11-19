#!/usr/bin/env python3
"""Trello -> Todoist sync page for the VPS.

A small web page at /trello-sync/ (behind the admin login in nginx) around
trello_to_todoist.py, which runs unchanged as a subprocess:

- Sync now / Preview changes (--dry-run), with the output kept per run
- Automatic sync: off, every 15 minutes, hourly, or daily at 08:00 (TZ)
- Connections: the Trello key/token and Todoist token, typed into the page
  once, stored in DATA_DIR/secrets.json (mode 600), never sent back
- Import the synced_cards.json the script kept on the PC, so cards already
  turned into tasks are not created again

Standard library only (the script itself needs `requests`, from its venv).
Listens on 127.0.0.1 only; nginx adds the login.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("TTS_SCRIPT") or os.path.join(HERE, "trello_to_todoist.py")
PAGE_FILE = os.path.join(HERE, "page.html")

DATA_DIR = os.environ.get("TTS_DATA_DIR") or os.environ.get("STATE_DIRECTORY") or "/var/lib/trello-sync"
PYTHON = os.environ.get("TTS_PYTHON", sys.executable)
LISTEN = os.environ.get("TTS_LISTEN", "127.0.0.1:8620")
PREFIX = "/trello-sync"
MAIN_ORIGIN = "https://" + os.environ.get("TTS_MAIN_HOST", "107-173-7-177.sslip.io")
ALLOW_ANY_ORIGIN = os.environ.get("TTS_ALLOW_ANY_ORIGIN") == "1"  # tests only

RUN_TIMEOUT = 600
KEEP_RUNS = 40
SCHEDULES = {"off": None, "15m": 15 * 60, "1h": 3600, "daily": "08:00"}
SECRET_NAMES = ("TRELLO_KEY", "TRELLO_TOKEN", "TODOIST_TOKEN")


def path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def now() -> float:
    return time.time()


def read_json(name: str, default):
    try:
        with open(path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(name: str, data, private: bool = False) -> None:
    tmp = path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path(name))


def log(msg: str) -> None:
    sys.stderr.write("trello-sync: %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Runs


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.running: Optional[Dict] = None

    def runs(self) -> List[Dict]:
        return read_json("runs.json", [])

    def start(self, dry_run: bool, trigger: str) -> Optional[Dict]:
        """Start a run in the background; None if one is already going."""
        with self.lock:
            if self.running:
                return None
            run = {"id": "%d" % (now() * 1000), "dry_run": dry_run, "trigger": trigger,
                   "started": now(), "finished": None, "ok": None, "summary": "Running…"}
            self.running = run
        threading.Thread(target=self._run, args=(run,), daemon=True).start()
        return run

    def _run(self, run: Dict) -> None:
        secrets = read_json("secrets.json", {})
        env = {k: v for k, v in os.environ.items() if k not in SECRET_NAMES}
        env.update({k: secrets.get(k, "") for k in SECRET_NAMES})
        env["TTS_STATE_FILE"] = path("synced_cards.json")
        env["PYTHONUNBUFFERED"] = "1"
        cmd = [PYTHON, SCRIPT] + (["--dry-run"] if run["dry_run"] else [])
        try:
            proc = subprocess.run(cmd, env=env, cwd=DATA_DIR, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT)
            output = proc.stdout.decode("utf-8", "replace")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired as ex:
            output = (ex.stdout or b"").decode("utf-8", "replace") + "\n\nStopped: took longer than %d minutes." % (RUN_TIMEOUT // 60)
            ok = False
        except OSError as ex:
            output, ok = "Could not start the script: %s" % ex, False
        # Never keep a token in the log, whatever the script printed.
        for value in secrets.values():
            if isinstance(value, str) and len(value) >= 8:
                output = output.replace(value, "<hidden>")
        lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        summary = lines[-1] if lines else ("Finished." if ok else "Failed with no output.")
        if not ok and lines and "Traceback" in output:
            summary = "Failed: " + lines[-1]
        run.update(finished=now(), ok=ok, summary=summary[:300], output=output[-50000:])
        with self.lock:
            runs = self.runs()
            runs.insert(0, run)
            write_json("runs.json", runs[:KEEP_RUNS])
            self.running = None
        log("%s run (%s) %s: %s" % ("preview" if run["dry_run"] else "sync", run["trigger"],
                                    "ok" if ok else "FAILED", summary))


RUNNER = Runner()


# --------------------------------------------------------------------------
# Schedule


def settings() -> Dict:
    s = read_json("settings.json", {})
    if s.get("schedule") not in SCHEDULES:
        s["schedule"] = "off"
    return s


def next_due(s: Dict, last_auto: float) -> Optional[float]:
    sched = SCHEDULES.get(s["schedule"])
    if sched is None:
        return None
    if isinstance(sched, int):
        return max(last_auto + sched, s.get("schedule_set", 0) + 60)
    hour, minute = (int(x) for x in sched.split(":"))
    base = dt.datetime.fromtimestamp(max(last_auto, s.get("schedule_set", 0)))
    due = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= base:
        due += dt.timedelta(days=1)
    return due.timestamp()


def scheduler() -> None:
    while True:
        try:
            s = settings()
            due = next_due(s, s.get("last_auto", 0))
            if due is not None and now() >= due:
                if RUNNER.start(dry_run=False, trigger="schedule"):
                    s["last_auto"] = now()
                    write_json("settings.json", s)
        except Exception as ex:  # keep the scheduler alive whatever happens
            log("scheduler error: %s" % ex)
        time.sleep(20)


# --------------------------------------------------------------------------
# State import


def validate_state(data) -> int:
    """Accept the PC's synced_cards.json (any format the script reads); return card count."""
    if not isinstance(data, dict):
        raise ValueError("This isn't a synced_cards.json file.")
    cards = data.get("synced_cards", data.get("synced_card_ids"))
    if isinstance(cards, list):
        if not all(isinstance(c, str) for c in cards):
            raise ValueError("Unexpected card list.")
        return len(cards)
    if isinstance(cards, dict):
        return len(cards)
    raise ValueError("No synced_cards found in this file.")


# --------------------------------------------------------------------------
# HTTP


def status() -> Dict:
    s = settings()
    secrets = read_json("secrets.json", {})
    state = read_json("synced_cards.json", None)
    try:
        remembered = validate_state(state) if state is not None else 0
    except ValueError:
        remembered = 0
    runs = RUNNER.runs()
    with RUNNER.lock:
        running = dict(RUNNER.running) if RUNNER.running else None
    due = next_due(s, s.get("last_auto", 0))
    return {
        "running": running,
        "runs": [{k: r.get(k) for k in ("id", "dry_run", "trigger", "started", "finished", "ok", "summary")} for r in runs],
        "schedule": s["schedule"],
        "next_run": due,
        "secrets": {k: bool(secrets.get(k)) for k in SECRET_NAMES},
        "cards_remembered": remembered,
        "state_exists": state is not None,
        "imported_at": s.get("imported_at"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "trello-sync"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def _send(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _route(self) -> str:
        p = self.path.split("?", 1)[0]
        return p[len(PREFIX):] if p.startswith(PREFIX + "/") else ""

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        r = self._route()
        if r == "/":
            with open(PAGE_FILE, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if r == "/api/status":
            return self._json(200, status())
        if r.startswith("/api/run/"):
            run_id = r[len("/api/run/"):]
            for run in RUNNER.runs():
                if run.get("id") == run_id:
                    return self._json(200, run)
            return self._json(404, {"error": "No such run."})
        return self._json(404, {"error": "Not found."})

    def do_POST(self):  # noqa: N802
        r = self._route()
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        origin = self.headers.get("Origin")
        if ctype != "application/json" or (origin and origin != MAIN_ORIGIN and not ALLOW_ANY_ORIGIN):
            return self._json(403, {"error": "Refused."})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Bad request."})

        if r == "/api/run":
            dry = bool(body.get("dry_run"))
            if not dry and not read_json("synced_cards.json", None) and not body.get("confirm_fresh"):
                return self._json(409, {"error": "no_state"})
            run = RUNNER.start(dry_run=dry, trigger="page")
            if not run:
                return self._json(409, {"error": "A run is already going."})
            return self._json(202, {"id": run["id"]})

        if r == "/api/schedule":
            sched = body.get("schedule")
            if sched not in SCHEDULES:
                return self._json(400, {"error": "Unknown schedule."})
            s = settings()
            s.update(schedule=sched, schedule_set=now(), last_auto=now())
            write_json("settings.json", s)
            return self._json(200, status())

        if r == "/api/secrets":
            secrets = read_json("secrets.json", {})
            for k in SECRET_NAMES:
                v = body.get(k)
                if isinstance(v, str) and v.strip():
                    if len(v.strip()) > 300:
                        return self._json(400, {"error": "%s looks too long." % k})
                    secrets[k] = v.strip()
            write_json("secrets.json", secrets, private=True)
            return self._json(200, status())

        if r == "/api/import-state":
            data = body.get("state")
            try:
                count = validate_state(data)
            except ValueError as ex:
                return self._json(400, {"error": str(ex)})
            if os.path.exists(path("synced_cards.json")):
                os.replace(path("synced_cards.json"), path("synced_cards.before-import.json"))
            write_json("synced_cards.json", data)
            s = settings()
            s["imported_at"] = now()
            write_json("settings.json", s)
            log("imported state with %d cards" % count)
            return self._json(200, status())

        return self._json(404, {"error": "Not found."})


def make_server(listen: str) -> ThreadingHTTPServer:
    os.makedirs(DATA_DIR, exist_ok=True)
    host, port = listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    server = make_server(LISTEN)
    threading.Thread(target=scheduler, daemon=True).start()
    log("listening on %s, data in %s" % (LISTEN, DATA_DIR))
    server.serve_forever()


if __name__ == "__main__":
    main()
