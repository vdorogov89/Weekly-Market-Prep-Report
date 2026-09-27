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

WHY A 30-60 MINUTE WINDOW, CHECKED EVERY 30 MINUTES:
GitHub Actions documents its `schedule` trigger as best-effort, not
guaranteed — runs can be delayed by anywhere from a few minutes to
much longer during high load. Checking every 30 minutes (rather than
every 5-10) also matters for a separate reason: GitHub bills each job
run in whole-minute increments regardless of how briefly it actually
runs, so a 5-10 minute check interval adds up to 2000+ billed minutes a
month all by itself — enough to exhaust a private repo's entire free
Actions quota (public repos have unlimited free minutes, so this only
matters if this repo is private). A 30-minute window guarantees at
least 30 minutes' notice and is wide enough that no event's countdown
can slip through entirely between two 30-minute-spaced checks.

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

# Remind when an event starts between REMIND_MIN and REMIND_MAX minutes
# from now. The window must be at least as wide as the check interval
# (30 minutes, see the workflow's cron) so that no event's countdown can
# slip through entirely between two checks.
REMIND_MIN_MINUTES = 30
REMIND_MAX_MINUTES = 60

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

    due = []
    for e in events:
        minutes_until = (e["when"] - now_utc).total_seconds() / 60
        if REMIND_MIN_MINUTES <= minutes_until <= REMIND_MAX_MINUTES and e["id"] not in state:
            due.append((e, minutes_until))

    print(f"Diagnostic: {len(events)} USD events this week, {len(due)} due for a reminder right now.")

    for event, minutes_until in due:
        moscow_time = event["when"].astimezone(timezone(timedelta(hours=3)))
        impact_icon = "\U0001F534" if event["impact"] == "High" else "\U0001F7E1"
        message = (
            f"\u23F0 Через ~{round(minutes_until)} мин ({moscow_time.strftime('%H:%M')} МСК):\n"
            f"{impact_icon} {event['title']} (USD, {event['impact']})"
        )
        try:
            send_telegram_message(message)
            state[event["id"]] = now_utc.isoformat()
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: failed to send reminder for {event['title']!r}: {exc}", file=sys.stderr)
            # Not marked as reminded, so it will be retried on the next run
            # (still within the window, since the window is 10 minutes wide).

    state = prune_state(state, now_utc)
    save_state(state)
    print(f"Sent {len(due)} reminder(s).")


if __name__ == "__main__":
    main()
