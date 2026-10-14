"""
Economic calendar event reminders -> Telegram bot.

This is a SEPARATE script/workflow from the others in this repo — sends
its own message(s), on its own (frequent) schedule, to the same Telegram
bot.

What this does:
1. Fetches the same free public USD economic calendar feed used by
   weekly_market_prep.py (High/Medium impact events for the current
   week).
2. For each event, checks whether it starts in roughly 30-60 minutes
   from now.
3. If so, and we haven't already reminded about it (tracked in a state
   file committed back to the repo), sends a Telegram message.

HOW RELIABLE THIS IS (AND ISN'T):
GitHub documents the `schedule` trigger as best-effort: runs can be
delayed during high load ("high load times include the start of every
hour"), and "if the load is sufficiently high enough, some queued jobs
may be dropped". On 2026-10-14 this repo got 4 runs in ~14 hours instead
of ~28, and none fell inside the window before the 15:30 MSK CPI release,
so no reminder was sent. No window width can fix a run that never
happens, so this script now (a) is triggered every 10 minutes at minutes
away from :00/:30 to give GitHub more chances, (b) sends TWO reminders
for High events (about an hour before, and a last call ~15 minutes
before) so one dropped run doesn't lose the event, and (c) groups events
that start at the same time (e.g. the four CPI lines) into one message.
It is still best-effort: for anything you can't afford to miss, keep the
event in the weekly report's calendar too.

Requirements (installed automatically by the GitHub Actions workflow):
    pip install requests python-dateutil

Environment variables required:
    TELEGRAM_BOT_TOKEN   - same one already used by the other scripts
    TELEGRAM_CHAT_ID     - same one already used by the other scripts

NOTE: this script keeps a small state file (reminded_events.json) so it
doesn't send the same event's reminder twice across multiple runs whose
windows overlap. Old entries are pruned automatically.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dateutil import parser as date_parser

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = "reminded_events.json"

# Each stage reminds when an event starts between "min" and "max" minutes
# from now, once per event. Each window is at least twice as wide as the
# check interval (10 minutes, see the workflow's cron), so a normal run
# schedule gets two chances per stage. "only_high" keeps the second
# reminder for High-impact events only, to limit noise.
REMINDER_STAGES = [
    {"name": "early", "min": 40, "max": 65, "only_high": False, "prefix": "\u23F0"},
    {"name": "late", "min": 5, "max": 25, "only_high": True, "prefix": "\u23F0\u23F0"},
]

# Entries older than this are dropped from the state file so it doesn't
# grow forever.
PRUNE_OLDER_THAN_DAYS = 3


# ---------------------------------------------------------------------
# Fetching the calendar (same source/logic as weekly_market_prep.py)
# ---------------------------------------------------------------------

def fetch_usd_events() -> list:
    """
    Returns a list of {"id", "title", "when", "impact"} for this week's
    USD High/Medium impact events. "when" is a timezone-aware UTC
    datetime. "id" is a stable string used to dedupe reminders.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CalendarReminderBot/1.0)"}
    resp = requests.get(CALENDAR_URL, headers=headers, timeout=20)
    resp.raise_for_status()
    raw_events = resp.json()

    events = []
    for e in raw_events:
        try:
            if e.get("country") != "USD":
                continue
            if e.get("impact") not in ("High", "Medium"):
                continue
            when = date_parser.parse(e["date"])
            if when.tzinfo is None:
                # Same fallback assumption as weekly_market_prep.py: the
                # feed's times are conventionally US Eastern if no
                # explicit offset is present.
                from zoneinfo import ZoneInfo
                when = when.replace(tzinfo=ZoneInfo("America/New_York"))
            when_utc = when.astimezone(timezone.utc)
            title = e.get("title", "?")
            event_id = f"{title}|{when_utc.isoformat()}"
            events.append({"id": event_id, "title": title, "when": when_utc, "impact": e["impact"]})
        except Exception:  # noqa: BLE001
            continue  # skip malformed rows rather than failing the whole feed

    return events


# ---------------------------------------------------------------------
# State (already-reminded event ids)
# ---------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not read state file, starting fresh: {exc}", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def prune_state(state: dict, now_utc: datetime) -> dict:
    cutoff = now_utc - timedelta(days=PRUNE_OLDER_THAN_DAYS)
    pruned = {}
    for event_id, reminded_at_str in state.items():
        try:
            reminded_at = datetime.fromisoformat(reminded_at_str)
            if reminded_at >= cutoff:
                pruned[event_id] = reminded_at_str
        except ValueError:
            continue
    return pruned


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set. "
            "Set them as GitHub repo secrets (see README)."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=20)
    resp.raise_for_status()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    now_utc = datetime.now(timezone.utc)
    events = fetch_usd_events()
    state = load_state()

    # (stage name, start time) -> [(event, state key, minutes until start)]
    groups = {}
    for e in events:
        minutes_until = (e["when"] - now_utc).total_seconds() / 60
        for stage in REMINDER_STAGES:
            if stage["only_high"] and e["impact"] != "High":
                continue
            if not (stage["min"] <= minutes_until <= stage["max"]):
                continue
            key = f"{e['id']}|{stage['name']}"
            if key in state:
                continue
            # Entries written by the previous single-window version had no
            # stage suffix; treat them as the "early" reminder already sent.
            if stage["name"] == "early" and e["id"] in state:
                continue
            groups.setdefault((stage["name"], e["when"]), []).append((e, key, minutes_until))

    due_count = sum(len(v) for v in groups.values())
    print(f"Diagnostic: {len(events)} USD events this week, {due_count} due for a reminder right now.")

    sent = 0
    stage_by_name = {st["name"]: st for st in REMINDER_STAGES}
    for (stage_name, when), items in sorted(groups.items(), key=lambda kv: kv[0][1]):
        moscow_time = when.astimezone(timezone(timedelta(hours=3)))
        minutes_until = items[0][2]
        lines = [f"{stage_by_name[stage_name]['prefix']} Через ~{round(minutes_until)} мин ({moscow_time.strftime('%H:%M')} МСК):"]
        for event, _key, _m in items:
            icon = "\U0001F534" if event["impact"] == "High" else "\U0001F7E1"
            lines.append(f"{icon} {event['title']} (USD, {event['impact']})")
        try:
            send_telegram_message("\n".join(lines))
            for _event, key, _m in items:
                state[key] = now_utc.isoformat()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to send reminder for {when.isoformat()}: {exc}", file=sys.stderr)
            # Not marked as reminded, so the next run retries while still in the window.

    state = prune_state(state, now_utc)
    save_state(state)
    print(f"Sent {sent} reminder message(s).")


if __name__ == "__main__":
    main()
