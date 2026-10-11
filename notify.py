"""Send ntfy push notifications 24h and 12h before unsubmitted Gradescope deadlines.

Env vars: GS_EMAIL, GS_PASSWORD, NTFY_TOPIC
Usage:
    python notify.py          # normal run (meant for a cron / GitHub Actions)
    python notify.py --test   # list upcoming assignments and send a test push
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from gradescopeapi.classes.connection import GSConnection

THRESHOLDS_HOURS = [24, 12]
STATE_FILE = Path(__file__).parent / "sent.json"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BASE_URL = "https://www.gradescope.com"


def load_env_file():
    """Load a local .env file if present (for running on your laptop)."""
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def push(topic, title, message, click_url=None, priority="high"):
    headers = {"Title": title, "Priority": priority, "Tags": "alarm_clock"}
    if click_url:
        headers["Click"] = click_url
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()


def fetch_upcoming(email, password):
    """Return a list of unsubmitted assignments with a future due date."""
    conn = GSConnection()
    conn.login(email, password)
    now = datetime.now(timezone.utc)

    courses = conn.account.get_courses()["student"]
    upcoming = []
    for course_id, course in courses.items():
        # Skip old courses so we don't scrape every class you've ever taken.
        try:
            if int(course.year) < now.year - 1:
                continue
        except (TypeError, ValueError):
            pass

        for a in conn.account.get_assignments(course_id):
            if a.due_date is None or a.due_date <= now:
                continue
            if a.grade is not None or (a.submissions_status or "").strip() == "Submitted":
                continue
            upcoming.append(
                {
                    "key": f"{course_id}:{a.assignment_id or a.name}",
                    "course": course.name,
                    "name": a.name.strip(),
                    "due": a.due_date,
                    "url": f"{BASE_URL}/courses/{course_id}"
                    + (f"/assignments/{a.assignment_id}" if a.assignment_id else ""),
                }
            )
    return upcoming


def main():
    load_env_file()
    email = os.environ["GS_EMAIL"]
    password = os.environ["GS_PASSWORD"]
    topic = os.environ["NTFY_TOPIC"]
    test_mode = "--test" in sys.argv

    upcoming = fetch_upcoming(email, password)
    now = datetime.now(timezone.utc)

    if test_mode:
        print(f"Found {len(upcoming)} unsubmitted upcoming assignment(s):")
        for a in sorted(upcoming, key=lambda x: x["due"]):
            hours = (a["due"] - now).total_seconds() / 3600
            print(f"  [{a['course']}] {a['name']} - due in {hours:.1f}h")
        push(topic, "Gradescope notifier works", f"Tracking {len(upcoming)} upcoming assignment(s).")
        print("Test notification sent.")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    for a in upcoming:
        hours_left = (a["due"] - now).total_seconds() / 3600
        due_iso = a["due"].isoformat()
        sent = state.get(a["key"], {})
        # If the due date changed (e.g. extension), reset what we've sent.
        if sent.get("due") != due_iso:
            sent = {"due": due_iso, "sent": []}

        crossed = [t for t in THRESHOLDS_HOURS if hours_left <= t]
        if crossed and any(t not in sent["sent"] for t in crossed):
            due_local = a["due"].astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p")
            push(
                topic,
                title=f"Due in {hours_left:.0f}h: {a['name']}",
                message=f"{a['course']} - due {due_local}",
                click_url=a["url"],
                priority="urgent" if min(crossed) <= 12 else "high",
            )
            print(f"Notified: {a['course']} / {a['name']} ({hours_left:.1f}h left)")
            sent["sent"] = sorted(set(sent["sent"]) | set(crossed))
        state[a["key"]] = sent

    # Drop entries whose deadline has passed.
    state = {
        k: v for k, v in state.items()
        if datetime.fromisoformat(v["due"]) > now
    }
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
