#!/usr/bin/env python3
"""
trello_to_todoist.py

Pulls all open (not archived, not marked complete, not in a "done"-style
list) cards from every Trello board you belong to that are assigned to you
specifically, and creates matching tasks in Todoist, including the exact
due date and time if the card has one.

Each run also re-checks cards it synced previously:
  - If you've since been removed from the card, the card was
    archived/deleted, or the card was marked complete, the matching
    Todoist task is automatically deleted. If a card is later un-marked
    as complete (and you're still on it), it will be picked up again on
    the next run as if it were new.
  - If the due date/time on the Trello card changes, the Todoist task's
    due date is updated to match. If the due date is removed entirely on
    the Trello card, it's removed from the Todoist task too.

SETUP
-----
1. Install dependency:
   pip install requests --break-system-packages   (or just `pip install requests`)

2. Get Trello credentials:
   - Go to https://trello.com/power-ups/admin
   - Create a Power-Up (any name), open "API key" tab -> copy your API key
   - On that same page, generate a token (click the "Token" link) -> copy it
   - Trello key/token generation is free, no paid plan required.

3. Get a Todoist API token:
   - Todoist web app -> Settings -> Integrations -> Developer -> copy "API token"

4. Set the three values as environment variables (recommended) or edit the
   CONFIG block below directly:

   export TRELLO_KEY="..."
   export TRELLO_TOKEN="..."
   export TODOIST_TOKEN="..."

5. Run it:
   python3 trello_to_todoist.py

   Add --dry-run to preview what would change without touching Todoist.

6. (Optional) Run on a schedule with cron, e.g. every morning at 8am:
   0 8 * * * /usr/bin/python3 /path/to/trello_to_todoist.py >> /path/to/sync.log 2>&1
"""

import os
import sys
import json
import argparse
import requests

# ----------------------------- CONFIG ---------------------------------

TRELLO_KEY = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN", "")
TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN", "")

# List names to skip when pulling cards (case-insensitive substring match).
# Cards sitting in a list whose name contains any of these are treated as
# already finished and won't be synced.
DONE_LIST_KEYWORDS = ["done", "complete", "finished", "archive"]

# Name of the Todoist project to dump synced tasks into.
# If it doesn't exist yet, the script creates it.
TODOIST_PROJECT_NAME = "VEIT"

