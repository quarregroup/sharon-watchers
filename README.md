# sharon-watchers — GitHub Actions 24/7 monitoring

Two always-on sentries for Sharon production:

- **proxy-auth-sentry** — every 5 min. Live-probes the VAPI↔proxy auth path. P0 Slack alert if shared secret has drifted. Catches the 2026-05-29 outage class.
- **sharon-critical-bug-monitor** — every 5 min. Scans last 10 min of VAPI prod calls for the 10 TIER 1 call-killer patterns (literal leak, double-intro, hyphenated-digit, signal-word spoken, etc.). Slack alert per call with the bug pattern + transcript snippet.

Both alert to the **priority-brief** Slack channel (where Milan watches).

## Required GitHub Secrets

Set under repo Settings → Secrets and variables → Actions:

| Secret | Source | Purpose |
|---|---|---|
| `BQ_SA_JSON` | Contents of `/mnt/c/Users/mp243/Downloads/prj-d-bigquery-82f0-c92915952557.json` | BigQuery service account JSON (paste the whole JSON object as the secret value) |
| `VAPI_API_KEY` | `6f361e7c-471f-4d49-a2b7-026f95beabe1` | VAPI Bearer token for pulling call records |
| `VAPI_SHARED_SECRET` | `23da8e488033444d3708e2e025d4c18d200a61baf1378363` | Header VAPI sends to our proxy for auth |
| `VAPI_ASSISTANT_ID` | `5e2570ad-422b-4c44-bc0a-c531c8dbc032` | Sharon assistant ID (optional — defaulted in script) |
| `SHARON_CRITICAL_ALERT_WEBHOOK` | Slack incoming-webhook URL for priority-brief channel | Where alerts go |
| `PRIORITY_BRIEF_WEBHOOK_URL` | (optional fallback) | Same channel — fallback if the above isn't set |

## Setup steps

```bash
# 1. cd into this directory
cd github_actions_watchers

# 2. Initialize repo
git init -b main
git add .
git commit -m "Initial sharon-watchers setup"

# 3. Create a private repo on GitHub (or use existing).
#    If creating new: gh repo create quarre/sharon-watchers --private --source=. --push
#    Or push to existing: git remote add origin <url> && git push -u origin main

# 4. Add the secrets above via GitHub UI or `gh secret set`:
gh secret set BQ_SA_JSON < /mnt/c/Users/mp243/Downloads/prj-d-bigquery-82f0-c92915952557.json
gh secret set VAPI_API_KEY --body "6f361e7c-471f-4d49-a2b7-026f95beabe1"
gh secret set VAPI_SHARED_SECRET --body "23da8e488033444d3708e2e025d4c18d200a61baf1378363"
gh secret set VAPI_ASSISTANT_ID --body "5e2570ad-422b-4c44-bc0a-c531c8dbc032"
gh secret set SHARON_CRITICAL_ALERT_WEBHOOK --body "<slack webhook URL>"

# 5. Manually trigger the first run to confirm secrets work:
gh workflow run proxy-auth-sentry
gh workflow run sharon-critical-bug-monitor

# 6. Watch the runs:
gh run watch
```

## Notes

- **Cron cadence is best-effort.** GitHub Actions cron can lag 0-15 min under load, but never skips entirely.
- **Free tier:** private repos get 2,000 min/month. Each run is ~30s × 2 workflows × 12/hr × 24 = ~14,400 sec/day = 240 min/day = 7,200 min/month. **Will exceed free tier on a private repo.** Options: (a) public repo (unlimited), (b) GitHub Team plan ($4/user/mo, 3,000 min), (c) reduce cadence to */10 for one of them (halves cost). Recommend option a — code has no sensitive content; secrets are vaulted.
- **State file persistence:** the critical-bug monitor uses `actions/cache@v4` to persist the dedup state file across runs. Without this, the same bug would alert every 5 min.
- **Existing local scripts unaffected.** This is the durability layer — your manual `python3 ...` invocations and the WSL crontab keep working. Belt + suspenders.

## Adding more watchers

To add inbound_alert_watcher, pipedrive_reminder_watcher, vapi_quality_monitor later:
1. Copy script to `scripts/`
2. Env-ify any hardcoded paths/keys
3. Add a new `.github/workflows/<name>.yml`
4. Add any new secrets to the table above

## What fires the alerts

**P0 (red, immediate):**
- proxy_auth_sentry: live probe rejected → "PROXY AUTH BROKEN" Slack alert. Outbound calls are failing 401 right now.

**P1 (yellow, investigate):**
- proxy_auth_sentry: recent calls show 401 but probe works → brief drift window, look at Cloud Run revisions
- sharon_critical_bug_monitor: prod call hit a TIER 1 pattern → Sharon shipped a known call-killer

State files prevent re-alerting on the same call_id.
