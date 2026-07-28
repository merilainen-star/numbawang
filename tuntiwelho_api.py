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

Environment variables:
  TW_USER / TW_PASS  -> credentials
  TW_API_URL         -> optional GraphQL endpoint override
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

# Backend endpoint moved from /tvv-mobile to /mobiili in Mar 2026.
# Keep both for compatibility; try new one first.
DEFAULT_API_URLS = [
    "https://app.tuntivelho.com/mobiili/backend/public/graphql",
    "https://app.tuntivelho.com/tvv-mobile/backend/public/graphql",
]


def get_api_urls():
    """Return API endpoint candidates (env override first)."""
    env_url = os.environ.get("TW_API_URL")
    urls = []
    if env_url:
        urls.append(env_url.strip())
    urls.extend(DEFAULT_API_URLS)

    # Preserve order while dropping duplicates/empties.
    deduped = []
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped

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


def _sanitize_error_message(msg):
    """Make an API error message safe for the pipe-delimited TV_RESULT line.

    Collapses whitespace/newlines and replaces '|' (the TV_RESULT field
    separator) so an API message can never split TV_RESULT into extra
    fields or break Tasker's parsing.
    """
    if not msg:
        return ""
    cleaned = " ".join(str(msg).split())
    return cleaned.replace("|", "/")


