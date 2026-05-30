"""Follow-up callback service: when to call back, and placing the outbound call.

Zero extra dependencies — the Twilio REST call goes out over stdlib urllib with
HTTP Basic auth, and the outbound call's TwiML connects the leg straight to the
deployed Pipecat crisis-bot agent. The bot then detects the call is outbound
(from == our Twilio number), treats the 'to' number as the caller, loads their
memory, and opens with the promised check-in.

Demo knob: set CRISIS_FOLLOWUP_DELAY_SECONDS to force every follow-up due that
many seconds out, so a callback fires live on stage (~120) instead of "tomorrow".
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def parse_when(when_text: str | None) -> str:
    """Map the caller's natural-language 'when' to an ISO-8601 UTC due time.

    Heuristic and forgiving — precision isn't the point; getting a real timestamp
    the scheduler can compare is. The demo override short-circuits everything.
    """
    now = datetime.now(timezone.utc)
    demo = os.getenv("CRISIS_FOLLOWUP_DELAY_SECONDS")
    if demo:
        return (now + timedelta(seconds=int(demo))).isoformat()

    t = (when_text or "").lower()
    if "now" in t or "right away" in t:
        delta = timedelta(minutes=2)
    elif "minute" in t or "bit" in t or "shortly" in t:
        delta = timedelta(minutes=15)
    elif "hour" in t:
        delta = timedelta(hours=1)
    elif "tonight" in t or "evening" in t or "later today" in t:
        delta = timedelta(hours=6)
    elif "week" in t:
        delta = timedelta(days=7)
    else:  # "tomorrow", "morning", or anything unrecognized
        delta = timedelta(days=1)
    return (now + delta).isoformat()


def build_followup_twiml() -> str:
    """TwiML that connects the outbound leg to the deployed Pipecat agent.

    PIPECAT_SERVICE_HOST is '<agent-name>.<org>' (e.g. 'crisis-bot.my-org'),
    same value used in the inbound TwiML Bin.
    """
    host = os.environ["PIPECAT_SERVICE_HOST"]
    ws = os.getenv("PIPECAT_TWILIO_WS", "wss://api.pipecat.daily.co/ws/twilio")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{ws}">'
        f'<Parameter name="_pipecatCloudServiceHost" value="{host}"/>'
        "</Stream></Connect></Response>"
    )


def place_outbound_call(to_number: str) -> str:
    """Create a Twilio outbound call to the caller, connected to the bot. Returns the call SID."""
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    form = urllib.parse.urlencode(
        {"To": to_number, "From": from_number, "Twiml": build_followup_twiml()}
    ).encode()

    req = urllib.request.Request(url, data=form, method="POST")
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("sid", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twilio call failed ({e.code}): {e.read().decode()[:300]}") from e
