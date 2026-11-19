"""Checks trello_to_todoist.py's re-check with Trello and Todoist faked (no API calls).
Run: python3 trello-sync/test_script.py"""
import sys, types, importlib
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("requests", types.SimpleNamespace(HTTPError=Exception, get=None, post=None, delete=None))
t = importlib.import_module("trello_to_todoist")
cards = {
  "removed":  {"idMembers": ["someone"], "idList": "L1"},
  "archived": {"idMembers": ["me"], "idList": "L1", "closed": True},
  "complete": {"idMembers": ["me"], "idList": "L1", "dueComplete": True},
  "donelist": {"idMembers": ["me"], "idList": "L2"},
  "mine":     {"idMembers": ["me"], "idList": "L1"},
}
asked = []
def fake_trello_get(path, params=None):
    asked.append((params or {}).get("fields", ""))
    if path.startswith("/lists/"): return {"name": "Done" if path.endswith("L2") else "Doing"}
    return cards[path.split("/")[2]]
deleted = []
t.trello_get = fake_trello_get
t.delete_todoist_task = lambda tid, dry, why: deleted.append((tid, why))
state = {"synced_cards": {k: {"task_id": "T-" + k, "due": None} for k in cards}}
removed, _ = t.cleanup_stale_tasks(state, "me", dry_run=False)
reasons = dict(deleted)
ok = True
for k, want in [("removed", "you were removed"), ("archived", "archived"), ("complete", "complete"), ("donelist", "done list")]:
    good = want in reasons.get("T-" + k, "")
    ok &= good; print(("PASS  " if good else "FAIL  ") + k + " -> " + reasons.get("T-" + k, "kept"))
good = "T-mine" not in reasons and list(state["synced_cards"]) == ["mine"]; ok &= good
print(("PASS  " if good else "FAIL  ") + "own card kept, others dropped from state")
good = all("closed" in f for f in asked if "idMembers" in f); ok &= good
print(("PASS  " if good else "FAIL  ") + "card re-check asks Trello for 'closed'")
print("all checks passed" if ok else "FAILED")