def _extract_api_error_message(errors, leima_errors, fallback):
    """Build a human-readable error message from GraphQL/leimaTallenna errors.

    leimaTallenna errors (e.g. "Suunta ei ole sallittu. Olet ehkä leimannut
    jo ulos.") are the specific, user-facing reason for a failed punch, so
    they take priority; top-level GraphQL errors are appended if present.
    Falls back to `fallback` when neither source has a usable message.
    """
    messages = []
    for err_list in (leima_errors, errors):
        for err in (err_list or []):
            msg = _sanitize_error_message(err.get("message") if isinstance(err, dict) else None)
            if msg and msg not in messages:
                messages.append(msg)
    return "; ".join(messages) if messages else fallback


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

    api_urls = get_api_urls()

    for idx, api_url in enumerate(api_urls):
        req = urllib.request.Request(api_url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                res_array = json.loads(response.read().decode('utf-8'))
                return res_array[0]
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            should_try_fallback = (e.code == 404 and idx < len(api_urls) - 1)
            if should_try_fallback:
                print(f"HTTP 404 from {api_url} - trying fallback endpoint...")
                continue
            print(f"HTTP Error {e.code} ({api_url}):\n{body}")
            raise RuntimeError(f"http_error_{e.code}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if idx < len(api_urls) - 1:
                print(f"Connection error from {api_url} - trying fallback endpoint...")
                continue
            print(f"Connection Error ({api_url}): {e}")
            raise RuntimeError("connection_error")
        except (json.JSONDecodeError, IndexError) as e:
            if idx < len(api_urls) - 1:
                print(f"Unexpected response from {api_url} - trying fallback endpoint...")
                continue
            print(f"Unexpected response format ({api_url}): {e}")
            raise RuntimeError("parse_error")

    # Defensive fallback: loop should have returned or raised already.
    raise RuntimeError("http_error_404")


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
    """Fetch the user's kellokortti to get default talaatuid & tyopisteid.

    Also returns the previousstamp dict (may be None/empty) so it can be
    used as a fallback punch-in timestamp when the punch mutation does not
    return a populated previousstamps list.
    """
    res = graphql_request(KELLOKORTTI_QUERY, {"subuser": None}, token)
    errors = res.get('errors', [])
    if errors:
        print(f"Warning: kellokortti query returned errors: {errors}")

    kk = res.get('data', {}).get('kellokortti', {})
    defaults = kk.get('selectiondefaults', {})
    prev = kk.get('previousstamp') or {}

    talaatuid = defaults.get('talaatuid')
    tyopisteid = defaults.get('tyopisteid')

    print(f"  Default talaatuid: {talaatuid}")
    print(f"  Default tyopisteid: {tyopisteid}")

    if prev:
        direction = "IN" if prev.get('suuntaid') == 0 else "OUT"
        print(f"  Last punch was: {direction}")

    # Return prev so do_punch() can use it as a fallback IN-stamp
    return talaatuid, tyopisteid, prev or None


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


def _find_punch_in_time(previousstamps, out_aika=None):
    """Find the most recent punch-in stamp (suuntaid==0) before the punch-out.

    previousstamps is a list of dicts with 'aika' (int) and 'suuntaid'.

    IMPORTANT — API timestamp encoding:
    The Tuntivelho API stores 'aika' as local-clock-time encoded as if it were
    a UTC epoch (i.e. aika = local_wall_clock_time_as_utc_epoch).  On a device
    set to EET (UTC+2) the values come out ~7200 s AHEAD of time.time().
    Using datetime.fromtimestamp(aika) on such a device therefore adds the
    timezone offset a second time, making the IN stamp appear to be ~2 h in
    the future and causing the cutoff check to discard it.

    Solution: compare aika integers directly against each other (both share the
    same encoding so their difference equals the actual elapsed seconds), and
    recover the display datetime with datetime.utcfromtimestamp(aika) which does
    NOT apply a local-timezone shift and thus yields the correct local clock time.

    Returns a naive datetime (local clock face time) for the best IN stamp, or None.
    """
    if not previousstamps:
        return None

    max_lookback_s = 86400   # 24 h in seconds; same scale as aika integers

    best_aika = None
    for stamp in previousstamps:
        if stamp.get('suuntaid') != 0:
            continue
        aika = stamp.get('aika')
        if aika is None:
            continue
        try:
            aika_int = int(aika)
            if aika_int > 1e12:      # milliseconds → convert to seconds
                aika_int //= 1000
        except (ValueError, TypeError):
            continue

        # Compare aika integers directly to stay in the API's encoding space.
        if out_aika is not None:
            if aika_int >= out_aika:
                continue   # stamp is at or after the OUT → skip
            if out_aika - aika_int > max_lookback_s:
                continue   # stamp is older than 24 h → skip

        if best_aika is None or aika_int > best_aika:
            best_aika = aika_int

    if best_aika is None:
        print("  Debug previousstamps (suuntaid / aika):")
        for s in previousstamps:
            print(f"    suuntaid={s.get('suuntaid')} aika={s.get('aika')}")
        return None

    # utcfromtimestamp correctly decodes the API's local-as-UTC encoding:
    # it returns a naive datetime whose H:MM matches the original local clock time.
    try:
        return datetime.utcfromtimestamp(best_aika)
    except (ValueError, OSError):
        return None


def _calc_daily_delta(punch_in_dt, punch_out_dt):
    """Calculate today's balance delta in minutes.

    Subtracts LUNCH_BREAK_MINUTES and compares to TARGET_WORKDAY_MINUTES.
    Returns signed minutes (positive = over target).
    """
    elapsed = (punch_out_dt - punch_in_dt).total_seconds() / 60.0
    worked = elapsed - LUNCH_BREAK_MINUTES
    return int(round(worked - TARGET_WORKDAY_MINUTES))


def do_punch(token, action, talaatuid, tyopisteid, dry_run=False,
             prev_stamp_from_defaults=None):
    """
    Punch IN or OUT.

    prev_stamp_from_defaults: the previousstamp dict returned by get_defaults()
        (fields: tv_leimaid, aika, suuntaid).  Used as a fallback IN-timestamp
        when the punch mutation response's previousstamps list is empty — which
        happens when the API omits the @include(if: $withStamps) data.
    
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
        "leimausaika": int(datetime.now().timestamp()),
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
        message = _extract_api_error_message(
            errors, leima_errors, fallback="Leimaus epäonnistui (syytä ei saatu API:lta)"
        )
        emit_status("ERROR", direction_name, message)
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
            # DEBUG: show raw stamp count and contents so we can diagnose
            # whether the API is populating previousstamps at all.
            print(f"  Debug: previousstamps count = {len(previousstamps)}")
            for s in previousstamps[:10]:  # cap at 10 to avoid spam
                print(f"    suuntaid={s.get('suuntaid')} aika={s.get('aika')}")

            # Fallback: if the mutation returned no previousstamps but we
            # captured the previousstamp (singular) from get_defaults() earlier
            # AND it was a punch-IN (suuntaid == 0), synthesise a one-item list
            # from it so _find_punch_in_time() can still compute the delta.
            if not previousstamps and prev_stamp_from_defaults:
                psd = prev_stamp_from_defaults
                if psd.get('suuntaid') == 0:
                    print("  Debug: previousstamps empty — using kellokortti previousstamp as fallback IN")
                    previousstamps = [psd]
                else:
                    print(f"  Debug: previousstamps empty and kellokortti previousstamp is not IN (suuntaid={psd.get('suuntaid')}) — cannot compute delta")

            # Use the OUT stamp's raw aika for integer comparison against
            # previousstamps (keeps us in the API's encoding space).
            out_aika_raw = stamp.get('aika') if stamp else None
            print(f"  Debug: out stamp aika (raw) = {out_aika_raw}")
            try:
                out_aika_int = int(out_aika_raw) if out_aika_raw is not None else None
                if out_aika_int and out_aika_int > 1e12:
                    out_aika_int //= 1000
            except (ValueError, TypeError):
                out_aika_int = None
            print(f"  Debug: out_aika_int = {out_aika_int}")
            punch_in_dt = _find_punch_in_time(previousstamps, out_aika_int)
            # Derive punch_out_dt in the same encoding space for delta arithmetic
            if out_aika_int:
                try:
                    punch_out_dt = datetime.utcfromtimestamp(out_aika_int)
                except (ValueError, OSError):
                    punch_out_dt = datetime.now()
            else:
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

    # Step 2: Fetch defaults (also captures the previous stamp for fallback)
    print("Fetching user defaults...")
    try:
        talaatuid, tyopisteid, prev_stamp = get_defaults(token)
    except RuntimeError as e:
        emit_status("ERROR", direction_name, str(e))
        sys.exit(1)

    # Step 3: Punch
    do_punch(token, args.action, talaatuid, tyopisteid, dry_run=args.dry_run,
             prev_stamp_from_defaults=prev_stamp)


if __name__ == "__main__":
    main()
