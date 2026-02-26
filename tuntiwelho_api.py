"""
Tuntiwelho Auto-Puncher
=======================
Automates punch-in / punch-out for the Tuntiwelho (Finago Mobiili) timecard app.

Usage:
  python tuntiwelho_api.py --username USER --password PASS --action punch_in
  python tuntiwelho_api.py --username USER --password PASS --action punch_out
  python tuntiwelho_api.py --username USER --password PASS --action test_login
  python tuntiwelho_api.py --username USER --password PASS --action punch_in --dry-run

Requirements: Python 3 (stdlib only — no pip packages needed).
"""
import urllib.request
import urllib.error
import json
import ssl
import sys
import os
import argparse
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar

# --- SSL & Session Setup ---
ssl_context = ssl.create_default_context()
# NOTE: Using default trust store. If you get SSL errors on Termux, install
# the certifi package: pip install certifi, then uncomment the next two lines:
# import certifi
# ssl_context = ssl.create_default_context(cafile=certifi.where())

HTTP_TIMEOUT = 30  # seconds — prevents indefinite hangs on Tasker

# The Tuntiwelho PHP backend uses a session cookie (Tyovuorovelho=...)
# This cookie MUST be sent on subsequent requests or the server crashes with
# "Attempt to read property 'id' on null".
cookie_jar = CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cookie_jar),
    urllib.request.HTTPSHandler(context=ssl_context)
)
urllib.request.install_opener(opener)

API_URL = "https://app.tuntivelho.com/tvv-mobile/backend/public/graphql"


# --- Tasker Machine-Readable Output ---

def emit_status(status, action, message=""):
    """Print TV_STATUS and TV_RESULT lines for Tasker, then flush stdout."""
    epoch = int(time.time())
    print(f"TV_STATUS={status}")
    print(f"TV_RESULT={status}|{action}|{epoch}|{message}")
    sys.stdout.flush()


# --- GraphQL Queries ---

LOGIN_MUTATION = """
mutation login($kayttajatunnus: String!, $salasana: String!, $subuser: Boolean, $kieliid: Int) {
  login(kayttajatunnus: $kayttajatunnus, salasana: $salasana, subuser: $subuser, kieliid: $kieliid) {
    henkiloid
    success
    nimi
    token
    errors { message }
  }
}
"""

# Fetch the user's timecard state + defaults for talaatuid/tyopisteid
KELLOKORTTI_QUERY = """
query kellokortti($subuser: Int) {
  kellokortti(subuser: $subuser) {
    previousstamp {
      tv_leimaid
      aika
      suuntaid
    }
    selectiondefaults {
      talaatuid
      tyopisteid
    }
    talaadut {
      talaatuid
    }
  }
}
"""

# The actual punch mutation. $withStamps is used by the @include directive.
PUNCH_MUTATION = """
mutation leimaTallenna($input: LeimaInput!, $subuser: Int, $withStamps: Boolean = true) {
  leimaTallenna(input: $input, subuser: $subuser) {
    previousstamp {
      tv_leimaid
      aika
      suuntaid
      talaatuid
      tyopisteid
      tyolajiid
    }
    previousstamps @include(if: $withStamps) {
      henkiloid
      tv_leimaid
      aika
      suuntaid
    }
    selectiondefaults {
      talaatuid
      tyopisteid
      tyolajiid
    }
    errors {
      message
    }
  }
}
"""

# --- Core Functions ---

def graphql_request(query, variables, token=None):
    """Send a GraphQL request. Backend requires array-wrapped payloads.

    Raises RuntimeError with a short reason on HTTP or connection errors
    so callers can emit machine-readable status before exiting.
    """
    payload = [{"query": query, "variables": variables}]
    data = json.dumps(payload).encode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Origin": "https://app.tuntivelho.com"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(API_URL, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            res_array = json.loads(response.read().decode('utf-8'))
            return res_array[0]
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        print(f"HTTP Error {e.code}:\n{body}")
        raise RuntimeError(f"http_error_{e.code}")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        print(f"Connection Error: {e}")
        raise RuntimeError("connection_error")
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Unexpected response format: {e}")
        raise RuntimeError("parse_error")


def login(username, password):
    try:
        res = graphql_request(LOGIN_MUTATION, {
            "kayttajatunnus": username,
            "salasana": password,
            "subuser": False,
            "kieliid": 1
        })
    except RuntimeError as e:
        emit_status("ERROR", "LOGIN", str(e))
        sys.exit(1)
    data = res.get('data', {}).get('login', {})
    if data.get('success'):
        print(f"Logged in successfully as: {data.get('nimi')}")
        return data.get('token'), data.get('henkiloid')
    else:
        print("Login failed:", data.get('errors'))
        emit_status("ERROR", "LOGIN", "login_failed")
        sys.exit(1)


