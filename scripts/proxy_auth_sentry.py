#!/usr/bin/env python3
"""
🚨 PROXY AUTH SENTRY — catches VAPI↔proxy 401-unauthorized drift in near-real-time.

Why: 2026-05-29 had 65 calls fail with pipeline-error-custom-llm-401-unauthorized
silently — we only noticed days later via post-hoc analysis. Every silent 401 =
a dial Sharon couldn't answer = wasted lead. Never again.

What it does:
1. Pulls outbound VAPI calls from BQ for the last N minutes (default 30).
2. Counts calls with ended_reason matching 401/unauthorized.
3. Live-probes the proxy with our shared secret. If proxy itself rejects us,
   blast a P0 Slack alert (the secret has drifted).
4. If only post-hoc 401s appeared but the live probe works, blast a P1 alert
   (some calls hit a bad window — investigate cause).
5. State-file dedup so the same call_id doesn't re-alert.

Usage:
    python3 proxy_auth_sentry.py                  # last 30 min
    python3 proxy_auth_sentry.py --min 10         # last 10 min (for cron */5)

Slack: uses SHARON_CRITICAL_ALERT_WEBHOOK env var, falls back to
PRIORITY_BRIEF_WEBHOOK_URL.
"""
import os, sys, json, argparse, time
from pathlib import Path
from datetime import datetime, timezone

import requests

# GitHub Actions writes secrets via env vars; workflow writes BQ SA JSON to a file.
# GOOGLE_APPLICATION_CREDENTIALS is set by the workflow step before invoking us.
PROXY_URL = os.environ.get(
    "PROXY_URL",
    "https://sharon-vapi-gemini-proxy-463574266273.us-east4.run.app/v1/chat/completions",
)
VAPI_SHARED_SECRET = os.environ["VAPI_SHARED_SECRET"]
SHARON_MODEL = os.environ.get("SHARON_MODEL", "gemini-3.1-flash-lite")

STATE = Path(os.environ.get("STATE_DIR", "/tmp")) / "sharon_proxy_auth_sentry_state.json"
SLACK_WEBHOOK = (os.environ.get("SHARON_CRITICAL_ALERT_WEBHOOK")
                 or os.environ.get("PRIORITY_BRIEF_WEBHOOK_URL")
                 or os.environ.get("EOD_DASHBOARD_WEBHOOK_URL"))


def load_state():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()).get("alerted", []))
        except Exception:
            pass
    return set()


def save_state(alerted):
    STATE.write_text(json.dumps({"alerted": sorted(alerted)[-200:],
                                 "last_run": datetime.now(timezone.utc).isoformat()}))


def fetch_recent_401s(window_min: int):
    from google.cloud import bigquery
    c = bigquery.Client(project="prj-d-bigquery-82f0")
    sql = f"""
    SELECT call_id, started_at, customer_phone_number, phone_number_id, ended_reason
    FROM `prj-d-bigquery-82f0.main.vapi_call__raw`
    WHERE (ended_reason LIKE '%401%' OR ended_reason LIKE '%unauthorized%')
      AND SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', started_at)
          >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window_min} MINUTE)
    ORDER BY started_at DESC
    """
    return [dict(r) for r in c.query(sql).result()]


def probe_proxy():
    """Returns (ok: bool, detail: str). True = auth works. False = drift."""
    try:
        r = requests.post(
            PROXY_URL,
            headers={"Authorization": f"Bearer {VAPI_SHARED_SECRET}",
                     "Content-Type": "application/json"},
            json={"model": SHARON_MODEL,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 5},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "200 OK"
        if r.status_code in (401, 403):
            return False, f"{r.status_code} — proxy rejects current shared secret. SECRET HAS DRIFTED."
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, f"probe exception: {e}"


def slack_alert(severity: str, title: str, body: str):
    if not SLACK_WEBHOOK:
        print(f"[NO WEBHOOK] {severity}: {title}\n{body}")
        return
    payload = {
        "text": f"{severity} — {title}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{severity}: {title}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ],
    }
    r = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if r.status_code >= 300:
        print(f"slack POST failed: {r.status_code} {r.text[:120]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=30, help="window in minutes (default 30)")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc.strftime('%H:%M UTC')}] proxy_auth_sentry scanning last {args.min} min")

    # 1. Live probe — most authoritative signal
    probe_ok, probe_detail = probe_proxy()
    print(f"  proxy live probe: {'OK' if probe_ok else 'FAIL'} — {probe_detail}")

    # 2. Recent 401 ended_reason from BQ
    recent_401s = fetch_recent_401s(args.min)
    print(f"  401 calls in last {args.min} min: {len(recent_401s)}")

    alerted = load_state()
    new_failures = [c for c in recent_401s if c["call_id"] not in alerted]

    if not probe_ok:
        # P0 — proxy actively rejecting our secret. PROD is broken right now.
        body = (
            f"*Live proxy probe FAILED:* `{probe_detail}`\n"
            f"VAPI is sending shared secret that proxy currently rejects. "
            f"Every outbound call right now will fail with 401-unauthorized.\n\n"
            f"*Last {args.min} min in BQ:* {len(recent_401s)} call(s) hit 401.\n\n"
            f"*Fix:* compare Cloud Run env `VAPI_SHARED_SECRET` against VAPI assistant "
            f"`5e2570ad-422b-4c44-bc0a-c531c8dbc032` model.headers.Authorization. "
            f"Update whichever drifted. Re-run `infra_gate.py` after fix."
        )
        slack_alert("🚨 P0 PROXY AUTH BROKEN", "Sharon outbound calls failing 401", body)
        for c in new_failures:
            alerted.add(c["call_id"])
        save_state(alerted)
        sys.exit(2)

    if new_failures:
        # P1 — auth works now but recent calls failed → brief window of bad config
        sample = "\n".join([
            f"  • `{c['call_id']}` at {c['started_at'][:19]}Z → {c['customer_phone_number']} "
            f"(caller_id `{(c['phone_number_id'] or '')[:8]}…`)"
            for c in new_failures[:5]
        ])
        body = (
            f"*Live proxy auth: ✅ OK*\n"
            f"But *{len(new_failures)} call(s)* hit 401 in the last {args.min} min:\n{sample}\n\n"
            f"Looks like a brief drift window that has since recovered. Investigate Cloud Run "
            f"revision history or recent VAPI assistant edits."
        )
        slack_alert("⚠️ P1 PROXY AUTH FLAP", f"{len(new_failures)} 401s in window", body)
        for c in new_failures:
            alerted.add(c["call_id"])

    save_state(alerted)
    print(f"  alerted set size: {len(alerted)}, new this run: {len(new_failures)}")
    sys.exit(0 if probe_ok and not new_failures else 1)


if __name__ == "__main__":
    main()
