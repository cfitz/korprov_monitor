#!/usr/bin/env python3
"""
Trafikverket Körprov Slot Monitor
===================================
Polls the Trafikverket booking API for available körprov slots and sends
a Telegram alert when one appears before SEARCH_UNTIL_DATE.

Setup:
  uv sync
  uv run playwright install firefox

Configuration:
  0. NO ASSHOLES
  1. Fill in PERSONNUMMER (required)
  2. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (optional — see README)
  3. Set SEARCH_UNTIL_DATE to the latest date you're willing to book
  4. Uncomment any LOCATION_BATCHES you want to monitor
  5. Run: uv run python korprov_monitor.py

To find LICENCE_ID / EXAMINATION_TYPE_ID / VEHICLE_TYPE_ID for a different
exam type: log in to fp.trafikverket.se/Boka/, open DevTools → Network,
make a search, and inspect the POST to `occasion-bundles`.
"""

import re
import time
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

# Your Swedish personnummer
PERSONNUMMER = "19XXXXXX-XXXX"

# Telegram — create a bot with @BotFather, then message it to get your chat ID:
#   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# ── Cookie & session token ────────────────────────────────────────────────────
# Leave these empty — the script will log in automatically via BankID on startup
# and refresh them whenever the session expires.
# You can optionally paste a fresh cookie here to skip the first BankID prompt.
COOKIE = ""
SESSION_TOKEN = ""

# ── Exam config ───────────────────────────────────────────────────────────────
# There are values for MC A - Driving Test
LICENCE_ID = 4
EXAMINATION_TYPE_ID = 10
VEHICLE_TYPE_ID = 1

# How often to check (seconds). Keep this to a sane number
CHECK_INTERVAL_SECONDS = 60

# Only alert on slots up to and including this date (YYYY-MM-DD), or None for no limit
SEARCH_UNTIL_DATE = "2026-07-07"

# ─── LOCATION BATCHES ─────────────────────────────────────────────────────────
# The API accepts 1 primary + up to 3 nearby locations per request (4 total).
# Each batch = one API call. Uncomment batches you want to monitor.
# examinationCategories: 1 = theory/car/MC, 2 = MC track (manöverbana)
#
# All location IDs verified from live API response.
#
LOCATION_BATCHES = [
    # ── Stockholm ───────────────────────────────────
    (
        1000071,
        "Uppsala",
        [1000005, 1000038, 1000118],
        ["Eskilstuna", "Västerås", "Gävle"],
    ),
    (
        1000326,
        "Järfälla",
        [1000019, 1000132, 1000140],
        ["Farsta", "Södertälje", "Stockholm City"],
    ),
    # ── Norrland ─────────────────────────────────────────────────────────────
    (
        1000118,
        "Gävle",
        [1000121, 1000056],
        ["Bollnäs", "Hudiksvall"],
    ),
    (
        1000105,
        "Sundsvall",
        [1000057, 1000106, 1000112],
        ["Härnösand", "Örnsköldsvik", "Östersund"],
    ),
    # (1000021, "Umeå",            [1000111, 1000087, 1000082], ["Skellefteå", "Piteå", "Luleå"]),
    # (1000082, "Luleå",           [1000083, 1000085, 1000084], ["Boden", "Kalix", "Haparanda"]),
    # (1000092, "Kiruna",          [1000090, 1000086, 1000089], ["Gällivare", "Pajala", "Övertorneå"]),
    # (1000022, "Lycksele",        [1000027, 1000109, 1000091], ["Vilhelmina", "Arvidsjaur", "Jokkmokk"]),
    # ── Dalarna / Mellansverige ───────────────────────────────────────────────
    # (1000098, "Falun",           [1000039, 1000040, 1000038], ["Fagersta", "Köping", "Västerås"]),
    # ── Östergötland / Småland ────────────────────────────────────────────────
    # ( 1000009, "Linköping", [1000329, 1000011, 1000149], ["Norrköping", "Motala", "Nyköping"], ),
    # (1000074, "Jönköping",       [1000078, 1000028, 1000030], ["Vetlanda", "Ljungby", "Växjö"]),
    # (1000093, "Kalmar",          [1000094, 1000095, 1000015], ["Oskarshamn", "Västervik", "Vimmerby"]),
    # (1000047, "Karlskrona",      [1000046, 1000064, 1000061], ["Kristianstad", "Ystad", "Malmö"]),
    # (1000318, "Värnamo",         [1000028, 1000074, 1000030], ["Ljungby", "Jönköping", "Växjö"]),
    # ── Västsverige ───────────────────────────────────────────────────────────
    # (1000325, "Göteborg Högsbo", [1000143, 1000339, 1000066], ["Göteborg-Hisingen", "Göteborg Öst", "Borås"]),
    # (1000127, "Vänersborg",      [1000130, 1000069, 1000129], ["Skövde", "Lidköping", "Mariestad"]),
    # (1000059, "Halmstad",        [1000060, 1000322, 1000066], ["Falkenberg", "Varberg", "Borås"]),
    # ── Värmland / Örebro ─────────────────────────────────────────────────────
    # (1000031, "Karlstad",        [1000036, 1000001, 1000005], ["Sunne", "Örebro", "Eskilstuna"]),
    # ── Skåne ─────────────────────────────────────────────────────────────────
    # (1000061, "Malmö",           [1000062, 1000123, 1000046], ["Lund", "Helsingborg", "Kristianstad"]),
    # (1000122, "Ängelholm",       [1000123, 1000046, 1000064], ["Helsingborg", "Kristianstad", "Ystad"]),
    # ── Övrigt ────────────────────────────────────────────────────────────────
    # (1000097, "Visby", [], []),
]

