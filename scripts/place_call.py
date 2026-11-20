"""
Place an outbound Twilio call to your phone that connects to the VoxMed AI.

Usage:
    # Either add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER to .env
    # or export them in your shell, then run:
    ./.venv/bin/python scripts/place_call.py +91XXXXXXXXXX
"""
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from twilio.rest import Client

# Load variables from .env in project root automatically
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = os.environ.get("VOXMED_URL", "https://voxmed-ai.onrender.com")

if len(sys.argv) != 2:
    sys.exit("Usage: ./.venv/bin/python scripts/place_call.py +91XXXXXXXXXX")

to_number = sys.argv[1].strip()
# Auto-prepend +91 if a 10-digit Indian mobile number is passed without country code
if len(to_number) == 10 and to_number.isdigit():
    to_number = f"+91{to_number}"
elif not to_number.startswith("+"):
    to_number = f"+{to_number}"

# Warm up Render + NeonDB connection pool before placing the call
try:
    print(f"Warming up {BASE_URL} ...")
    urllib.request.urlopen(f"{BASE_URL.rstrip('/')}/twilio/language_selected?CallSid=WARMUP&Digits=1", timeout=60).read()
except Exception as exc:
    print(f"Warmup notice: {exc}")

sw_space = os.environ.get("SIGNALWIRE_SPACE_URL", "").strip().replace("https://", "").replace("http://", "").rstrip("/")
sw_project = os.environ.get("SIGNALWIRE_PROJECT_ID", "").strip()
sw_token = os.environ.get("SIGNALWIRE_API_TOKEN", "").strip()
sw_from = os.environ.get("SIGNALWIRE_PHONE_NUMBER", "").strip()

if sw_space and sw_project and sw_token and sw_from and "--signalwire" in sys.argv:
    import httpx
    api_url = f"https://{sw_space}/api/laml/2010-04-01/Accounts/{sw_project}/Calls.json"
    resp = httpx.post(
        api_url,
        auth=(sw_project, sw_token),
        data={
            "To": to_number,
            "From": sw_from,
            "Url": f"{BASE_URL.rstrip('/')}/twilio/incoming",
        },
        timeout=20.0,
    )
    if resp.status_code >= 400:
        sys.exit(f"SignalWire API Error ({resp.status_code}): {resp.text}")
    data = resp.json()
    print(f"[SignalWire] Call started to {to_number}! Call SID: {data.get('sid')}")
    sys.exit(0)

missing = [
    k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    if not os.environ.get(k)
]
if missing:
    sys.exit(
        f"Missing environment variable(s): {', '.join(missing)}\n\n"
        "Add either SignalWire or Twilio credentials to your .env file:\n"
        "  SIGNALWIRE_SPACE_URL=your-space.signalwire.com\n"
        "  SIGNALWIRE_PROJECT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n"
        "  SIGNALWIRE_API_TOKEN=PTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "  SIGNALWIRE_PHONE_NUMBER=+1xxxxxxxxxx\n"
    )

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
call = client.calls.create(
    to=to_number,
    from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=f"{BASE_URL.rstrip('/')}/twilio/incoming",
)
print(f"[Twilio] Call started to {to_number}! Call SID: {call.sid}")
