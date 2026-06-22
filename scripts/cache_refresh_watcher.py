#!/usr/bin/env python3
"""
🔒💾 GEMINI CACHE TTL AUTO-EXTEND — bulletproof cache never expires.

Locked 2026-06-22 (Milan): "cache should never be able to expire" + "we
maximize caching as much as we can and at all times."

Behavior on every fire:
1. List every Gemini context cache via `client.caches.list()` — the source
   of truth, not a local registry (which can drift).
2. For each cache, call `client.caches.update(ttl='24h')` to push expiry
   24h out from NOW. This is the single most-robust pattern Gemini supports:
   "as long as something is touching the cache, it never dies."
3. Print a one-screen summary for the run log.
4. Alert via Slack if listing fails OR if there are ZERO caches alive
   (= Sharon will fall back to uncached = $$$ leak).

Replaces the prior watcher that depended on a local DEFAULT_CACHE_TARGETS
registry + SHARON_PROMPT_ROOT pointing at Milan's WSL filesystem. That
approach was fragile (paths, bundled prompts, symlinks). This approach
is "extend what's actually alive" — works regardless of which service
created the cache.

Env required:
  GEMINI_API_KEY                 — for `client.caches.list/update`
  SHARON_CRITICAL_ALERT_WEBHOOK  — Slack webhook for failure alerts
"""
import os
import sys
from datetime import datetime, timezone

# Slack webhook for FAILED alerts (Sharon's main critical channel)
SLACK = os.environ.get("SHARON_CRITICAL_ALERT_WEBHOOK") or os.environ.get(
    "PRIORITY_BRIEF_WEBHOOK_URL"
)

NEW_TTL = "86400s"  # 24h — Gemini's max explicit TTL


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
            print(
                f"slack POST failed {r.status_code}: {r.text[:120]}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"slack POST exception: {e}", file=sys.stderr)


def main():
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get(
        "GOOGLE_API_KEY"
    ):
        slack_alert(
            "🚨 cache_refresh_watcher: GEMINI_API_KEY missing — cannot list/extend"
        )
        sys.exit(1)

    # google-genai SDK auto-picks GEMINI_API_KEY or GOOGLE_API_KEY
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] cache_refresh_watcher fire")

    # Step 1 — list all caches
    try:
        caches = list(client.caches.list())
    except Exception as e:
        slack_alert(f"🚨 cache_refresh_watcher: list_caches failed — {e}")
        print(f"list_caches failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  found {len(caches)} live cache(s)")

    if not caches:
        slack_alert(
            "🚨 cache_refresh_watcher: ZERO live caches on Gemini. "
            "Sharon falls back to uncached path = $$ leak."
        )
        sys.exit(1)

    # Step 2 — extend TTL on each
    extended = []
    failed = []
    for c in caches:
        try:
            updated = client.caches.update(
                name=c.name,
                config=genai_types.UpdateCachedContentConfig(ttl=NEW_TTL),
            )
            extended.append((c.name, c.display_name, updated.expire_time))
            print(
                f"  ✅ extended {c.name} display='{c.display_name}' new_expire={updated.expire_time}"
            )
        except Exception as e:
            failed.append((c.name, c.display_name, str(e)))
            print(
                f"  ❌ extend FAILED {c.name} display='{c.display_name}': {e}",
                file=sys.stderr,
            )

    if failed:
        lines = [f"• `{n}` ({dn}) → {err[:120]}" for n, dn, err in failed]
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 Cache TTL extend — partial failure",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{len(failed)}* cache(s) failed to extend "
                        f"(of {len(caches)} total).\n\n" + "\n".join(lines)
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"watcher run at `{ts}`",
                    }
                ],
            },
        ]
        slack_alert(
            f"🚨 cache_refresh_watcher: {len(failed)}/{len(caches)} extends failed",
            blocks=blocks,
        )
        sys.exit(1)

    print(
        f"✅ ALL GREEN — extended {len(extended)} caches, "
        f"each now expires ~24h from now"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
