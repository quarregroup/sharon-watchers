#!/usr/bin/env python3
"""
🚨 LIVE CRITICAL-BUG MONITOR — scans recent VAPI calls for call-killer bug patterns.

Runs the same critical-bug checks the deploy gate uses, but on the ACTUAL transcript of every prod call.
Hits a Slack alert when Sharon ships a critical bug in production.

Designed to run every 5-10 minutes via cron / Cloud Scheduler.

Critical patterns it catches (each = call-killer that breaks the call before discovery):

1. LITERAL_NAME_LEAK     — Sharon spoke "Mr. Johnson" / "Ms. Smith" / etc. (literals from prompt examples)
2. LITERAL_ADDRESS_LEAK  — Sharon spoke "42-17" / "Lenox Avenue" / etc.
3. DOUBLE_INTRO          — Sharon introduced herself ≥2 consecutive turns ("Sharon" + "Quarry" in N then N+1)
4. HYPHENATED_DIGITS     — Sharon spoke a hyphenated address as 4 individual digits ("four two one seven")
5. CLOSER_LEAD_SWAP      — Sharon said "{lead_lastname} will give you a ring" instead of "Milan"
6. AFTER_FAREWELL_QUESTION — Sharon asked a new question AFTER farewell
7. SIGNAL_WORD_SPOKEN    — Sharon said "foreclosure" / "tax lien" / "probate" aloud
8. FAB_PRIOR_CONTACT     — Sharon claimed prior chat on a COLD deal
9. OH_HELLO_ON_OUTBOUND  — Sharon responded "Oh hello" on a cold outbound call
10. VERBATIM_REPEAT_10+W — Same 10+ word sentence appears in 2+ Sharon turns

Usage:
    python3 sharon_critical_bug_monitor.py           # scan last 30 min
    python3 sharon_critical_bug_monitor.py --min 60  # scan last 60 min
"""
import os, sys, json, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request
import requests

VAPI_KEY = os.environ["VAPI_API_KEY"]
# Route to the channel Milan actually watches (priority-brief) — falls back to EOD only if priority-brief env not loaded
SLACK_WEBHOOK = (
    os.environ.get("SHARON_CRITICAL_ALERT_WEBHOOK")
    or os.environ.get("PRIORITY_BRIEF_WEBHOOK_URL")
    or os.environ.get("EOD_DASHBOARD_WEBHOOK_URL")
)

ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID", "5e2570ad-422b-4c44-bc0a-c531c8dbc032")

STATE_FILE = Path(os.environ.get("STATE_DIR", "/tmp")) / "sharon_critical_bug_monitor_state.json"


def vapi_get_calls_since(since_dt: datetime) -> list:
    """Fetch all calls since the given datetime."""
    url = f"https://api.vapi.ai/call?limit=100&createdAtGt={since_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
    r = requests.get(url, headers={"Authorization": f"Bearer {VAPI_KEY}"}, timeout=30)
    r.raise_for_status()
    return r.json() or []


# ---------- Critical-bug detectors ----------

LITERAL_NAMES_BANNED = re.compile(
    r"\bM(?:r|s|rs)\.\s*(Johnson|Smith|Reilly|Romero|Chen|Noelien|Davis|Phillips|Garcia|Williams|Kim|Patel|Lopez)\b",
    re.IGNORECASE,
)
LITERAL_ADDRESS_BANNED = re.compile(
    r"\b(42-17|forty[- ]two seventeen|Lenox Avenue|Maple Avenue|Lincoln Avenue|Long Island City|Pine Street|Main Street)\b",
    re.IGNORECASE,
)
SHARON_INTRO_RE = re.compile(r"\b(Sharon)\b.{0,40}\b(Quarry|Quarre)\b", re.IGNORECASE)
HYPHENATED_DIGITS_RE = re.compile(
    r"\b(four|three|two|five|six|seven|eight|nine|one) (one|two|three|four|five|six|seven|eight|nine|zero) (one|two|three|four|five|six|seven|eight|nine|zero) (one|two|three|four|five|six|seven|eight|nine|zero)\b",
    re.IGNORECASE,
)
HYPHENATED_DIGITS_DIGIT_FORM_RE = re.compile(r"\b\d \d \d \d \d?\b")
SIGNAL_WORDS_RE = re.compile(
    r"\b(foreclosure|tax lien|lis pendens|NOD|probate|distressed|distress|estate filing)\b",
    re.IGNORECASE,
)
FAB_PRIOR_CONTACT_RE = re.compile(
    r"\b(we chatted before|as we discussed|last time we spoke|Milan asked me to follow up|we spoke earlier)\b",
    re.IGNORECASE,
)
OH_HELLO_RE = re.compile(r"^[ \t]*Oh,?\s+hello\.?$", re.IGNORECASE | re.MULTILINE)