def get_defaults(token):
    """Fetch the user's kellokortti to get default talaatuid & tyopisteid."""
    res = graphql_request(KELLOKORTTI_QUERY, {"subuser": None}, token)
    errors = res.get('errors', [])
    if errors:
        print(f"Warning: kellokortti query returned errors: {errors}")

    kk = res.get('data', {}).get('kellokortti', {})
    defaults = kk.get('selectiondefaults', {})
    prev = kk.get('previousstamp', {})

    talaatuid = defaults.get('talaatuid')
    tyopisteid = defaults.get('tyopisteid')

    print(f"  Default talaatuid: {talaatuid}")
    print(f"  Default tyopisteid: {tyopisteid}")

    if prev:
        direction = "IN" if prev.get('suuntaid') == 0 else "OUT"
        print(f"  Last punch was: {direction}")

    return talaatuid, tyopisteid


def do_punch(token, action, talaatuid, tyopisteid, dry_run=False):
    """
    Punch IN or OUT.
    
    LeimaInput fields (reverse-engineered from JS bundle):
      - tapahtuma: "sisaan" | "ulos" | "tauolle" | "tauolta"
      - talaatuid: int (work quality / type)
      - tyopisteid: int (workplace)
      - leimausaika: optional timestamp
    """
    tapahtuma = "sisaan" if action == "punch_in" else "ulos"
    direction_name = "IN" if action == "punch_in" else "OUT"

    input_obj = {
        "tietoja": "",
        "talaatuid": talaatuid,
        "tyopisteid": tyopisteid,
        "tyolajiid": None,
        "polaatuid": None,
        "leimausaika": int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
        "tapahtuma": tapahtuma,
    }

    variables = {
        "input": input_obj,
        "subuser": None,
        "withStamps": True
    }

    if dry_run:
        print(f"\n[DRY RUN] Would punch {direction_name} with payload:")
        print(json.dumps(variables, indent=2))
        print("[DRY RUN] Remove --dry-run flag to actually stamp your timecard.")
        emit_status("DRYRUN", direction_name, "dry_run")
        return

    print(f"Sending stamp {direction_name} request...")
    try:
        res = graphql_request(PUNCH_MUTATION, variables, token=token)
    except RuntimeError as e:
        emit_status("ERROR", direction_name, str(e))
        sys.exit(1)

    errors = res.get("errors", [])
    leima_errors = (res.get("data", {}) or {}).get("leimaTallenna", {})
    leima_errors = (leima_errors or {}).get("errors", [])

    if errors or leima_errors:
        print(f"Punch {direction_name} Failed!")
        if errors:
            print(json.dumps(errors, indent=2))
        if leima_errors:
            print(json.dumps(leima_errors, indent=2))
        reason = "graphql_error" if errors else "leima_error"
        emit_status("ERROR", direction_name, reason)
        sys.exit(1)
    else:
        print(f"Punch {direction_name} Successful!")
        stamp = (res.get("data", {}) or {}).get("leimaTallenna", {}).get("previousstamp", {})
        stamp_id = stamp.get('tv_leimaid', '') if stamp else ''
        if stamp:
            print(f"  Stamp ID: {stamp_id}")
            print(f"  Time: {stamp.get('aika')}")
        emit_status("OK", direction_name, f"stamp_id={stamp_id}")


def main():
    parser = argparse.ArgumentParser(description="Tuntiwelho Auto-Puncher")
    parser.add_argument("--username", default=os.environ.get("TW_USER"),
                        help="Tuntiwelho username (or set TW_USER env var)")
    parser.add_argument("--password", default=os.environ.get("TW_PASS"),
                        help="Tuntiwelho password (or set TW_PASS env var)")
    parser.add_argument("--action", choices=["test_login", "punch_in", "punch_out"],
                        required=True, help="Action to perform")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log in, print payload, but do NOT actually punch")

    args = parser.parse_args()
    if not args.username or not args.password:
        parser.error("Credentials required: use --username/--password or set TW_USER/TW_PASS env vars")
    direction_name = "IN" if args.action == "punch_in" else (
        "OUT" if args.action == "punch_out" else "LOGIN"
    )

    # Step 1: Login (also establishes session cookie)
    token, henkiloid = login(args.username, args.password)

    if args.action == "test_login":
        print("Login Test Passed! Exiting without punching.")
        emit_status("OK", "LOGIN", "ok")
        sys.exit(0)

    # Step 2: Fetch defaults
    print("Fetching user defaults...")
    try:
        talaatuid, tyopisteid = get_defaults(token)
    except RuntimeError as e:
        emit_status("ERROR", direction_name, str(e))
        sys.exit(1)

    # Step 3: Punch
    do_punch(token, args.action, talaatuid, tyopisteid, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
