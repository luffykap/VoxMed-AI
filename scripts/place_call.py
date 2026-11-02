"""
Place an outbound Twilio call to your phone that connects to the VoxMed AI.

Usage:
    # Either add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER to .env
    # or export them in your shell, then run:
    ./.venv/bin/python scripts/place_call.py +91XXXXXXXXXX
"""
import os
import sys
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

missing = [
    k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    if not os.environ.get(k)
]
if missing:
    sys.exit(
        f"Missing environment variable(s): {', '.join(missing)}\n\n"
        "Add them to your .env file (or export them in terminal):\n"
        "  TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "  TWILIO_AUTH_TOKEN=your_auth_token\n"
        "  TWILIO_PHONE_NUMBER=+1xxxxxxxxxx\n"
    )

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
call = client.calls.create(
    to=to_number,
    from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=f"{BASE_URL.rstrip('/')}/twilio/incoming",
    method="POST",
)
print(f"Call started to {to_number}! Call SID: {call.sid}")
