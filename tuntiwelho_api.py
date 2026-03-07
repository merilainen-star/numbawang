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
from datetime import datetime, timezone, timedelta
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

# --- Workday Constants ---
TARGET_WORKDAY_MINUTES = 7 * 60 + 30   # 7 h 30 min
LUNCH_BREAK_MINUTES = 30               # automatic lunch deduction


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

# Query to fetch the employee's working time balance after punching
BALANCE_QUERY = """
query kellokortti($subuser: Int) {
  kellokortti(subuser: $subuser) {
    tase {
      tase
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


def fetch_balance(token):
    """Fetch the employee's working time balance (Tase) from kellokortti.

    The API returns the balance in seconds. This function converts it
    to a human-readable HH:MM string (e.g. '27:59' or '-02:30').
    Returns empty string if the balance cannot be fetched.
    """
    try:
        res = graphql_request(BALANCE_QUERY, {"subuser": None}, token)
        tase_obj = (res.get('data') or {}).get('kellokortti', {}).get('tase')
        if not tase_obj:
            return ""
        tase_seconds = tase_obj.get('tase')
        if tase_seconds is None:
            return ""
        sign = "-" if tase_seconds < 0 else ""
        total = abs(int(tase_seconds))
        hours = total // 3600
        minutes = (total % 3600) // 60
        return f"{sign}{hours}:{minutes:02d}"
    except Exception:
        return ""


# --- Balance Delta Helpers ---

def _minutes_to_signed_hhmm(total_minutes):
    """Convert a signed minute value to '+H:MM' or '-H:MM' string."""
    sign = "-" if total_minutes < 0 else "+"
    abs_min = abs(total_minutes)
    h = abs_min // 60
    m = abs_min % 60
    return f"{sign}{h}:{m:02d}"


def _balance_str_to_minutes(balance_str):
    """Convert a balance string like '27:59' or '-2:30' to signed minutes.

    Accepts formats: '27:59', '-2:30', '+3:15', '0:45'.
    Returns None if parsing fails.
    """
    if not balance_str:
        return None
    try:
        negative = balance_str.startswith("-")
        cleaned = balance_str.lstrip("+-")
        parts = cleaned.split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        total = h * 60 + m
        return -total if negative else total
    except (ValueError, IndexError):
        return None


def _find_punch_in_time(previousstamps):
    """Find the earliest punch-in timestamp (suuntaid==0) for today.

    previousstamps is a list of dicts with 'aika' (epoch) and 'suuntaid'.
    Returns a datetime in local time, or None.
    """
    if not previousstamps:
        return None
    today = datetime.now().date()
    earliest = None
    for stamp in previousstamps:
        if stamp.get('suuntaid') != 0:
            continue
        aika = stamp.get('aika')
        if aika is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(aika))
        except (ValueError, TypeError, OSError):
            continue
        if dt.date() != today:
            continue
        if earliest is None or dt < earliest:
            earliest = dt
    return earliest


def _calc_daily_delta(punch_in_dt, punch_out_dt):
    """Calculate today's balance delta in minutes.

    Subtracts LUNCH_BREAK_MINUTES and compares to TARGET_WORKDAY_MINUTES.
    Returns signed minutes (positive = over target).
    """
    elapsed = (punch_out_dt - punch_in_dt).total_seconds() / 60.0
    worked = elapsed - LUNCH_BREAK_MINUTES
    return int(round(worked - TARGET_WORKDAY_MINUTES))


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
        leima_data = (res.get("data", {}) or {}).get("leimaTallenna", {}) or {}
        stamp = leima_data.get("previousstamp", {})
        stamp_id = stamp.get('tv_leimaid', '') if stamp else ''
        if stamp:
            print(f"  Stamp ID: {stamp_id}")
            print(f"  Time: {stamp.get('aika')}")

        # Fetch previous balance from API (non-fatal if it fails)
        balance = fetch_balance(token)
        if balance:
            print(f"  Balance (API): {balance}")

        # Build extra key=value fields for Tasker
        extra_parts = [f"stamp_id={stamp_id}"]
        now_time_str = datetime.now().strftime("%H:%M")

        if action == "punch_out":
            # --- Calculate today's daily delta ---
            previousstamps = leima_data.get("previousstamps", []) or []
            punch_in_dt = _find_punch_in_time(previousstamps)
            punch_out_dt = datetime.now()
            delta_str = ""
            est_balance_str = ""

            if punch_in_dt:
                delta_minutes = _calc_daily_delta(punch_in_dt, punch_out_dt)
                delta_str = _minutes_to_signed_hhmm(delta_minutes)
                print(f"  Punch-in: {punch_in_dt.strftime('%H:%M')}")
                print(f"  Punch-out: {punch_out_dt.strftime('%H:%M')}")
                print(f"  Daily delta: {delta_str}")
                extra_parts.append(f"delta={delta_str}")

                # Estimated total balance = previous API balance + today's delta
                if balance:
                    prev_minutes = _balance_str_to_minutes(balance)
                    if prev_minutes is not None:
                        est_total = prev_minutes + delta_minutes
                        est_balance_str = _minutes_to_signed_hhmm(est_total)
                        extra_parts.append(f"est_balance={est_balance_str}")
                        print(f"  Est. total balance: {est_balance_str}")
            else:
                print("  Warning: could not find today's punch-in time")

            # Also include raw balance from API if available
            if balance:
                extra_parts.append(f"balance={balance}")

            # Build display string: ULOS 16:20 +0:24 (+24:15)
            display = f"ULOS {now_time_str}"
            if delta_str:
                display += f" {delta_str}"
                if est_balance_str:
                    display += f" ({est_balance_str})"
            elif balance:
                display += f" ({balance})"
            extra_parts.append(f"display={display}")

        elif action == "punch_in":
            # For punch in, show previous known balance if available
            if balance:
                extra_parts.append(f"balance={balance}")
            display = f"SISÄÄN {now_time_str}"
            if balance:
                display += f" ({balance})"
            extra_parts.append(f"display={display}")

        emit_status("OK", direction_name, "|".join(extra_parts))


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
