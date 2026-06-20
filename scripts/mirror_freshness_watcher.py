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

# Each entry: (max_age_min_during_biz_hours, business_hours_only?)
#  - business_hours_only=True → only checked M-F 12-22 UTC (8AM-6PM ET).
#    Outside that window the job is correctly idle, no alert.
#  - business_hours_only=False → always checked (e.g. the watcher itself).
WATCHED_JOBS = {
    # bq_to_r2_mirror_pipedrive removed 2026-06-19 — cron set to '0 0 1 1 0'
    # (effectively never runs). Pipedrive freshness is now covered by
    # postgres_to_r2_mirror (live Postgres sink) + ingest_pipedrive +
    # the per-tick add_initial_qualification chain.
    "bq_to_r2_mirror_call_sms": (90, True),   # cron :08/:38 12-22 UTC M-F
    "postgres_to_r2_mirror":    (90, True),   # cron :10/:40 12-22 UTC M-F
    "mirror_freshness_monitor": (30, False),  # itself runs */5 always; 30m = dead-man
}


def in_business_window(now: datetime) -> bool:
    """M-F 12-22 UTC = 8 AM - 6 PM ET (EDT). Outside this window the mirror
    jobs correctly do not run, so we should not page on their idleness.
    Mon=0 .. Sun=6. Buffer of +30min on the close-of-day side so a job that
    successfully fired at the last biz-hours tick (e.g. 21:50 UTC) doesn't
    immediately flag at 22:01."""
    if now.weekday() > 4:  # Sat/Sun
        return False
    hour = now.hour
    return 12 <= hour <= 22


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

    biz = in_business_window(now)
    lagged: list[tuple[str, datetime | None, int]] = []
    last_success_map = status.get("last_success", {}) if isinstance(status, dict) else {}
    for job, (max_min, biz_only) in WATCHED_JOBS.items():
        # Skip biz-hours-only jobs outside the window — they correctly idle.
        if biz_only and not biz:
            continue
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
