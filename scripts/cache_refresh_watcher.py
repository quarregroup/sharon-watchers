#!/usr/bin/env python3
"""
🔒💾 CACHE AUTO-RENEWAL WATCHER — 24/7 enforcement of the HARD RULE.

Locked 2026-06-08 (Milan): "anything that has a cache set to expire must
automatically be renewed. and it should always be listening and watching this.
no excuse."

Behavior on every fire (every 4h via GA cron):
1. Refresh every cache in DEFAULT_CACHE_TARGETS (cached_prompts.refresh_all_caches)
2. Cross-check live state on Gemini API (client.caches.list) vs the registry —
   any cache_name in registry but not in live = DEAD = Slack-alert
3. Print a one-screen summary table for the run log
4. Exit 0 if all green, exit 1 + Slack-alert if anything red

Source files (evaluator/judge/reflection .md) must be present at the paths
declared in DEFAULT_CACHE_TARGETS. In the GA repo these are bundled under
`./prompts/` and DEFAULT_CACHE_TARGETS paths are remapped via the env var
SHARON_PROMPT_ROOT (or symlink).

Env required:
- GEMINI_API_KEY
- SHARON_CRITICAL_ALERT_WEBHOOK (Slack incoming-webhook URL)
- SHARON_PROMPT_ROOT (optional — defaults to /mnt/c/Users/mp243/Downloads/sharon_prompt/phone_prompt)
"""
import os, sys, json, re
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).parent
PROMPT_ROOT = Path(os.environ.get("SHARON_PROMPT_ROOT",
                                  "/mnt/c/Users/mp243/Downloads/sharon_prompt/phone_prompt"))

# Make cached_prompts importable from the watcher repo
sys.path.insert(0, str(PROMPT_ROOT / "langsmith_suite"))

# Slack webhook for FAILED alerts
SLACK = os.environ.get("SHARON_CRITICAL_ALERT_WEBHOOK") or os.environ.get("PRIORITY_BRIEF_WEBHOOK_URL")


def slack_alert(text: str, blocks: list | None = None):
    if not SLACK:
        print(f"[NO WEBHOOK] would alert: {text}", file=sys.stderr)
        return
    import requests
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        r = requests.post(SLACK, json=payload, timeout=10)
        if r.status_code >= 300:
            print(f"slack POST failed {r.status_code}: {r.text[:120]}", file=sys.stderr)
    except Exception as e:
        print(f"slack POST exception: {e}", file=sys.stderr)


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        slack_alert("🚨 cache_refresh_watcher: GEMINI_API_KEY missing — cannot refresh")
        sys.exit(1)

    from cached_prompts import refresh_all_caches, _get_client, load_registry, DEFAULT_CACHE_TARGETS

    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] cache_refresh_watcher fire")
    print(f"  prompt_root: {PROMPT_ROOT}")
    print(f"  targets:     {len(DEFAULT_CACHE_TARGETS)}")

    # --- Step 1: refresh every target ---
    refreshed = refresh_all_caches(ttl_hours=24)

    # --- Step 2: cross-check live state vs registry ---
    client = _get_client()
    try:
        live_names = {c.name for c in client.caches.list()}
    except Exception as e:
        slack_alert(f"🚨 cache_refresh_watcher: list_caches failed — {e}")
        sys.exit(1)

    reg = load_registry()
    dead = []
    for t in DEFAULT_CACHE_TARGETS:
        name = t["name"]
        entry = reg.get(name) or {}
        cn = entry.get("cache_name")
        if cn and cn not in live_names:
            dead.append((name, cn))
        elif not cn:
            # Skipped (below MIN_CACHE_TOKENS) or never created
            dead.append((name, "<missing>"))

    print(f"  live caches on Gemini: {len(live_names)}")
    print(f"  registered targets:    {len(DEFAULT_CACHE_TARGETS)}")
    print(f"  dead/missing:          {len(dead)}")

    if dead:
        lines = [f"• `{n}` → {cn[:50]}" for n, cn in dead]
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "🚨 Cache auto-renewal — FAILED"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*{len(dead)}* cache(s) dead or missing after refresh attempt.\n\n" + "\n".join(lines)}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"watcher run at `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`"}
            ]},
        ]
        slack_alert(f"🚨 cache_refresh_watcher: {len(dead)} caches dead/missing", blocks=blocks)
        sys.exit(1)

    print("✅ ALL GREEN — every registered cache is alive on Gemini API")
    sys.exit(0)


if __name__ == "__main__":
    main()
