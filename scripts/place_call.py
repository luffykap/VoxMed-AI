"""
Place an outbound Twilio call to your phone that connects to the VoxMed AI.

Usage:
    export TWILIO_ACCOUNT_SID=ACxxxxxxxx
    export TWILIO_AUTH_TOKEN=xxxxxxxx
    export TWILIO_PHONE_NUMBER=+1XXXXXXXXXX      # your Twilio number
    python scripts/place_call.py +91XXXXXXXXXX   # the number to ring

Trial accounts can only call numbers verified in the Twilio console
(Phone Numbers -> Manage -> Verified Caller IDs).
"""
import os
import sys

from twilio.rest import Client

BASE_URL = os.environ.get("VOXMED_URL", "https://voxmed-ai.onrender.com")

if len(sys.argv) != 2:
    sys.exit("Usage: python scripts/place_call.py <your-number-in-E.164>")

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
call = client.calls.create(
    to=sys.argv[1],
    from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=f"{BASE_URL}/twilio/incoming",  # Twilio fetches TwiML from here once you answer
    method="POST",
)
print("Call started:", call.sid)
