"""Tests for the Trello -> Todoist page service, with a stand-in script (no API calls).
Run: python trello-sync/test_app.py"""
import http.client, json, os, shutil, sys, tempfile, threading, time

tmp = tempfile.mkdtemp()
fake = os.path.join(tmp, "fake_sync.py")
with open(fake, "w") as fh:
    fh.write(
        "import os, sys\n"
        "print('dry' if '--dry-run' in sys.argv else 'real')\n"
        "print('key=' + os.environ.get('TRELLO_KEY', ''))\n"
        "print('state=' + os.environ.get('TTS_STATE_FILE', ''))\n"
        "if os.environ.get('TODOIST_TOKEN') == 'make-it-fail-please': sys.exit('boom')\n"
        "print('Done. Synced 2 new task(s), removed 0, updated 0 due date(s).')\n")
os.environ.update(TTS_DATA_DIR=os.path.join(tmp, "data"), TTS_SCRIPT=fake, TTS_ALLOW_ANY_ORIGIN="0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

FAILS = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond: FAILS.append(name)

server = app.make_server("127.0.0.1:0")
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c.request(method, "/trello-sync" + path, body=json.dumps(body) if isinstance(body, dict) else body, headers=h)
    r = c.getresponse(); raw = r.read()
    try: return r.status, json.loads(raw)
    except ValueError: return r.status, raw.decode()

def wait_idle():
    for _ in range(100):
        st, j = req("GET", "/api/status")
        if not j["running"]: return j
        time.sleep(0.1)
    return j

st, page = req("GET", "/")
check("page served", st == 200 and "Trello → Todoist" in page)
st, j = req("GET", "/api/status")
check("fresh status", st == 200 and j["schedule"] == "off" and not j["state_exists"] and j["secrets"] == {"TRELLO_KEY": False, "TRELLO_TOKEN": False, "TODOIST_TOKEN": False})

st, j = req("POST", "/api/secrets", {"TRELLO_KEY": "  key-1234567890  ", "TODOIST_TOKEN": ""})
check("save one secret, trimmed, empty ignored", st == 200 and j["secrets"]["TRELLO_KEY"] and not j["secrets"]["TODOIST_TOKEN"])
check("status never returns secret values", "key-1234567890" not in json.dumps(j))

st, j = req("POST", "/api/run", {"dry_run": False})
check("real sync refused before import", st == 409 and j["error"] == "no_state")
st, j = req("POST", "/api/run", {"dry_run": True})
check("preview starts", st == 202)
j = wait_idle()
run = j["runs"][0]
check("preview recorded ok with summary", run["ok"] and run["dry_run"] and run["summary"].startswith("Done. Synced 2"))
st, full = req("GET", "/api/run/" + run["id"])
check("output kept, token hidden", "dry" in full["output"] and "key=<hidden>" in full["output"] and "key-1234567890" not in full["output"])
check("script gets state path in data dir", "state=" + os.path.join(tmp, "data", "synced_cards.json") in full["output"])

st, j = req("POST", "/api/import-state", {"state": {"nope": 1}})
check("bad state refused", st == 400)
pc = {"synced_cards": {"c1": {"task_id": "t1", "due": None}, "c2": {"task_id": "t2", "due": "2026-11-20T10:00:00Z"}}}
st, j = req("POST", "/api/import-state", {"state": pc})
check("import counts cards", st == 200 and j["cards_remembered"] == 2 and j["state_exists"])
with open(os.path.join(tmp, "data", "synced_cards.json")) as fh:
    check("imported file identical", json.load(fh) == pc)
st, j = req("POST", "/api/import-state", {"state": {"synced_card_ids": ["a", "b", "c"]}})
check("old list format accepted; previous kept as backup", j["cards_remembered"] == 3 and os.path.exists(os.path.join(tmp, "data", "synced_cards.before-import.json")))

st, j = req("POST", "/api/run", {"dry_run": False})
check("real sync allowed after import", st == 202)
j = wait_idle()
check("real run recorded", not j["runs"][0]["dry_run"] and j["runs"][0]["ok"])

req("POST", "/api/secrets", {"TODOIST_TOKEN": "make-it-fail-please"})
req("POST", "/api/run", {"dry_run": True}); j = wait_idle()
check("failed run marked failed", j["runs"][0]["ok"] is False and "boom" in j["runs"][0]["summary"])
st, full = req("GET", "/api/run/" + j["runs"][0]["id"])
check("failed output hides that token too", "make-it-fail-please" not in full["output"])

st, j = req("POST", "/api/schedule", {"schedule": "1h"})
check("schedule set with next run about an hour away", st == 200 and j["schedule"] == "1h" and 3500 < j["next_run"] - time.time() < 3700)
st, j = req("POST", "/api/schedule", {"schedule": "daily"})
nxt = time.localtime(j["next_run"])
check("daily next run at 08:00 within a day", (nxt.tm_hour, nxt.tm_min) == (8, 0) and 0 < j["next_run"] - time.time() <= 86400)
st, j = req("POST", "/api/schedule", {"schedule": "5s"})
check("unknown schedule refused", st == 400)
s = app.settings(); s.update(schedule="15m", schedule_set=0, last_auto=time.time() - 16 * 60)
check("15m schedule due after 15 minutes", app.next_due(s, s["last_auto"]) <= time.time())

st, _ = req("POST", "/api/run", "dry_run=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
check("form posts refused", st == 403)
st, _ = req("POST", "/api/run", {"dry_run": True}, headers={"Origin": "https://evil.test"})
check("other origin refused", st == 403)
st, _ = req("GET", "/../etc/passwd")
check("unknown path 404", st == 404)

server.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
print(); print("all checks passed" if not FAILS else "%d FAILED" % len(FAILS)); sys.exit(1 if FAILS else 0)