def parse_ai_turns(transcript: str) -> list[str]:
    """Extract Sharon's spoken turns from a VAPI transcript."""
    turns = []
    for line in transcript.splitlines():
        m = re.match(r"^(AI|Assistant|Sharon):\s*(.*)$", line.strip(), re.IGNORECASE)
        if m:
            turns.append(m.group(2).strip())
    return turns


def parse_user_turns(transcript: str) -> list[str]:
    turns = []
    for line in transcript.splitlines():
        m = re.match(r"^(User|Customer|Caller):\s*(.*)$", line.strip(), re.IGNORECASE)
        if m:
            turns.append(m.group(2).strip())
    return turns


def check_critical_bugs(call: dict) -> list[dict]:
    """Return list of {bug, evidence} dicts for any critical pattern hit."""
    transcript = call.get("transcript") or ""
    if not transcript.strip():
        return []
    ai_turns = parse_ai_turns(transcript)
    user_turns = parse_user_turns(transcript)
    all_ai = "\n".join(ai_turns)

    hits = []

    # 1. LITERAL_NAME_LEAK — but ONLY if the leaked name is NOT in this call's deal_data
    overrides = call.get("assistantOverrides") or {}
    vv = overrides.get("variableValues") or {}
    dd = vv.get("deal_data")
    if isinstance(dd, str):
        try:
            dd = json.loads(dd)
        except Exception:
            dd = {}
    ddd = (dd.get("deal") if isinstance(dd, dict) else None) or dd or {}
    if isinstance(ddd, dict):
        ddd = ddd.get("deal", ddd) if isinstance(ddd.get("deal"), dict) else ddd
    real_lastname = ((ddd or {}).get("person_name") or "").split(" ")[-1] if isinstance(ddd, dict) else ""

    for m in LITERAL_NAMES_BANNED.finditer(all_ai):
        leaked_name = m.group(0)
        leaked_last = m.group(1)
        if real_lastname and leaked_last.lower() == real_lastname.lower():
            continue  # OK — Sharon used the actual deal_data name
        hits.append({"bug": "LITERAL_NAME_LEAK", "evidence": f"Sharon said '{leaked_name}' but deal_data lastname is '{real_lastname}'"})

    # 2. LITERAL_ADDRESS_LEAK
    real_props = []
    if isinstance(ddd, dict):
        for p in (ddd.get("properties") or [])[:5]:
            if isinstance(p, dict):
                real_props.append(p.get("property_address_normalized", "").upper())
    real_props_str = " ".join(real_props)
    for m in LITERAL_ADDRESS_BANNED.finditer(all_ai):
        leaked = m.group(0)
        if leaked.upper() in real_props_str:
            continue
        hits.append({"bug": "LITERAL_ADDRESS_LEAK", "evidence": f"Sharon said '{leaked}' — not in deal_data"})

    # 3. DOUBLE_INTRO — two consecutive AI turns both mention Sharon+Quarry intro
    for i in range(len(ai_turns) - 1):
        if SHARON_INTRO_RE.search(ai_turns[i]) and SHARON_INTRO_RE.search(ai_turns[i + 1]):
            hits.append({
                "bug": "DOUBLE_INTRO",
                "evidence": f"AI turn {i+1}+{i+2} both intro'd: '{ai_turns[i][:80]}' → '{ai_turns[i+1][:80]}'"
            })
            break

    # 4. HYPHENATED_DIGITS — 4 sequential digit-words OR 4 sequential digit-chars
    if HYPHENATED_DIGITS_RE.search(all_ai) or HYPHENATED_DIGITS_DIGIT_FORM_RE.search(all_ai):
        # Find the actual snippet
        m = HYPHENATED_DIGITS_RE.search(all_ai) or HYPHENATED_DIGITS_DIGIT_FORM_RE.search(all_ai)
        hits.append({
            "bug": "HYPHENATED_DIGITS",
            "evidence": f"Sharon spoke address as digits: '{m.group(0)}'"
        })

    # 5. CLOSER_LEAD_SWAP — Sharon says "{lead_lastname} will give you a ring/call"
    if real_lastname:
        swap_re = re.compile(rf"\b(M(?:r|s|rs)\. )?{re.escape(real_lastname)}\b.{{0,30}}\b(will give you a ring|will call|will follow up|will reach out)\b", re.IGNORECASE)
        m = swap_re.search(all_ai)
        if m:
            hits.append({"bug": "CLOSER_LEAD_SWAP", "evidence": f"Sharon said: '{m.group(0)[:100]}'"})

    # 6. AFTER_FAREWELL_QUESTION
    farewell_re = re.compile(r"\b(have a great day|take care|appreciate your time|i'?ll let you get back)\b", re.IGNORECASE)
    for i, turn in enumerate(ai_turns):
        if farewell_re.search(turn) and i < len(ai_turns) - 1:
            next_turn = ai_turns[i + 1]
            if "?" in next_turn:
                hits.append({"bug": "AFTER_FAREWELL_QUESTION", "evidence": f"After '{turn[:60]}', Sharon asked: '{next_turn[:80]}'"})
                break

    # 7. SIGNAL_WORD_SPOKEN
    sig_match = SIGNAL_WORDS_RE.search(all_ai)
    if sig_match:
        signal_in_data = (isinstance(ddd, dict) and any(
            sig_match.group(0).lower() in str(v).lower()
            for v in [ddd.get("signal_type"), ddd.get("signal_subtype")] if v
        ))
        # Even if it's in signal_type, Sharon shouldn't speak it
        hits.append({"bug": "SIGNAL_WORD_SPOKEN", "evidence": f"Sharon said '{sig_match.group(0)}' aloud"})

    # 8. FAB_PRIOR_CONTACT — only fire if deal_data.conversation is empty (cold)
    conv = ddd.get("conversation", []) if isinstance(ddd, dict) else []
    if not conv:
        m = FAB_PRIOR_CONTACT_RE.search(all_ai)
        if m:
            hits.append({"bug": "FAB_PRIOR_CONTACT", "evidence": f"Cold deal but Sharon said: '{m.group(0)}'"})

    # 9. OH_HELLO_ON_OUTBOUND
    is_inbound = call.get("type") == "inboundPhoneCall" or call.get("type") == "inbound"
    if not is_inbound and ai_turns and OH_HELLO_RE.match(ai_turns[0]):
        hits.append({"bug": "OH_HELLO_ON_OUTBOUND", "evidence": f"Outbound call, Sharon's T1: '{ai_turns[0][:60]}'"})

    # 10. VERBATIM_REPEAT_10+W — same 10-word sentence twice
    seen_sentences = {}
    for i, turn in enumerate(ai_turns):
        for sent in re.split(r"[.!?]\s+", turn):
            words = sent.strip().split()
            if len(words) >= 10:
                key = " ".join(w.lower() for w in words[:15])
                if key in seen_sentences and seen_sentences[key] != i:
                    hits.append({
                        "bug": "VERBATIM_REPEAT_10W",
                        "evidence": f"Sentence repeated across turns {seen_sentences[key]+1} and {i+1}: '{sent[:100]}'"
                    })
                    break
                seen_sentences[key] = i
        else:
            continue
        break

    return hits