# ─── END CONFIGURATION ────────────────────────────────────────────────────────

# ─── SESSION REFRESH ──────────────────────────────────────────────────────────

_session_expired = False  # set True when login error detected


def refresh_session():
    """
    Opens a headless browser, shows a BankID QR code via explorer.exe,
    waits for approval, then extracts fresh cookies and session token.
    Raises TimeoutError if not approved within 2 minutes.
    """
    global COOKIE, SESSION_TOKEN

    print("\n🔐 Starting BankID session refresh...")

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://fp.trafikverket.se/Boka/")
        page.wait_for_load_state("networkidle")

        try:
            page.locator(
                "button:has-text('Godkänn nödvändiga'), button:has-text('Godkänn alla')"
            ).first.click(timeout=5000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass

        # Click Logga in, then Fortsätt (Mobilt BankID is pre-selected) to get QR code
        page.locator("button:has-text('Logga in'), a:has-text('Logga in')").first.click(
            timeout=10000
        )
        page.locator("button:has-text('Fortsätt')").first.click(timeout=10000)
        page.wait_for_timeout(2000)

        qr_path = "/tmp/bankid_qr.png"
        page.screenshot(path=qr_path)

        # Open in Windows — keep overwriting so Photos auto-refreshes
        try:
            import subprocess

            win_path = (
                subprocess.check_output(["wslpath", "-w", qr_path]).decode().strip()
            )
            subprocess.Popen(
                ["explorer.exe", win_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"  📱 QR code opened in Windows — scan it with your BankID app.")
        except Exception:
            print(f"  📱 Scan the BankID QR code at: {qr_path}")

        print("  Waiting up to 2 minutes...")

        deadline = time.time() + 120
        last_url = ""
        while time.time() < deadline:
            current = page.url
            if current != last_url:
                print(f"    URL: {current}")
                last_url = current
            try:
                if not page.locator(
                    "button:has-text('Logga in'), a:has-text('Logga in')"
                ).is_visible():
                    break
            except Exception:
                pass
            page.screenshot(path=qr_path)
            time.sleep(1)
        else:
            page.screenshot(path="/tmp/bankid_debug_final.png")
            print(f"  Debug screenshot: /tmp/bankid_debug_final.png")
            browser.close()
            raise TimeoutError("BankID approval timed out after 2 minutes")

        print("  ✅ BankID approved — extracting session...")

        # Navigate into the booking flow to generate a session token URL
        try:
            page.locator("text=Boka prov").first.click(timeout=10000)
            page.wait_for_url("**/Boka/ng/search/**", timeout=15000)
        except Exception:
            pass

        cookies = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        current_url = page.url
        token_match = re.search(r"/search/([^/]+)/", current_url)
        new_token = token_match.group(1) if token_match else SESSION_TOKEN

        browser.close()

        COOKIE = cookie_str
        SESSION_TOKEN = new_token
        print(f"  Session refreshed. Token: {new_token[:10]}...")
        return cookie_str, new_token


def handle_session_expired(notify=True):
    """Called when the API returns a session expiry error."""
    global _session_expired
    if _session_expired:
        return  # already handling it
    _session_expired = True

    if TELEGRAM_BOT_TOKEN and notify:
        try:
            send_telegram(
                "🔐 Trafikverket session expired — scan the BankID QR code to resume monitoring."
            )
        except Exception as e:
            print(f"  Could not send Telegram message: {e}")

    try:
        refresh_session()
        _session_expired = False
        print("  ✅ Session refreshed — resuming monitoring.\n")
    except Exception as e:
        print(f"  ❌ Session refresh failed: {e}")
        _session_expired = False


def make_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json; charset=utf-8",
        "Origin": "https://fp.trafikverket.se",
        "Referer": f"https://fp.trafikverket.se/Boka/ng/search/{SESSION_TOKEN}/{LICENCE_ID}/0/0/0",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": COOKIE,
    }


def send_telegram(message: str):
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=10,
    )
    resp.raise_for_status()
    print("[Telegram sent]")