# Where we remember which Trello cards have already been synced: maps
# card_id -> {"task_id": ..., "due": ...}
# On the VPS the web page sets TTS_STATE_FILE (outside the repo clone).
STATE_FILE = os.environ.get("TTS_STATE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "synced_cards.json")

TRELLO_API = "https://api.trello.com/1"
TODOIST_API = "https://api.todoist.com/api/v1"

# "closed" is needed so the re-check notices archived cards.
CARD_FIELDS = "name,due,dueComplete,url,idList,shortLink,idMembers,closed"

# ------------------------------------------------------------------------


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        # Migrate old formats to the current dict-of-dicts format.
        synced = data.get("synced_cards", data.get("synced_card_ids"))
        migrated = {}
        if isinstance(synced, list):
            for cid in synced:
                migrated[cid] = {"task_id": None, "due": None}
        elif isinstance(synced, dict):
            for cid, val in synced.items():
                if isinstance(val, dict):
                    migrated[cid] = val
                else:
                    migrated[cid] = {"task_id": val, "due": None}
        return {"synced_cards": migrated}
    return {"synced_cards": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def trello_get(path, params=None):
    params = params or {}
    params.update({"key": TRELLO_KEY, "token": TRELLO_TOKEN})
    resp = requests.get(f"{TRELLO_API}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_my_member_id():
    me = trello_get("/members/me", {"fields": "id"})
    return me["id"]


def get_boards():
    return trello_get("/members/me/boards", {"fields": "name,url,closed"})


def get_open_cards(board_id):
    return trello_get(
        f"/boards/{board_id}/cards",
        {"filter": "open", "fields": CARD_FIELDS},
    )


def get_card_status(card_id):
    """Fetch a single card's current state. Returns None if the card no
    longer exists at all."""
    try:
        return trello_get(f"/cards/{card_id}", {"fields": CARD_FIELDS})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def get_lists(board_id):
    lists = trello_get(f"/boards/{board_id}/lists", {"fields": "name"})
    return {l["id"]: l["name"] for l in lists}


def is_done_list(list_name):
    name = list_name.lower()
    return any(keyword in name for keyword in DONE_LIST_KEYWORDS)


def to_todoist_datetime(trello_due):
    """Convert Trello's due timestamp ('2026-07-15T14:00:00.000Z') into the
    RFC3339 UTC format Todoist expects ('2026-07-15T14:00:00Z')."""
    if not trello_due:
        return None
    # Strip milliseconds if present.
    cleaned = trello_due.replace(".000Z", "Z")
    if "." in cleaned and cleaned.endswith("Z"):
        cleaned = cleaned.split(".")[0] + "Z"
    return cleaned


def todoist_headers():
    return {"Authorization": f"Bearer {TODOIST_TOKEN}"}


def get_or_create_project():
    resp = requests.get(f"{TODOIST_API}/projects", headers=todoist_headers(), timeout=30)
    resp.raise_for_status()
    for project in resp.json()["results"]:
        if project["name"] == TODOIST_PROJECT_NAME:
            return project["id"]

    resp = requests.post(
        f"{TODOIST_API}/projects",
        headers=todoist_headers(),
        json={"name": TODOIST_PROJECT_NAME},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_todoist_task(project_id, card, board_name, list_name, dry_run):
    content = card["name"]
    description = f"From Trello board '{board_name}' (list: {list_name})\n{card['url']}"
    due_datetime = to_todoist_datetime(card.get("due"))

    if dry_run:
        print(f"  [DRY RUN] Would create: {content}" + (f" (due {due_datetime})" if due_datetime else ""))
        return None

    payload = {
        "content": content,
        "description": description,
        "project_id": project_id,
    }
    if due_datetime:
        payload["due_datetime"] = due_datetime

    resp = requests.post(f"{TODOIST_API}/tasks", headers=todoist_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    print(f"  Created: {content}")
    return resp.json()["id"]


def delete_todoist_task(task_id, dry_run, reason):
    if dry_run:
        print(f"  [DRY RUN] Would delete task {task_id} ({reason})")
        return
    if not task_id:
        return  # nothing to delete, e.g. migrated from old state format
    resp = requests.delete(f"{TODOIST_API}/tasks/{task_id}", headers=todoist_headers(), timeout=30)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()
    print(f"  Deleted task {task_id} ({reason})")


def update_todoist_task_due(task_id, due_datetime, dry_run):
    if dry_run:
        label = due_datetime if due_datetime else "no date"
        print(f"  [DRY RUN] Would update task {task_id} due date to: {label}")
        return
    if not task_id:
        return

    if due_datetime:
        payload = {"due_datetime": due_datetime}
    else:
        payload = {"due_string": "no date"}  # clears the due date

    resp = requests.post(f"{TODOIST_API}/tasks/{task_id}", headers=todoist_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    print(f"  Updated due date on task {task_id}")


_list_name_cache = {}


def get_list_name(list_id):
    """Look up a Trello list's name (cached for the duration of the run)."""
    if list_id not in _list_name_cache:
        try:
            _list_name_cache[list_id] = trello_get(f"/lists/{list_id}", {"fields": "name"})["name"]
        except requests.HTTPError:
            _list_name_cache[list_id] = ""
    return _list_name_cache[list_id]


def cleanup_stale_tasks(state, my_id, dry_run):
    """Re-check previously synced cards: remove tasks for cards you're no
    longer on / that are archived, complete, or moved into a "done"-style
    list, and keep due dates current for everything else."""
    stale_card_ids = []
    updated_count = 0

    for card_id, record in state["synced_cards"].items():
        task_id = record.get("task_id")
        card = get_card_status(card_id)

        if card is None:
            delete_todoist_task(task_id, dry_run, "card deleted")
            stale_card_ids.append(card_id)
            continue
        if card.get("closed"):
            delete_todoist_task(task_id, dry_run, "card archived")
            stale_card_ids.append(card_id)
            continue
        if card.get("dueComplete"):
            delete_todoist_task(task_id, dry_run, "card marked complete in Trello")
            stale_card_ids.append(card_id)
            continue
        if is_done_list(get_list_name(card.get("idList", ""))):
            delete_todoist_task(task_id, dry_run, "card moved to a done list")
            stale_card_ids.append(card_id)
            continue
        if my_id not in card.get("idMembers", []):
            delete_todoist_task(task_id, dry_run, "you were removed from the card")
            stale_card_ids.append(card_id)
            continue

        # Still valid — check whether the due date changed.
        current_due = to_todoist_datetime(card.get("due"))
        if current_due != record.get("due"):
            update_todoist_task_due(task_id, current_due, dry_run)
            if not dry_run:
                record["due"] = current_due
            updated_count += 1

    if stale_card_ids and not dry_run:
        for card_id in stale_card_ids:
            del state["synced_cards"][card_id]

    return len(stale_card_ids), updated_count


def main():
    parser = argparse.ArgumentParser(description="Sync open Trello cards into Todoist")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing Todoist")
    args = parser.parse_args()

    if not TRELLO_KEY or not TRELLO_TOKEN:
        sys.exit("Missing TRELLO_KEY / TRELLO_TOKEN. See setup instructions at the top of this file.")
    if not TODOIST_TOKEN and not args.dry_run:
        sys.exit("Missing TODOIST_TOKEN. See setup instructions at the top of this file.")

    state = load_state()

    project_id = None
    if not args.dry_run:
        project_id = get_or_create_project()

    my_id = get_my_member_id()

    print("Checking previously synced cards for changes...")
    removed_count, updated_count = cleanup_stale_tasks(state, my_id, args.dry_run)
    print(f"Removed {removed_count} stale task(s), updated {updated_count} due date(s).\n")

    boards = [b for b in get_boards() if not b.get("closed")]
    print(f"Found {len(boards)} open board(s).\n")

    new_count = 0
    for board in boards:
        list_names = get_lists(board["id"])
        cards = get_open_cards(board["id"])
        outstanding = [
            c for c in cards
            if not is_done_list(list_names.get(c["idList"], ""))
            and not c.get("dueComplete")
            and my_id in c.get("idMembers", [])
        ]

        if not outstanding:
            continue

        print(f"Board: {board['name']} ({len(outstanding)} outstanding card(s))")
        for card in outstanding:
            if card["id"] in state["synced_cards"]:
                continue
            list_name = list_names.get(card["idList"], "Unknown list")
            task_id = create_todoist_task(project_id, card, board["name"], list_name, args.dry_run)
            if not args.dry_run:
                state["synced_cards"][card["id"]] = {
                    "task_id": task_id,
                    "due": to_todoist_datetime(card.get("due")),
                }
            new_count += 1
        print()

    if not args.dry_run:
        save_state(state)

    if new_count == 0 and removed_count == 0 and updated_count == 0:
        print("No changes — everything's already in sync.")
    else:
        print(f"Done. Synced {new_count} new task(s), removed {removed_count}, updated {updated_count} due date(s).")


if __name__ == "__main__":
    main()
