#!/usr/bin/env python3
"""
🔒⚙️ SCHEDULER CRON HEALTH WATCHER — every 15 min, alert on stale tier-1 jobs.

Locked 2026-06-22 (Milan: "this is our live workflow it should be battle
tested. just like our caching protocols these are things that shouldnt fail").

Reads quarre-scheduler's `/` endpoint (bearer-protected) which returns the
in-process `last_success` and `last_failure` maps per cron. For every tier-1
job:
  - If `last_failure > last_success` (i.e. last fire failed)
  - AND `last_failure` is older than 30 min (one cron tick has passed)
  - → Slack alert

The scheduler.ts process itself ALSO sends an immediate Slack alert on any
tier-1 non-zero exit. This watcher is the "fallback" — covers the case where
the scheduler PROCESS itself dies (no exit to alert from) or where Slack
was momentarily unreachable.

Tier-1 jobs (Sharon's live workflow):
  crm_update, dbt_refresh_queue_duckdb, ingest_call_sms, ingest_pipedrive,
  crm_phase1_writes, add_initial_qualification

Env required:
  SCHEDULER_BASE_URL     — https://quarre-scheduler.fly.dev
  SCHEDULER_BEARER       — bearer for /
  SHARON_CRITICAL_ALERT_WEBHOOK  — Slack webhook
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

SCHEDULER_BASE_URL = os.environ.get(
    "SCHEDULER_BASE_URL", "https://quarre-scheduler.fly.dev"
)
SCHEDULER_BEARER = os.environ.get("SCHEDULER_BEARER")
SLACK = os.environ.get("SHARON_CRITICAL_ALERT_WEBHOOK") or os.environ.get(
    "PRIORITY_BRIEF_WEBHOOK_URL"
)

TIER_1_JOBS = {
    "crm_update",
    "dbt_refresh_queue_duckdb",
    "ingest_call_sms",
    "ingest_pipedrive",
    "crm_phase1_writes",
    "add_initial_qualification",
}

STALE_THRESHOLD = timedelta(minutes=30)


def slack_alert(text: str):
    if not SLACK:
        print(f"[NO WEBHOOK] would alert: {text}", file=sys.stderr)
        return
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 300:
                print(f"slack POST failed {r.status}", file=sys.stderr)
    except Exception as e:
        print(f"slack POST exception: {e}", file=sys.stderr)


def main():
    if not SCHEDULER_BEARER:
        slack_alert(
            "🚨 cron_health_watcher: SCHEDULER_BEARER not set — cannot check"
        )
        sys.exit(1)

    req = urllib.request.Request(
        f"{SCHEDULER_BASE_URL}/",
        headers={"Authorization": f"Bearer {SCHEDULER_BEARER}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        slack_alert(
            f"🚨 cron_health_watcher: scheduler / endpoint unreachable — {e}"
        )
        sys.exit(1)

    last_success = data.get("last_success") or {}
    last_failure = data.get("last_failure") or {}

    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat(timespec='seconds')}] cron_health_watcher fire")

    stale = []
    for job in TIER_1_JOBS:
        succ = last_success.get(job)
        fail = last_failure.get(job)
        succ_dt = (
            datetime.fromisoformat(succ.replace("Z", "+00:00")) if succ else None
        )
        fail_dt = (
            datetime.fromisoformat(fail.replace("Z", "+00:00")) if fail else None
        )

        # Stale if: most recent event is a failure AND it's older than the
        # threshold (i.e. the next scheduled tick should have already happened).
        most_recent_failed = fail_dt and (not succ_dt or fail_dt > succ_dt)
        if most_recent_failed:
            age = now - fail_dt
            if age > STALE_THRESHOLD:
                stale.append((job, fail_dt.isoformat(timespec="seconds"), age))
            print(
                f"  ⚠️  {job}: last_failure={fail} succ={succ} age={age}"
            )
        else:
            print(f"  ✅ {job}: last_success={succ}")

    if not stale:
        print("✅ ALL TIER-1 GREEN")
        sys.exit(0)

    lines = [f"• `{j}` failing since {ts} (age={age})" for j, ts, age in stale]
    text = (
        f"🚨 *{len(stale)}* tier-1 scheduler cron(s) stale-failed for >30 min:\n\n"
        + "\n".join(lines)
        + "\n\nCheck `flyctl logs -a quarre-scheduler` + scheduler `/` endpoint."
    )
    slack_alert(text)
    sys.exit(1)


if __name__ == "__main__":
    main()