def check_batch(session, primary_id, primary_name, nearby_ids, nearby_names):
    """One API call covering primary + up to 3 nearby locations."""
    date_from = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    all_names = [primary_name] + nearby_names

    payload = {
        "bookingSession": {
            "socialSecurityNumber": PERSONNUMMER,
            "licenceId": LICENCE_ID,
            "bookingModeId": 0,
            "ignoreDebt": False,
            "ignoreBookingHindrance": False,
            "examinationTypeId": EXAMINATION_TYPE_ID,
            "excludeExaminationCategories": [],
            "rescheduleTypeId": 0,
            "paymentIsActive": False,
            "paymentReference": "",
            "paymentUrl": "",
            "searchedMonths": 0,
        },
        "occasionBundleQuery": {
            "startDate": date_from,
            "searchedMonths": 0,
            "locationId": primary_id,
            "nearbyLocationIds": nearby_ids,
            "languageId": 13,
            "vehicleTypeId": VEHICLE_TYPE_ID,
            "tachographTypeId": 1,
            "occasionChoiceId": 0,
            "examinationTypeId": EXAMINATION_TYPE_ID,
        },
    }

    try:
        resp = session.post(
            "https://fp.trafikverket.se/Boka/occasion-bundles",
            json=payload,
            headers=make_headers(),
            timeout=15,
        )
        if "text/html" in resp.headers.get("Content-Type", ""):
            print(
                f"  [{primary_name}] ⚠️  Session expired — attempting auto-refresh..."
            )
            handle_session_expired()
            return []
        if not resp.ok:
            # Check for login-required error in JSON body
            try:
                err = resp.json()
                if err.get("type") == "LoginRequiredException":
                    print(
                        f"  [{primary_name}] ⚠️  Session expired — attempting auto-refresh..."
                    )
                    handle_session_expired()
                    return []
            except Exception:
                pass
            print(f"  [{primary_name}] HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        bundles = resp.json().get("data", {}).get("bundles", [])
        # Filter by date — date is nested inside occasions[0]
        if SEARCH_UNTIL_DATE:
            bundles = [
                b
                for b in bundles
                if (b.get("occasions") or [{"date": "9999-12-31"}])[0].get(
                    "date", "9999-12-31"
                )
                <= SEARCH_UNTIL_DATE
            ]

        if bundles:
            print(f"  ✅ {' / '.join(all_names)}: {len(bundles)} slot(s) found")
        else:
            print(
                f"  ❌ {' / '.join(all_names)}: no slots (or none before {SEARCH_UNTIL_DATE})"
            )
        return bundles
    except Exception as e:
        print(f"  [{primary_name}] Error: {e}")
        return []


def format_slot(bundle: dict) -> str:
    occ = (bundle.get("occasions") or [{}])[0]
    location = occ.get("locationName", "?")
    date = occ.get("date", "?")
    time_ = occ.get("time", "?")
    return f"{location} — {date} {time_}"


def run_monitor():
    all_names = []
    for b in LOCATION_BATCHES:
        all_names.append(b[1])
        all_names.extend(b[3])

    print("=" * 55)
    print("  Trafikverket MC Körprov Monitor (A – full license)")
    print(f"  {len(LOCATION_BATCHES)} batch(es), {len(all_names)} location(s)")
    print(f"  Interval: {CHECK_INTERVAL_SECONDS}s")
    print("=" * 55)

    if not TELEGRAM_BOT_TOKEN:
        print("\n⚠️  Telegram not configured — alerts will NOT be sent.\n")
    if PERSONNUMMER == "19XXXXXX-XXXX":
        print("⚠️  PERSONNUMMER not set — please fill it in before running.\n")
        return

    if not COOKIE:
        print("No session cookie found — starting initial BankID login...\n")
        handle_session_expired(notify=False)

    session = requests.Session()
    notified_slots = set()

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now}] Checking {len(LOCATION_BATCHES)} batch(es)...")

        found_messages = []

        for primary_id, primary_name, nearby_ids, nearby_names in LOCATION_BATCHES:
            bundles = check_batch(
                session, primary_id, primary_name, nearby_ids, nearby_names
            )
            for bundle in bundles:
                occ = (bundle.get("occasions") or [{}])[0]
                slot_key = (
                    f"{occ.get('locationId')}-{occ.get('date')}-{occ.get('time')}"
                )
                if slot_key not in notified_slots:
                    found_messages.append(format_slot(bundle))
                    notified_slots.add(slot_key)

        if found_messages:
            message = (
                "🏍️ MC körprov slot(s) available!\n\n"
                + "\n".join(found_messages)
                + "\n\nBook: https://fp.trafikverket.se/Boka/"
            )
            if TELEGRAM_BOT_TOKEN:
                send_telegram(message)
            else:
                print("\n[Alert preview]:", message)

        print(f"  Next check in {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_monitor()
    except KeyboardInterrupt:
        print("\n\nMonitor stopped.")
