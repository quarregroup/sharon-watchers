#!/usr/bin/env python3
"""
Mirror-freshness watcher — fires every 15 min from GitHub Actions.

Probes quarre-scheduler /metrics for last-success timestamps of the
postgres_to_r2 mirror jobs. If any of {pipedrive, lead_manager_run,
call_sms} mirror is older than MAX_LAG_MIN, posts a Pipedrive note to
the ALERT_DEAL_ID so Milan sees it without needing a Slack workspace.

Exits 0 if green (no alert posted). Exits 1 if it actually paged.
Workflow concurrency=1 prevents storm.

Env:
- SCHEDULER_BEARER  - the same bearer in /tmp/scheduler_bearer
- PIPEDRIVE_PRODUCTION_API_KEY
- PIPEDRIVE_BOT_USER_ID
- ALERT_DEAL_ID     - the deal the Bot pings (Milan's "Sharon ops" deal)
- MAX_LAG_MIN       - default 30
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

import requests

SCHEDULER_URL = "https://quarre-scheduler.fly.dev"
PIPEDRIVE_BASE = "https://api.pipedrive.com/v1"

WATCHED_JOBS = {
    "bq_to_r2_mirror_pipedrive": 90,      # cron :30/:50, so 90 min covers 1 missed run + slack
    "bq_to_r2_mirror_call_sms": 90,       # same cadence
    "postgres_to_r2_mirror": 90,          # lead_manager_run + sharon writes
    "mirror_freshness_monitor": 30,       # itself runs every 5 min; 30 min = dead-man
}


def fetch_inflight() -> dict:
    bearer = os.environ["SCHEDULER_BEARER"]
    r = requests.get(
        f"{SCHEDULER_URL}/",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def post_pipedrive_note(deal_id: int, body: str) -> None:
    api_key = os.environ["PIPEDRIVE_PRODUCTION_API_KEY"]
    bot_id = int(os.environ["PIPEDRIVE_BOT_USER_ID"])
    r = requests.post(
        f"{PIPEDRIVE_BASE}/notes",
        params={"api_token": api_key},
        json={
            "deal_id": deal_id,
            "user_id": bot_id,
            "content": body,
        },
        timeout=15,
    )
    r.raise_for_status()


def main() -> int:
    now = datetime.now(timezone.utc)

    try:
        status = fetch_inflight()
    except Exception as e:
        # Scheduler unreachable = dead-man trip; this is the whole point of an
        # external watcher. Page Milan.
        body = (
            f"SCHEDULER UNREACHABLE\n"
            f"now (UTC): {now.isoformat()}\n"
            f"err: {e}\n"
            "Sharon's cron substrate is dead. Check quarre-scheduler on Fly."
        )
        deal_id = os.environ.get("ALERT_DEAL_ID")
        if deal_id:
            try:
                post_pipedrive_note(int(deal_id), body)
            except Exception:
                pass
        print(body, file=sys.stderr)
        return 1

    lagged: list[tuple[str, datetime | None, int]] = []
    last_success_map = status.get("last_success", {}) if isinstance(status, dict) else {}
    for job, max_min in WATCHED_JOBS.items():
        last = parse_iso(last_success_map.get(job))
        if last is None or (now - last) > timedelta(minutes=max_min):
            lagged.append((job, last, max_min))

    if not lagged:
        print("OK · all watched jobs within budget")
        return 0

    lines = ["MIRROR FRESHNESS ALERT", f"now (UTC): {now.isoformat()}"]
    for job, last, max_min in lagged:
        age_min = "never" if last is None else f"{(now - last).total_seconds() / 60:.1f}m"
        lines.append(f"- {job}: last_success {last} (age={age_min}, budget={max_min}m)")
    lines.append("Sharon's deal_data may be stale. Check quarre-scheduler logs.")
    body = "\n".join(lines)

    deal_id = os.environ.get("ALERT_DEAL_ID")
    if deal_id:
        try:
            post_pipedrive_note(int(deal_id), body)
            print(f"ALERT posted to deal {deal_id}")
        except Exception as e:
            print(f"FAIL posting Pipedrive note: {e}", file=sys.stderr)
            print(body)
            return 1
    else:
        print("ALERT_DEAL_ID not set; printing only:")
        print(body)

    return 1


if __name__ == "__main__":
    sys.exit(main())