def slack_alert(hits_by_call: dict):
    """Post a single Slack message with all hits."""
    if not SLACK_WEBHOOK or not hits_by_call:
        return
    lines = ["🚨 *SHARON CRITICAL BUG ALERT* — call-killer pattern(s) detected in production"]
    for call_id, info in hits_by_call.items():
        meta = info["meta"]
        hits = info["hits"]
        lines.append(f"\n*Call `{call_id[:36]}`* ({meta.get('type','?')}, {meta.get('started_at','?')[:19]})")
        for h in hits:
            lines.append(f"  • `{h['bug']}` — {h['evidence'][:200]}")
    payload = {"text": "\n".join(lines)[:3900]}
    req = urllib.request.Request(SLACK_WEBHOOK, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  slack post failed: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"alerted_call_ids": []}


def save_state(state: dict):
    # Keep only most recent 200 alerted call_ids to avoid unbounded growth
    state["alerted_call_ids"] = state.get("alerted_call_ids", [])[-200:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    minutes_back = 30
    if "--min" in sys.argv:
        minutes_back = int(sys.argv[sys.argv.index("--min") + 1])
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M UTC')}] Scanning Sharon calls since {since.strftime('%H:%M UTC')} (-{minutes_back}min)...")

    state = load_state()
    alerted = set(state.get("alerted_call_ids", []))

    try:
        calls = vapi_get_calls_since(since)
    except Exception as e:
        print(f"  ❌ VAPI fetch failed: {e}")
        sys.exit(2)
    # Filter to Sharon prod assistant only
    calls = [c for c in calls if c.get("assistantId") == ASSISTANT_ID]
    print(f"  Found {len(calls)} Sharon calls in window")

    hits_by_call = {}
    for c in calls:
        if c.get("id") in alerted:
            continue
        bugs = check_critical_bugs(c)
        if bugs:
            hits_by_call[c["id"]] = {
                "meta": {
                    "type": c.get("type"),
                    "started_at": c.get("startedAt"),
                    "ended_reason": c.get("endedReason"),
                    "customer": (c.get("customer") or {}).get("number"),
                },
                "hits": bugs,
            }

    if hits_by_call:
        for cid, info in hits_by_call.items():
            print(f"  🚨 {cid}: {len(info['hits'])} bug(s)")
            for h in info["hits"]:
                print(f"     • {h['bug']}: {h['evidence'][:140]}")
        slack_alert(hits_by_call)
        # Mark these as alerted so we don't re-fire
        for cid in hits_by_call:
            alerted.add(cid)
        state["alerted_call_ids"] = list(alerted)
        save_state(state)
        print(f"\n  Slack alert sent for {len(hits_by_call)} call(s).")
    else:
        print(f"  ✅ no critical-bug patterns detected in {len(calls)} calls")


if __name__ == "__main__":
    main()
