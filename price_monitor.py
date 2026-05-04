"""
price_monitor.py
================
Car rental price monitor — any airport, any dates.

Checks Full Size SUV prices across 9 major providers using Playwright
browser automation. All providers use form-filling so any location works —
just update BOOKING["airport_code"] and the provider-specific location configs
below. Results are compared against a reference booking price and logged to CSV.

Usage:
    python price_monitor.py

Dependencies:
    pip install playwright
    playwright install chromium
"""

import argparse
import asyncio
import builtins
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass  # python-dotenv not installed — rely on env vars being set externally

try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    SupabaseClient = None  # type: ignore

try:
    from playwright_stealth import stealth_async as _stealth_async
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False
from typing import Dict, List, Optional, Set

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING — tee all output to a UTF-8 log file AND the console simultaneously.
#
# Why not sys.stdout redirection?
#   • On Windows the console codec (cp1252) is set at the OS level; wrapping
#     sys.stdout with TextIOWrapper breaks readline, pytest capture, and other
#     tools that also hold a reference to the original buffer.
#   • reconfigure() only works when stdout is a real terminal, not when piped.
#
# Solution: leave sys.stdout alone; define log() that writes directly to both
# a UTF-8 file handle and the console (replacing unencodable chars on the
# console but never in the file). Then shadow the built-in print with log so
# every existing print() call is automatically redirected.
# ─────────────────────────────────────────────────────────────────────────────

_LOG_PATH = Path(__file__).parent / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_log_fh: "Optional[io.TextIOWrapper]" = None


def _open_log() -> "io.TextIOWrapper":
    global _log_fh
    if _log_fh is None:
        _log_fh = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)
    return _log_fh


def log(*args, sep: str = " ", end: str = "\n", **_) -> None:
    """Write *args to both the UTF-8 log file and the console (tee style)."""
    text = sep.join(str(a) for a in args) + end
    # ── log file (always UTF-8, no lossy encoding) ──
    try:
        _open_log().write(text)
    except Exception:
        pass
    # ── console (replace unencodable chars so cp1252 consoles don't crash) ──
    try:
        builtins.print(text, end="", flush=True)
    except UnicodeEncodeError:
        safe = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        builtins.print(safe, end="", flush=True)


# Shadow the built-in so all existing print() calls in this module use log().
print = log  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit these before each run
# ─────────────────────────────────────────────────────────────────────────────

BOOKING = {
    "provider":      "SIXT",
    "car_class":     "Full Size SUV",
    "location":      "LaGuardia Airport, New York",
    "airport_code":  "LGA",
    "pickup_date":   "2026-05-02",
    "pickup_time":   "12:00",
    "return_date":   "2026-05-06",
    "return_time":   "12:00",
    "booked_price":  636.73,
    "driver_age":    31,
    # ── Kayak search filters ──────────────────────────────────────────────
    # Applied to Kayak URLs via _build_kayak_fs_param() using CONFIRMED WORKING params only.
    # See _build_kayak_fs_param() docstring for full list of working/broken params.
    "payment_type":       "PAY_LATER",   # "PREPAID" | "PAY_LATER" | None — informational only;
                                         # Kayak strips paymenttype= params (both prepay/postpay).
                                         # Shown as label in results table but NOT applied as filter.
    "free_cancellation":  True,          # True → carpolicies=cancel  (CONFIRMED WORKING)
    "unlimited_mileage":  False,         # True → unlimitedmileage=1
    "transmission":       "AUTOMATIC",   # "AUTOMATIC" | "MANUAL" | None — NOT applied (unverified)
    "min_passengers":     5,             # ≥5 → carcapacity=pas_5_6  (CONFIRMED WORKING; seats= broken)
    "ac_required":        True,          # informational only; Kayak has no A/C filter
}

MIN_SAVING = 15.00  # Only flag deals that save at least this amount

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE CONFIG — set via environment variables or replace with literal values
# ─────────────────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# LOCATIONS DATABASE — dynamic lookup
# ─────────────────────────────────────────────────────────────────────────────
# All provider-specific location data is stored in locations_db.json next to
# this file.  _db_lookup() reads it once (cached) and returns the first entry
# matching the given provider + airport_code, or None when not found.
#
# To support a new airport: add entries to locations_db.json — no code changes
# required.  Missing entries cause the affected provider to skip gracefully.

_LOCATIONS_DB_CACHE: Optional[List[Dict]] = None


def _db_lookup(provider: str, airport_code: str) -> Optional[Dict]:
    """Return the first locations_db.json entry matching provider + airport_code."""
    global _LOCATIONS_DB_CACHE
    if _LOCATIONS_DB_CACHE is None:
        db_path = Path(__file__).parent / "locations_db.json"
        try:
            _LOCATIONS_DB_CACHE = json.loads(
                db_path.read_text(encoding="utf-8")
            )["locations"]
        except Exception as exc:
            print(f"  [DB] Cannot load locations_db.json: {exc}")
            _LOCATIONS_DB_CACHE = []
    for entry in _LOCATIONS_DB_CACHE:
        if (entry.get("provider", "").upper() == provider.upper()
                and entry.get("airport_code", "").upper() == airport_code.upper()):
            return entry
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER-SPECIFIC LOCATION CONFIG
# ─────────────────────────────────────────────────────────────────────────────
# All lookups are driven by BOOKING["airport_code"].  When a provider has no
# entry in locations_db.json for the requested airport the constant is set to
# None and the corresponding check_*() function skips gracefully.

# SIXT — betafunnel deep-link parameters.
# UUID is NOT stored in the DB (it's session-specific).  check_sixt() calls
# the SelectLocation API at runtime to get a fresh location_selection_id UUID.
_sixt_loc = _db_lookup("SIXT", BOOKING["airport_code"])
if _sixt_loc:
    # Prefer "name" (clean plain text) over "title" (which may have + for spaces).
    # "title" is kept for backward-compat entries that only have that field.
    _sixt_title = (
        _sixt_loc.get("name") or
        _sixt_loc.get("title", "").replace("+", " ")
    ).strip()
    SIXT_LOCATION: Optional[Dict] = {
        "branch_id": f"BRANCH:{_sixt_loc['location_id']}",
        "title":     _sixt_title,
    }
else:
    SIXT_LOCATION = None
    print(f"  [DB] No SIXT entry for {BOOKING['airport_code']} — SIXT will be skipped.")


_SIXT_GRPC_HEADERS = {
    "Content-Type": "application/json",
    "Accept":       "application/json",
    "Origin":       "https://www.sixt.com",
    "Referer":      "https://www.sixt.com/car-rental/usa/",
    "User-Agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
_SIXT_GRPC_BASE = (
    "https://grpc-prod.orange.sixt.com/com.sixt.service.rent_booking.api"
)


def _sixt_api_call(service: str, method: str, body: dict) -> Optional[dict]:
    """POST to a SIXT gRPC-JSON endpoint; return parsed response or None."""
    import uuid as _uuid_mod  # noqa: F811  (already imported at module level)
    url = f"{_SIXT_GRPC_BASE}.{service}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=_SIXT_GRPC_HEADERS, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  [SIXT] {method} failed: {exc}")
        return None


def _sixt_select_location(branch_id: str) -> Optional[str]:
    """
    Call SelectLocation and return the session-specific location_selection_id UUID.
    This UUID is required as input to GetOfferRecommendationsV2.
    """
    import uuid as _uuid_mod
    data = _sixt_api_call("SearchService", "SelectLocation", {
        "user_profile_id":          "",
        "location_purpose":         1,
        "vehicle_type":             1,
        "auto_complete_session_id": str(_uuid_mod.uuid4()),
        "location_id":              branch_id,
        "include_fastlane":         None,
    })
    if not data:
        return None
    return data.get("location_selection_id") or None


def _sixt_get_offers(branch_id: str, pickup_dt: str, return_dt: str) -> Optional[list]:
    """
    Full SIXT pricing flow (no browser):
      1. SelectLocation → location_selection_id
      2. GetOfferRecommendationsV2 → list of offers with prices

    pickup_dt / return_dt format: "2026-05-02T12:00"  (no seconds)
    Returns the raw offers list, or None on failure.
    """
    import uuid as _uuid_mod
    loc_sel_id = _sixt_select_location(branch_id)
    if not loc_sel_id:
        return None
    data = _sixt_api_call("BookingService", "GetOfferRecommendationsV2", {
        "offer_matrix_id": str(_uuid_mod.uuid4()),   # client-generated random UUID
        "currency":        "USD",
        "trip_spec": {
            "pickup_datetime":              {"value": pickup_dt},
            "pickup_location_selection_id": loc_sel_id,
            "return_location_selection_id": loc_sel_id,
            "return_datetime":              {"value": return_dt},
            "vehicle_type":                 10,
            "user_profile_id":              "",
            "corporate_customer_number":    "",
            "campaign":                     "",
        },
        "enable_b2b_fallback": True,
    })
    if not data:
        return None
    return data.get("offers") or []


def _sixt_best_fullsize_suv(offers: list) -> Optional[Dict]:
    """
    From a list of SIXT API offer dicts, return the cheapest Full Size SUV offer.

    ACRISS code structure: [Category][BodyType][Transmission][AC]
      Position 1 (Category):
        F = Fullsize   G = Fullsize Elite   P = Premium
      Position 2 (Body/Type):
        F = Fullsize/SUV/4WD body (the SUV indicator)
      These map to Hertz's FFAR/FFDR (Fullsize SUV) class —
      e.g., Chevrolet Tahoe (FFAV), Chevrolet Suburban (PFAV), BMW X5 (GFAR).
      Intermediate (I) and Standard (S) crossovers (e.g., RAV4, Blazer) are
      excluded as they represent a smaller "intermediate SUV" category.
    """
    # Accepted first-letter categories for "Full Size SUV"
    _FULLSIZE_CATS = {"F", "G", "P"}
    best: Optional[Dict] = None
    for offer in offers:
        acriss = (offer.get("offer_acriss_code") or "").upper()
        if len(acriss) < 2:
            continue
        if acriss[0] not in _FULLSIZE_CATS:
            continue
        if acriss[1] != "F":
            continue
        total = (offer.get("price_total") or {}).get("gross", {}).get("value")
        if not total:
            continue
        if best is None or total < best["_total"]:
            best = {**offer, "_total": total}
    return best

# Hertz — station code used in the direct results URL.
_hertz_loc = _db_lookup("Hertz", BOOKING["airport_code"])
HERTZ_STATION_CODE: Optional[str] = _hertz_loc["station_code"] if _hertz_loc else None
if not HERTZ_STATION_CODE:
    print(f"  [DB] No Hertz entry for {BOOKING['airport_code']} — Hertz will fall back to Kayak.")

# Enterprise Holdings API config — EHI provider covers Enterprise/National/Alamo.
_ehi_loc = _db_lookup("EHI", BOOKING["airport_code"])
EH_LOCATION_CONFIG: Dict = {}
if _ehi_loc:
    EH_LOCATION_CONFIG[BOOKING["airport_code"]] = {
        "id":              _ehi_loc["location_id"],
        # national_gma_id: National/Alamo GMA API may use a different location ID
        # than Enterprise for the same airport. Stored per-airport in the DB.
        "national_id":     _ehi_loc.get("national_gma_id", _ehi_loc["location_id"]),
        "alamo_id":        _ehi_loc.get("alamo_gma_id", _ehi_loc.get("national_gma_id", _ehi_loc["location_id"])),
        "group_branch_id": _ehi_loc["group_branch_id"],
        "name":            _ehi_loc["name"],
        "airport_code":    _ehi_loc["airport_code"],
        "country_code":    _ehi_loc["country_code"],
        "gps":             _ehi_loc["gps"],
        "time_zone_id":    _ehi_loc["time_zone_id"],
    }
else:
    print(f"  [DB] No EHI entry for {BOOKING['airport_code']} — Enterprise/National/Alamo will use form-fill.")

EH_API_BASE = "https://prd-east.webapi.enterprise.com/enterprise-ewt"

# Shared cache so Enterprise/National/Alamo reuse one BD session (populated by
# _check_ehi_all on first call; subsequent callers read from dict directly).
_ehi_cache: Dict[str, Optional[Dict]] = {}
_ehi_lock: Optional["asyncio.Lock"] = None


def _get_ehi_lock() -> "asyncio.Lock":
    global _ehi_lock
    if _ehi_lock is None:
        _ehi_lock = asyncio.Lock()
    return _ehi_lock

# ─────────────────────────────────────────────────────────────────────────────
# PROVIDERS & URLS
# ─────────────────────────────────────────────────────────────────────────────

PROVIDERS = [
    "SIXT", "Hertz", "Avis", "Budget",
    "National", "Enterprise", "Alamo", "Dollar", "Thrifty",
    "Kayak",   # Cheapest Full Size SUV from any OTA on Kayak (covers blocked direct sites)
]

PROVIDER_URLS = {
    "SIXT":       "https://www.sixt.com",
    "Hertz":      "https://www.hertz.com",
    "Avis":       "https://www.avis.com",
    "Budget":     "https://www.budget.com",
    "National":   "https://www.nationalcar.com",
    "Enterprise": "https://www.enterprise.com",
    "Alamo":      "https://www.alamo.com",
    "Dollar":     "https://www.dollar.com",
    "Thrifty":    "https://www.thrifty.com",
}

# SIXT — betafunnel URL is built at runtime inside check_sixt() because the
# offer_location_uuid is session-specific (fetched fresh from the SelectLocation
# API on every check).  No module-level URL constant is needed.

# Avis — direct results URL (discovered by inspecting the form-submit redirect).
# pickup_month / return_month are 2-digit (05), pickup_day / return_day are 2-digit (02).
_pu = BOOKING["pickup_date"].split("-")   # ["2026","05","02"]
_re = BOOKING["return_date"].split("-")   # ["2026","05","06"]
AVIS_RESULTS_URL = (
    "https://www.avis.com/en/reservation/vehicle-availability"
    "?dropoff_suggestion_type_code=AIRPORT"
    "&pickup_hour={pu_hh}&pickup_minute={pu_mm}&pickup_am_pm={pu_ampm}"
    "&pickup_day={pu_day}&pickup_month={pu_month}&pickup_year={pu_year}"
    "&pickup_location_region=NAM&pickup_suggestion_type_code=AIRPORT"
    "&residency_value=US"
    "&return_hour={re_hh}&return_minute={re_mm}&return_am_pm={re_ampm}"
    "&return_day={re_day}&return_month={re_month}&return_year={re_year}"
    "&pickup_location_code={loc}&return_location_code={loc}"
    "&age={age}&country=us&locale=en-US&brand=avis"
).format(
    pu_day=_pu[2], pu_month=_pu[1], pu_year=_pu[0],
    pu_hh="12", pu_mm="00", pu_ampm="PM",
    re_day=_re[2], re_month=_re[1], re_year=_re[0],
    re_hh="12", re_mm="00", re_ampm="PM",
    loc=BOOKING["airport_code"], age=BOOKING["driver_age"],
)

# Budget — same ABG platform as Avis, just change brand=budget.
BUDGET_RESULTS_URL = AVIS_RESULTS_URL.replace("brand=avis", "brand=budget").replace(
    "www.avis.com", "www.budget.com"
)

# Hertz — direct results URL built from HERTZ_STATION_CODE config above.
# None when the station code is not in locations_db.json for this airport.
if HERTZ_STATION_CODE:
    HERTZ_RESULTS_URL: Optional[str] = (
        "https://www.hertz.com/us/en/book/vehicles"
        "?pid={station}"
        "&pdate={pickup_date}T{pickup_time}:00"
        "&did={station}"
        "&ddate={return_date}T{return_time}:00"
        "&pCountryCode=US"
        "&age={age}"
    ).format(
        station=HERTZ_STATION_CODE,
        pickup_date=BOOKING["pickup_date"],
        pickup_time=BOOKING["pickup_time"],
        return_date=BOOKING["return_date"],
        return_time=BOOKING["return_time"],
        age=BOOKING["driver_age"],
    )
else:
    HERTZ_RESULTS_URL = None

# Dollar / Thrifty — same Hertz Holdings platform; each has its OWN station codes.
# Dollar and Thrifty station codes are stored separately in locations_db.json and
# frequently differ from Hertz station codes (e.g. LGA: Hertz=LGAT01, Dollar=LGAO01).
_dollar_loc  = _db_lookup("Dollar",  BOOKING["airport_code"])
_thrifty_loc = _db_lookup("Thrifty", BOOKING["airport_code"])
DOLLAR_STATION_CODE:  Optional[str] = _dollar_loc["station_code"]  if _dollar_loc  else None
THRIFTY_STATION_CODE: Optional[str] = _thrifty_loc["station_code"] if _thrifty_loc else None

_DOLLAR_THRIFTY_URL_TMPL = (
    "{base}/us/en/book/vehicles"
    "?pid={station}"
    "&pdate={pickup_date}T{pickup_time}:00"
    "&did={station}"
    "&ddate={return_date}T{return_time}:00"
    "&pCountryCode=US"
    "&age={age}"
)

if DOLLAR_STATION_CODE:
    DOLLAR_RESULTS_URL: Optional[str] = _DOLLAR_THRIFTY_URL_TMPL.format(
        base="https://www.dollar.com",
        station=DOLLAR_STATION_CODE,
        pickup_date=BOOKING["pickup_date"],
        pickup_time=BOOKING["pickup_time"],
        return_date=BOOKING["return_date"],
        return_time=BOOKING["return_time"],
        age=BOOKING["driver_age"],
    )
else:
    DOLLAR_RESULTS_URL = None
    print(f"  [DB] No Dollar entry for {BOOKING['airport_code']} — Dollar will return N/A.")

if THRIFTY_STATION_CODE:
    THRIFTY_RESULTS_URL: Optional[str] = _DOLLAR_THRIFTY_URL_TMPL.format(
        base="https://www.thrifty.com",
        station=THRIFTY_STATION_CODE,
        pickup_date=BOOKING["pickup_date"],
        pickup_time=BOOKING["pickup_time"],
        return_date=BOOKING["return_date"],
        return_time=BOOKING["return_time"],
        age=BOOKING["driver_age"],
    )
else:
    THRIFTY_RESULTS_URL = None
    print(f"  [DB] No Thrifty entry for {BOOKING['airport_code']} — Thrifty will return N/A.")

# Enterprise Holdings (National / Enterprise / Alamo) — deep-link URL format.
# These sites are SPAs using hash-based routing. The server serves find-a-vehicle.html
# for ANY request to that path; the client-side router processes the #/vehicles fragment.
# Without the hash fragment the server returns 404.
# Date format: MM%2FDD%2FYYYY (URL-encoded slashes), time: 12-hour with AM/PM.
_pu_mmddyyyy = f"{_pu[1]}%2F{_pu[2]}%2F{_pu[0]}"   # 05%2F02%2F2026
_re_mmddyyyy = f"{_re[1]}%2F{_re[2]}%2F{_re[0]}"   # 05%2F06%2F2026
_EH_BASE_FRAGMENT = (
    "#/vehicles"
    "?from={loc}"
    "&to={loc}"
    "&pickup={pu_date}+12%3A00+PM"
    "&return={re_date}+12%3A00+PM"
).format(
    loc=BOOKING["airport_code"],
    pu_date=_pu_mmddyyyy,
    re_date=_re_mmddyyyy,
)
NATIONAL_RESULTS_URL   = "https://www.nationalcar.com/en/reservation/find-a-vehicle.html" + _EH_BASE_FRAGMENT
ENTERPRISE_RESULTS_URL = "https://www.enterprise.com/en/reservation/find-a-vehicle.html" + _EH_BASE_FRAGMENT
ALAMO_RESULTS_URL      = "https://www.alamo.com/en/reservation/find-a-vehicle.html" + _EH_BASE_FRAGMENT

# CSV log file path
LOG_FILE = "price_log.csv"

# Playwright default timeout in milliseconds
TIMEOUT_MS = 60_000

# Realistic Chrome user-agent — reduces bot-detection rejections on Hertz / Avis / Budget
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# When True, print first few card texts for any provider that returns no SUV match.
# Useful for diagnosing Dollar / Thrifty / Alamo card label formats.
DEBUG_CARDS = True

# ─────────────────────────────────────────────────────────────────────────────
# BRIGHT DATA BROWSER API
# ─────────────────────────────────────────────────────────────────────────────
# Set BRIGHT_DATA_CDP_URL to your wss:// CDP endpoint to enable direct price
# checking for providers that are currently Kayak-backed.
# When None, all Kayak-backed providers continue to use Kayak as before.
# When set, each Kayak-backed provider will first attempt a direct check via
# the Bright Data browser (residential IP + anti-bot bypass), and only fall
# back to Kayak if the direct check fails to find a price.

BRIGHT_DATA_CDP_URL = "wss://brd-customer-hl_71b98e26-zone-car_rental_monitor:d69ojowoqp6o@brd.superproxy.io:9222"

# Maximum concurrent Bright Data CDP browser connections.
# Each connect_over_cdp() occupies one slot; released automatically on browser.close().
BD_MAX_CONCURRENT = 5
BD_SEMAPHORE: asyncio.Semaphore | None = None   # initialised in main() after event loop starts

# ─────────────────────────────────────────────────────────────────────────────
# KAYAK AGGREGATOR CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Providers sourced from Kayak.
# Hertz, National, Enterprise, Alamo have working direct API checks and NO longer
# use Kayak as a fallback — they return ERROR on direct-check failure.
# Budget has no direct API (ABG SSR), so Kayak is its primary source.
# Dollar/Thrifty keep a Kayak tab for airports where those stations are open.
_KAYAK_TARGETS = {"Budget", "Dollar", "Thrifty"}

# Map Kayak display-name variants → our canonical provider names.
# Only Budget/Dollar/Thrifty use Kayak as their data source.
# Hertz/National/Enterprise/Alamo names kept for __kayak_best__ parsing only
# (the unfiltered "best" tab may still show any provider).
_KAYAK_NAME_MAP = {
    "hertz":                  "Hertz",
    "budget":                 "Budget",
    "national car rental":    "National",
    "national":               "National",
    "enterprise rent-a-car":  "Enterprise",
    "enterprise":             "Enterprise",
    "alamo rent a car":       "Alamo",
    "alamo":                  "Alamo",
    "dollar car rental":      "Dollar",
    "dollar":                 "Dollar",
    "thrifty car rental":     "Thrifty",
    "thrifty":                "Thrifty",
}

# Kayak location ID — dynamically resolved from locations_db.json.
# The location_id value (e.g. "LGA-a15830") appears in Kayak filtered search URLs.
# Derive for a new airport: search kayak.com/cars, apply any agency filter, copy the ID from URL.
_kayak_loc = _db_lookup("Kayak", BOOKING["airport_code"])
KAYAK_LOCATION_ID: Optional[str] = _kayak_loc["location_id"] if _kayak_loc else None
if not KAYAK_LOCATION_ID:
    print(f"  [DB] No Kayak entry for {BOOKING['airport_code']} — Kayak fallback will be unavailable.")

# Kayak caragency= URL slug for each Kayak-backed provider.
# Hertz/National/Enterprise/Alamo removed — they use direct API checks.
_KAYAK_AGENCY_SLUGS: Dict[str, str] = {
    "Budget":  "budget",
    "Dollar":  "dollar",
    "Thrifty": "thrifty",
}

# Module-level cache so Budget/Dollar/Thrifty share one Kayak browser session
_kayak_cache: Optional[Dict[str, Dict]] = None

# ACRISS codes and class name keywords that map to Full Size SUV.
# These are checked both as substrings AND via the word-split logic in is_fullsize_suv().
FULLSIZE_SUV_KEYWORDS = [
    # Explicit full-size SUV phrases (SUV required)
    "full size suv", "fullsize suv", "full-size suv",
    "large suv", "premium suv",
    # NOTE: "full size", "fullsize", "full-size" alone are NOT listed here because
    # Avis/Budget use these labels for Full-Size Sedans (Toyota Camry class).
    # Matching is handled by the word-split + "suv" check in is_fullsize_suv().
    # ACRISS codes — G=Full-size, F=4WD/SUV, A=Automatic, R=A/C
    "gfar", "gpar", "gsar", "guar", "gfmr",
    "ifar", "ipar",  # Intermediate 4WD
]

# ─────────────────────────────────────────────────────────────────────────────
# CAR CLASS EQUIVALENCY
# Maps ACRISS codes → equivalent terms on each provider's website / Kayak.
# Used to ensure we only compare genuinely equivalent vehicle classes.
# ─────────────────────────────────────────────────────────────────────────────

CAR_CLASS_EQUIVALENTS: Dict[str, Dict] = {
    "GFAR": {   # Full-size 4WD/SUV Automatic with A/C — e.g. SIXT FULLSIZE ELITE SUV
        "name":           "Full-size SUV",
        "acriss_regex":   r"\b[GI][FP][A-Z][A-Z]\b",  # G or I body, F or P (4WD)
        "kayak_terms":    ["Full-size SUV", "Large SUV", "Full Size SUV"],
        "kayak_class_filter": "SUV",       # value for carclass= in Kayak URL
        "exclude_terms":  [                 # class lines containing these → skip
            "compact", "intermediate", "standard", "economy", "mini",
            "luxury", "convertible", "van", "minivan", "pickup", "hybrid",
        ],
        # ── Provider-specific identifiers ─────────────────────────────────────
        # Hertz: SIPP codes accepted as Full Size SUV in check_hertz().
        #   FFAR = Full Size SUV 2WD / FFDR = Full Size SUV AWD
        "hertz_sipp_codes": {"FFAR", "FFDR"},
        # EHI: SIPP code → display name mapping used in the EHI JS fetch call.
        #   Only codes listed here get explicit names; others fall through to
        #   _ehi_extract_best() → is_fullsize_suv(name) text matching.
        "ehi_sipp_codes": {
            "FFAR": "Full Size SUV",
            "FFDR": "Full Size SUV AWD",
        },
        # SIXT: lowercase substrings of the card title that indicate this class.
        #   Checked with `any(term in title.lower() for term in sixt_title_terms)`.
        "sixt_title_terms": [
            "fullsize suv", "full size suv", "full-size suv", "fullsize elite suv",
        ],
    },
    "IFAR": {   # Intermediate 4WD/SUV — e.g. standard SUV / crossover
        "name":           "Intermediate SUV",
        "acriss_regex":   r"\bI[FP][A-Z][A-Z]\b",
        "kayak_terms":    ["Intermediate SUV", "Standard SUV", "Mid-size SUV"],
        "kayak_class_filter": "SUV",
        "exclude_terms":  ["fullsize", "full-size", "large", "compact", "economy"],
        "hertz_sipp_codes": {"IFAR", "IFDR", "IRAR", "IRDR"},
        "ehi_sipp_codes":   {"SFAR": "Standard SUV", "RFAR": "Standard Elite SUV"},
        "sixt_title_terms": ["standard suv", "intermediate suv", "mid-size suv"],
    },
}

# Which ACRISS class is being monitored for this booking
ACTIVE_CAR_CLASS = "GFAR"   # SIXT FULLSIZE ELITE SUV → ACRISS GFAR

# Merged EHI SIPP-code → display-name map across ALL defined car classes.
# Used in the EHI JS fetch call so API responses with unnamed SIPP codes get
# readable names before is_fullsize_suv() / _ehi_extract_best() filters them.
_EHI_CODE_NAMES: Dict[str, str] = {
    # Merge from all CAR_CLASS_EQUIVALENTS entries
    **{k: v for cls in CAR_CLASS_EQUIVALENTS.values()
       for k, v in cls.get("ehi_sipp_codes", {}).items()},
    # Additional well-known EHI codes not yet covered by any class definition
    "RFAR": "Standard Elite SUV",
    "SFAR": "Standard SUV",
    "WFAR": "Luxury Elite SUV",
    "UFAR": "Premium Elite SUV",
    "PFAR": "Premium SUV",
    "FJAR": "Jeep Wrangler 4 door",
}
# Pre-build the JavaScript object literal for injection into the EHI JS f-string
_EHI_CODE_NAMES_JS: str = ", ".join(
    f"'{k}': '{v}'" for k, v in _EHI_CODE_NAMES.items()
)

# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT TYPE — map BOOKING["payment_type"] to provider-specific strings
# ─────────────────────────────────────────────────────────────────────────────
# Hertz API:  rate.rate_type == "PAYLATER" | "PREPAY"
# EHI API:    c.charges.PAYLATER | c.charges.PREPAY  (key in the charges object)
# None/any:   accept whichever rate type appears (pick cheapest)
_RAW_PAY = (BOOKING.get("payment_type") or "").upper()
# HERTZ_RATE_TYPE: expected value of rate_type in the Hertz API response
# EHI_CHARGE_KEY: key inside c.charges{} for the EHI GBO API
if _RAW_PAY in ("PAY_LATER", "PAYLATER"):
    HERTZ_RATE_TYPE: Optional[str] = "PAYLATER"
    EHI_CHARGE_KEY:  str           = "PAYLATER"
elif _RAW_PAY in ("PREPAID", "PREPAY"):
    HERTZ_RATE_TYPE = "PREPAY"
    EHI_CHARGE_KEY  = "PREPAY"
else:
    # No preference — accept any, pick cheapest
    HERTZ_RATE_TYPE = None
    EHI_CHARGE_KEY  = "PAYLATER"   # default to PAYLATER when preference is unset

# ─────────────────────────────────────────────────────────────────────────────
# LOCATION DISCOVERY CONFIG
# ─────────────────────────────────────────────────────────────────────────────

LOCATIONS_CACHE_FILE = Path("locations_cache.json")
LOCATIONS_DB_FILE    = Path(__file__).parent / "locations_db.json"  # static locations DB
LOCATIONS_CACHE_DAYS = 30   # refresh every 30 days
NEARBY_RADIUS_MILES      = 50   # search for locations within this radius
MAX_NEARBY_PER_PROVIDER  = 5    # cap nearby locations per provider after distance sort
CAB_BASE_FARE        = 10.0 # $ base fare to any location (distance fallback)
CAB_PER_MILE         = 3.0  # $ per mile beyond airport (distance fallback)

# Hardcoded taxi fares (USD) between common airport pairs for accurate net-saving estimates.
# Source: NYC TLC flat-rate schedule + typical airport taxi rates.
# Used instead of the distance formula when the destination is a known airport.
CAB_FARES: Dict[str, Dict[str, float]] = {
    "LGA": {"JFK": 30.0,  "EWR": 55.0,  "BOS": 220.0},
    "JFK": {"LGA": 30.0,  "EWR": 65.0},
    "EWR": {"LGA": 55.0,  "JFK": 65.0},
    "ORD": {"MDW": 40.0},
    "LAX": {"BUR": 50.0,  "LGB": 45.0,  "SNA": 55.0,  "ONT": 70.0},
    "SFO": {"OAK": 45.0,  "SJC": 60.0},
    "MIA": {"FLL": 42.0,  "PBI": 75.0},
    "DFW": {"DAL": 35.0,  "AUS": 200.0},
    "BOS": {"PVD": 85.0,  "MHT": 60.0},
    "ATL": {},
}

# Lat/lng for common airports used in distance calculations.
# Add more as needed; falls back to no distance filtering if airport not listed.
_AIRPORT_COORDS: Dict[str, tuple] = {
    "LGA": (40.7769, -73.8740),
    "JFK": (40.6413, -73.7781),
    "EWR": (40.6895, -74.1745),
    "ORD": (41.9742, -87.9073),
    "LAX": (33.9425, -118.4081),
    "SFO": (37.6213, -122.3790),
    "ATL": (33.6407, -84.4277),
    "DFW": (32.8998, -97.0403),
    "MIA": (25.7959, -80.2870),
    "BOS": (42.3656, -71.0096),
}

# ─────────────────────────────────────────────────────────────────────────────
# KAYAK FILTER BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_kayak_fs_param(
    agency_slug: Optional[str] = None,
    with_free_cancel: Optional[bool] = None,
) -> str:
    """
    Build the Kayak ?fs= filter string from BOOKING config.
    Applied to every Kayak URL so all searches honour the booking's preferences.

    Kayak fs= param is semicolon-separated key=value pairs, e.g.:
        carclass=SUV;caragency=hertz;carpolicies=cancel;carcapacity=pas_5_6

    CONFIRMED WORKING params (verified 2026-04-13 via live Kayak URL observation):
        carclass=SUV            → Full Size SUV class filter
        caragency={slug}        → agency filter (hertz, budget, national, enterprise, alamo, dollar, thrifty)
        carpolicies=cancel      → free cancellation only (NOT freecancel=1 — silently dropped)
        unlimitedmileage=1      → unlimited mileage (not verified but plausible)

    CONFIRMED BROKEN / REMOVED (silently stripped or returns no results):
        carcapacity=pas_5_6     → REMOVED — causes most providers to return empty results.
                                   Full Size SUVs seat 5-6 by definition but Kayak doesn't
                                   tag all inventory consistently with this filter.
        freecancel=1            → silently dropped by Kayak
        seats=5                 → silently dropped by Kayak
        paymenttype=postpay     → stripped by Kayak (causes no results when combined)
        paymenttype=prepay      → unconfirmed; not applied
        transmission=A/M        → unverified; not applied to avoid silent filter drop
        carclass=fullsize       → returns 0 results (use carclass=SUV instead)

    Args:
        agency_slug     : Kayak caragency= value, or None for no agency filter.
        with_free_cancel: Override for carpolicies=cancel filter.
                          None  → use BOOKING["free_cancellation"] setting (default).
                          True  → always include carpolicies=cancel.
                          False → always omit carpolicies=cancel (relaxed fallback).
    """
    active_class = CAR_CLASS_EQUIVALENTS.get(ACTIVE_CAR_CLASS, {})
    kayak_class  = active_class.get("kayak_class_filter", "SUV")

    parts: List[str] = [f"carclass={kayak_class}"]

    if agency_slug:
        parts.append(f"caragency={agency_slug}")

    # Determine whether to apply free-cancel filter
    apply_fc = BOOKING.get("free_cancellation") if with_free_cancel is None else with_free_cancel
    if apply_fc:
        parts.append("carpolicies=cancel")   # CONFIRMED WORKING (not freecancel=1)

    if BOOKING.get("unlimited_mileage"):
        parts.append("unlimitedmileage=1")

    # carcapacity=pas_5_6 intentionally omitted — restricts Kayak inventory
    # too aggressively, causing most providers to return empty results.
    # Full Size SUVs seat 5-6 by definition; carclass=SUV is sufficient.

    # NOTE: paymenttype=postpay and paymenttype=prepay are NOT applied —
    # postpay is stripped by Kayak causing empty results; prepay is unconfirmed.
    # Payment type shown as informational label in results table only.

    return ";".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# LOCATION DISCOVERY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two lat/lng coordinates."""
    R = 3958.8
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _cab_fare(distance_miles: float) -> float:
    """Estimate taxi fare from airport to off-airport rental location (distance fallback)."""
    return CAB_BASE_FARE + CAB_PER_MILE * distance_miles


def _cab_fare_between(from_airport: str, to_airport: str, to_lat: float, to_lng: float) -> float:
    """
    Return the best available cab-fare estimate from from_airport to a location.
    Uses hardcoded CAB_FARES table for known airport pairs; falls back to distance formula.
    """
    # Check hardcoded table first (most accurate)
    known = CAB_FARES.get(from_airport, {}).get(to_airport)
    if known is not None:
        return known
    # Fall back to haversine distance estimate
    home_coords = _AIRPORT_COORDS.get(from_airport)
    if home_coords:
        dist = _haversine_miles(home_coords[0], home_coords[1], to_lat, to_lng)
        return _cab_fare(dist)
    return CAB_BASE_FARE


def _load_locations_db() -> Dict:
    """Load the static locations database from locations_db.json."""
    try:
        return json.loads(LOCATIONS_DB_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [Locations] Cannot load {LOCATIONS_DB_FILE}: {exc}")
        return {"locations": []}


def _sixt_nearby_from_db(home_airport: str) -> List[Dict]:
    """
    Find SIXT branches near home_airport using the static locations_db.json.
    Includes airports AND city/neighbourhood/hotel branches.
    Excludes the home airport itself.  Returns up to MAX_NEARBY_PER_PROVIDER
    branches within NEARBY_RADIUS_MILES, sorted by distance.

    NOTE: branch_id is required for API pricing; city branches in the DB have
          empty branch_id and will show "unknown" price in Phase 3.
    """
    home_coords = _AIRPORT_COORDS.get(home_airport)
    if not home_coords:
        return []
    hlat, hlng = home_coords

    db = _load_locations_db()
    branches = []
    skipped_non_us = 0
    for entry in db.get("locations", []):
        if entry.get("provider") != "SIXT":
            continue
        # Skip non-US entries (geocoding errors / international contamination)
        country = (entry.get("country") or "").upper()
        if country and country not in ("US", "USA", "UNITED STATES", "PR", "GU", "VI"):
            continue
        airport_code = entry.get("airport_code", "")
        if airport_code == home_airport:
            continue
        ilat = float(entry.get("lat") or 0)
        ilng = float(entry.get("lng") or 0)
        if not (ilat and ilng):
            continue
        # US bounding box: lat 17-72, lng -180 to -64 (covers AK, HI, PR)
        if not (17.0 <= ilat <= 72.0 and -180.0 <= ilng <= -64.0):
            skipped_non_us += 1
            continue
        dist = _haversine_miles(hlat, hlng, ilat, ilng)
        if dist > NEARBY_RADIUS_MILES:
            continue
        branch_id = entry.get("branch_id", "")
        # Unique key for dedup and price lookup
        location_key = airport_code if airport_code else (branch_id if branch_id else entry.get("name", "")[:24])
        if not location_key:
            continue
        fare = _cab_fare_between(home_airport, airport_code, ilat, ilng)
        branches.append({
            "branch_id":     branch_id,
            "location_uuid": "",   # fetch at runtime via SIXT SuggestLocations API
            "name":          entry.get("name", airport_code or location_key),
            "airport_code":  airport_code,
            "location_key":  location_key,
            "is_airport":    bool(entry.get("is_airport", False)),
            "lat": ilat, "lng": ilng,
            "distance_miles": round(dist, 1),
            "cab_fare":       round(fare, 2),
        })
    if skipped_non_us:
        print(f"  [NearbyDB] Skipped {skipped_non_us} SIXT entries with non-US coords "
              f"(international IATA code collision — harmless)")
    branches.sort(key=lambda x: x["distance_miles"])
    return branches[:MAX_NEARBY_PER_PROVIDER]


def _kayak_nearby_from_db(home_airport: str) -> List[Dict]:
    """
    Find Kayak airport locations near home_airport using the static locations_db.json.
    Kayak DB only contains airports; removing is_airport filter has no effect.
    Returns up to MAX_NEARBY_PER_PROVIDER locations within NEARBY_RADIUS_MILES.
    """
    home_coords = _AIRPORT_COORDS.get(home_airport)
    if not home_coords:
        return []
    hlat, hlng = home_coords

    db = _load_locations_db()
    locations = []
    for entry in db.get("locations", []):
        if entry.get("provider") != "Kayak":
            continue
        country = (entry.get("country") or "").upper()
        if country and country not in ("US", "USA", "UNITED STATES", "PR", "GU", "VI"):
            continue
        code = entry.get("airport_code", "")
        if code == home_airport:
            continue
        ilat = float(entry.get("lat") or 0)
        ilng = float(entry.get("lng") or 0)
        if not (ilat and ilng):
            continue
        if not (17.0 <= ilat <= 72.0 and -180.0 <= ilng <= -64.0):
            continue
        dist = _haversine_miles(hlat, hlng, ilat, ilng)
        if dist > NEARBY_RADIUS_MILES:
            continue
        fare = _cab_fare_between(home_airport, code, ilat, ilng)
        locations.append({
            "kayak_location_id": entry.get("location_id", ""),
            "name":              entry.get("name", code),
            "airport_code":      code,
            "location_key":      code,   # Kayak is always airports
            "is_airport":        True,
            "lat": ilat, "lng": ilng,
            "distance_miles":    round(dist, 1),
            "cab_fare":          round(fare, 2),
        })
    locations.sort(key=lambda x: x["distance_miles"])
    return locations[:MAX_NEARBY_PER_PROVIDER]


def _hertz_nearby_from_db(home_airport: str) -> List[Dict]:
    """
    Find Hertz locations near home_airport using the static locations_db.json.
    Includes airports AND city/neighbourhood branches (city branches have valid
    station codes like NYCC13 and CAN be priced via the Hertz rates URL).
    Returns up to MAX_NEARBY_PER_PROVIDER locations within NEARBY_RADIUS_MILES.
    Only includes entries whose provider is "Hertz" (not Dollar / Thrifty).
    """
    home_coords = _AIRPORT_COORDS.get(home_airport)
    if not home_coords:
        return []
    hlat, hlng = home_coords

    db = _load_locations_db()
    results = []
    seen_keys: Set[str] = set()
    for entry in db.get("locations", []):
        if entry.get("provider") != "Hertz":
            continue
        airport_code  = entry.get("airport_code", "")
        station_code  = entry.get("station_code", entry.get("location_id", ""))
        if airport_code == home_airport:
            continue
        if not station_code:
            continue   # can't price without a station code
        # Unique key: prefer airport code; fall back to station code
        location_key = airport_code if airport_code else station_code
        if location_key in seen_keys:
            continue
        ilat = float(entry.get("lat") or 0)
        ilng = float(entry.get("lng") or 0)
        if not (ilat and ilng):
            continue
        if not (17.0 <= ilat <= 72.0 and -180.0 <= ilng <= -64.0):
            continue
        dist = _haversine_miles(hlat, hlng, ilat, ilng)
        if dist > NEARBY_RADIUS_MILES:
            continue
        seen_keys.add(location_key)
        fare = _cab_fare_between(home_airport, airport_code, ilat, ilng)
        results.append({
            "station_code":   station_code,
            "name":           entry.get("name", airport_code or station_code),
            "airport_code":   airport_code,
            "location_key":   location_key,
            "is_airport":     bool(entry.get("is_airport", False)),
            "lat": ilat, "lng": ilng,
            "distance_miles": round(dist, 1),
            "cab_fare":       round(fare, 2),
        })
    results.sort(key=lambda x: x["distance_miles"])
    return results[:MAX_NEARBY_PER_PROVIDER]


# EHI branch names containing these substrings are internal/ops branches,
# not customer-facing rental locations — exclude them from nearby results.
_EHI_JUNK_KEYWORDS = frozenset([
    "admin", " a/b", "training", "damage", "production", " oos", "cafc",
    "remote", "combined sat", "subregion", "ccard", "autobody", "auto body",
    "truck rental", "overseas", "corporate", "car sales", "idle", "24zz",
    "local account",
])


def _ehi_nearby_from_db(home_airport: str) -> List[Dict]:
    """
    Find Enterprise/National/Alamo locations near home_airport using the
    static locations_db.json.  Includes airports AND city/neighbourhood branches.
    Filters out internal EHI operational branches (admin, training, damage, etc.).
    Deduplicates by group_branch_id.  Returns up to MAX_NEARBY_PER_PROVIDER
    locations within NEARBY_RADIUS_MILES.
    """
    home_coords = _AIRPORT_COORDS.get(home_airport)
    if not home_coords:
        return []
    hlat, hlng = home_coords

    db = _load_locations_db()
    results = []
    seen_gbids: Set[str] = set()   # deduplicate by group_branch_id
    for entry in db.get("locations", []):
        if entry.get("provider") != "EHI":
            continue
        gbid = entry.get("group_branch_id", "")
        if not gbid:
            continue
        if gbid in seen_gbids:
            continue
        # Filter internal/junk EHI branches by name
        name_lower = (entry.get("name") or "").lower()
        if any(kw in name_lower for kw in _EHI_JUNK_KEYWORDS):
            continue
        airport_code = entry.get("airport_code", "")
        if airport_code == home_airport:
            continue
        country = (entry.get("country_code") or entry.get("country") or "").upper()
        if country and country not in ("US", "USA", "PR", "GU", "VI"):
            continue
        ilat = float(entry.get("lat") or 0)
        ilng = float(entry.get("lng") or 0)
        if not (ilat and ilng):
            continue
        if not (17.0 <= ilat <= 72.0 and -180.0 <= ilng <= -64.0):
            continue
        dist = _haversine_miles(hlat, hlng, ilat, ilng)
        if dist > NEARBY_RADIUS_MILES:
            continue
        seen_gbids.add(gbid)
        # Unique key: prefer airport code; fall back to group_branch_id
        location_key = airport_code if airport_code else gbid
        fare = _cab_fare_between(home_airport, airport_code, ilat, ilng)
        results.append({
            "group_branch_id": gbid,
            "location_id":     entry.get("location_id", ""),
            "name":            entry.get("name", airport_code or gbid),
            "airport_code":    airport_code,
            "location_key":    location_key,
            "is_airport":      bool(entry.get("is_airport", False)),
            "lat": ilat, "lng": ilng,
            "distance_miles":  round(dist, 1),
            "cab_fare":        round(fare, 2),
        })
    results.sort(key=lambda x: x["distance_miles"])
    return results[:MAX_NEARBY_PER_PROVIDER]


def discover_nearby_locations(booking: Dict) -> Dict[str, List[Dict]]:
    """
    Find car rental locations within NEARBY_RADIUS_MILES of the pickup airport
    using the static locations_db.json (no live API calls needed).

    Includes airports AND city/neighbourhood/hotel branches for each provider.
    Skips any location where estimated cab fare exceeds 50% of the booked price
    (a cab that expensive would never yield a net saving).

    Returns:
        {"SIXT":  [{branch_id, name, airport_code, location_key, is_airport, ...}, ...],
         "Kayak": [{kayak_location_id, name, airport_code, location_key, ...}, ...],
         "Hertz": [{station_code, name, airport_code, location_key, ...}, ...],
         "EHI":   [{group_branch_id, location_id, name, airport_code, location_key, ...}, ...]}

    Each entry has location_key (airport_code for airports, else provider-specific ID),
    is_airport (bool), and cab_fare (USD estimated via CAB_FARES table or haversine formula).
    """
    airport     = booking["airport_code"]
    booked      = booking["booked_price"]
    cab_limit   = booked * 0.5   # skip if cab alone costs > 50% of booking
    locations: Dict[str, List[Dict]] = {}

    def _cab_filter(locs: List[Dict], label: str) -> List[Dict]:
        kept, skipped = [], []
        for loc in locs:
            if loc["cab_fare"] > cab_limit:
                skipped.append(loc)
            else:
                kept.append(loc)
        if skipped:
            print(f"  [Locations] {label}: skipped {len(skipped)} location(s) "
                  f"where cab (${skipped[0]['cab_fare']:.0f}+) > 50% of booked (${booked:.0f})")
        return kept

    sixt_branches = _cab_filter(_sixt_nearby_from_db(airport), "SIXT")
    if sixt_branches:
        locations["SIXT"] = sixt_branches
        print(f"  [Locations] SIXT: {len(sixt_branches)} nearby locations within {NEARBY_RADIUS_MILES}mi")
        for b in sixt_branches:
            tag = b['airport_code'] or "city"
            print(f"    • {b['name']} ({tag})  {b['distance_miles']}mi  "
                  f"cab≈${b['cab_fare']:.0f}  branch={b['branch_id'] or '—'}")
    else:
        print(f"  [Locations] SIXT: no nearby locations within {NEARBY_RADIUS_MILES}mi of {airport}")

    kayak_locs = _cab_filter(_kayak_nearby_from_db(airport), "Kayak")
    if kayak_locs:
        locations["Kayak"] = kayak_locs
        print(f"  [Locations] Kayak: {len(kayak_locs)} nearby locations within {NEARBY_RADIUS_MILES}mi")
        for k in kayak_locs:
            print(f"    • {k['name']} ({k['airport_code']})  {k['distance_miles']}mi  "
                  f"cab≈${k['cab_fare']:.0f}  id={k['kayak_location_id']}")
    else:
        print(f"  [Locations] Kayak: no nearby locations within {NEARBY_RADIUS_MILES}mi of {airport}")

    hertz_locs = _cab_filter(_hertz_nearby_from_db(airport), "Hertz")
    if hertz_locs:
        locations["Hertz"] = hertz_locs
        print(f"  [Locations] Hertz: {len(hertz_locs)} nearby locations within {NEARBY_RADIUS_MILES}mi")
        for h in hertz_locs:
            tag = h['airport_code'] or "city"
            print(f"    • {h['name']} ({tag})  {h['distance_miles']}mi  "
                  f"cab≈${h['cab_fare']:.0f}  station={h['station_code']}")
    else:
        print(f"  [Locations] Hertz: no nearby locations within {NEARBY_RADIUS_MILES}mi of {airport}")

    ehi_locs = _cab_filter(_ehi_nearby_from_db(airport), "EHI")
    if ehi_locs:
        locations["EHI"] = ehi_locs
        print(f"  [Locations] EHI: {len(ehi_locs)} nearby locations within {NEARBY_RADIUS_MILES}mi")
        for e in ehi_locs:
            tag = e['airport_code'] or "city"
            print(f"    • {e['name']} ({tag})  {e['distance_miles']}mi  "
                  f"cab≈${e['cab_fare']:.0f}  branch={e['group_branch_id']}")
    else:
        print(f"  [Locations] EHI: no nearby locations within {NEARBY_RADIUS_MILES}mi of {airport}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# RESULT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_result(
    provider: str,
    car_class: str = "",
    model: str = "",
    price: Optional[float] = None,
    url: str = "",
    error: Optional[str] = None,
    na: bool = False,
) -> Dict:
    """Return a standardised result dict.

    Set na=True for "known N/A" cases (no station at this airport, location
    permanently closed) — these show as N/A in the output rather than ERROR,
    since there is nothing wrong with the monitor; the provider simply has no
    coverage at this airport.
    """
    return {
        "provider":  provider,
        "car_class": car_class,
        "model":     model,
        "price":     price,
        "url":       url,
        "error":     error,
        "na":        na,
    }


def parse_price(text: str) -> Optional[float]:
    """
    Extract the first numeric price from a string.
    Handles formats like '$636.73', '$1,444', '636,73' (European), 'USD 636.73'.
    Returns None if no valid price found.

    Thousands vs decimal comma detection:
      US thousands:  "1,444"  → comma followed by exactly 3 digits → remove comma
      European dec:  "1,44"   → comma followed by 1-2 digits (no dot) → replace with dot
    """
    if not text:
        return None
    # Strip currency symbols and whitespace, keep digits, commas, dots
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    # US thousands separator: comma followed by exactly 3 digits (possibly repeated)
    # e.g. "1,444" or "1,234,567"  → remove commas
    if re.search(r"\d,\d{3}", cleaned):
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        # European decimal: single comma not in thousands position → treat as decimal
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    # Take the first valid float
    match = re.search(r"\d+\.\d+|\d+", cleaned)
    if match:
        try:
            val = float(match.group())
            return val if val > 0 else None
        except ValueError:
            return None
    return None


def is_fullsize_suv(text: str) -> bool:
    """
    Return True if text matches a Full Size SUV category or ACRISS code.

    Handles providers that insert qualifiers between class words and 'SUV':
      - Hertz: "Large 2WD SUV", "Large AWD SUV"
      - Avis: "Full Size SUV", "Full-Size SUV", "Large SUV"
      - SIXT: "FULLSIZE ELITE SUV"

    IMPORTANT: "SUV" must appear as a standalone word (word boundary match).
    "SUVs" (plural) must NOT match — it's a category label, not a vehicle class.
    """
    lower = text.lower().strip()

    # Fast bail: "suv" must appear as a whole word (not "suvs", "suva", etc.)
    if not re.search(r'\bsuv\b', lower):
        # Still allow ACRISS code matches (no SUV word needed).
        # Use word boundaries so "guar" in "laguardia" doesn't falsely match.
        acriss = ["gfar", "gpar", "gsar", "guar", "gfmr", "ifar", "ipar"]
        return any(re.search(r'\b' + kw + r'\b', lower) for kw in acriss)

    # "suv" is a word — now require a fullsize qualifier
    # 1. Exact phrase match
    if any(kw in lower for kw in FULLSIZE_SUV_KEYWORDS):
        return True

    # 2. Word-split: "suv" present + fullsize indicator word
    words = set(re.split(r"[\s\-/,]+", lower))
    if words & {"large", "full", "fullsize"}:
        return True

    # 3. "full" + "size" + "suv" anywhere in string (catches "Full-Size SUV")
    if "full" in lower and "size" in lower:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# POPUP DISMISSAL — called by every provider before touching the search form
# ─────────────────────────────────────────────────────────────────────────────

async def dismiss_popups(page) -> None:
    """
    Best-effort popup/modal dismissal.  Runs after a 3-second settle delay so
    cookie banners and interstitials have time to appear.

    Strategy (in order):
      1. Press Escape — catches most overlay/dialog patterns
      2. Click any visible 'close / dismiss / accept' button by text or aria-label
      3. Dollar/Thrifty specific: force-hide div.modal.fade.offers-modal via JS
      4. Enterprise/National/Alamo specific: click div.login-curtain to dismiss
    All steps are best-effort — failures are silently swallowed.
    """
    await page.wait_for_timeout(3000)

    # 1 — Escape key
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # 2 — Common dismiss button texts (case-insensitive partial match via XPath)
    dismiss_texts = [
        "Close", "close", "No thanks", "No Thanks", "Maybe later",
        "Maybe Later", "Dismiss", "dismiss", "Accept", "Accept All",
        "Accept Cookies", "Got it", "OK", "I Agree", "Agree",
        "CLOSE", "ACCEPT", "Agree & Continue",
    ]
    for text in dismiss_texts:
        try:
            # Try button/link with exact visible text
            btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

    # Also try common close icon selectors (×, ✕, aria-label="Close")
    close_selectors = [
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "button[aria-label='Dismiss']",
        "[class*='close-btn']",
        "[class*='closeBtn']",
        "[class*='modal-close']",
        "[class*='popup-close']",
        "[class*='cookie'] button",
        "#onetrust-accept-btn-handler",   # OneTrust cookie banner
        ".cc-btn.cc-dismiss",             # Cookie Consent
    ]
    for sel in close_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click(timeout=2000)
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

    # 3 — Dollar/Thrifty: offers-modal blocks all clicks
    try:
        await page.evaluate("""
            const m = document.querySelector('div.modal.fade.offers-modal');
            if (m) { m.style.display = 'none'; m.classList.remove('show'); }
            const backdrop = document.querySelector('.modal-backdrop');
            if (backdrop) backdrop.remove();
            document.body.classList.remove('modal-open');
        """)
    except Exception:
        pass

    # 4 — Enterprise/National/Alamo: login-curtain overlay
    try:
        curtain = page.locator("div.login-curtain").first
        if await curtain.is_visible(timeout=1000):
            await curtain.click(timeout=2000)
            await page.wait_for_timeout(500)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER CONTEXT HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def _apply_stealth(page) -> None:
    """Apply playwright-stealth to a page if the package is available."""
    if _STEALTH_AVAILABLE:
        await _stealth_async(page)


async def _new_context(browser):
    """
    Create a new browser context with a realistic user-agent, viewport, and extra headers.
    Also patches navigator.webdriver to undefined to evade basic bot detection.
    """
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    # Patch navigator.webdriver so sites can't detect Playwright automation
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime: {}};
    """)
    return ctx


def _stealth_launch_args():
    """Extra Chromium flags that reduce bot-detection fingerprinting."""
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--start-maximized",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# BRIGHT DATA BROWSER HELPER
# ─────────────────────────────────────────────────────────────────────────────

class _SemaphoreBrowser:
    """
    Thin proxy around a Playwright browser that releases BD_SEMAPHORE when
    close() is called.  All other attribute accesses are forwarded to the
    real browser object, so call sites need no changes.
    """
    def __init__(self, browser):
        self._browser = browser

    def __getattr__(self, name):
        return getattr(self._browser, name)

    @property
    def contexts(self):
        return self._browser.contexts

    async def close(self):
        try:
            await self._browser.close()
        finally:
            if BD_SEMAPHORE is not None:
                BD_SEMAPHORE.release()


async def get_browser(playwright):
    """
    Return a Playwright browser object.

    • If BRIGHT_DATA_CDP_URL is set → acquire BD_SEMAPHORE (max BD_MAX_CONCURRENT
      concurrent connections), then connect via Bright Data CDP.  The semaphore
      slot is released automatically when browser.close() is called.
    • Otherwise → launch a local headless=True Chromium instance.

    Always call `await browser.close()` in a finally block after use.
    """
    if BRIGHT_DATA_CDP_URL:
        if BD_SEMAPHORE is not None:
            await BD_SEMAPHORE.acquire()
        try:
            print(f"  [Browser] PATH: Bright Data CDP  url={BRIGHT_DATA_CDP_URL[:60]}...")
            browser = await playwright.chromium.connect_over_cdp(BRIGHT_DATA_CDP_URL)
            print("  [Browser] CDP connection established OK")
            return _SemaphoreBrowser(browser)
        except Exception:
            if BD_SEMAPHORE is not None:
                BD_SEMAPHORE.release()
            raise
    else:
        print("  [Browser] PATH: local chromium.launch (BRIGHT_DATA_CDP_URL is None)")
        return await playwright.chromium.launch(headless=True, args=_stealth_launch_args())


async def _new_bd_page(browser):
    """
    Create a new page from a Bright-Data (or local) browser.
    Applies stealth if available and sets a realistic viewport + user-agent.
    """
    try:
        # connect_over_cdp returns existing contexts; create a fresh one
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
    except Exception:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await ctx.new_page()
    await _apply_stealth(page)
    return page, ctx


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: SIXT
# ─────────────────────────────────────────────────────────────────────────────

def _sixt_is_fullsize(title: str) -> bool:
    """
    Match SIXT card titles for the ACTIVE_CAR_CLASS.
    Uses sixt_title_terms from CAR_CLASS_EQUIVALENTS for easy reconfiguration.
    Falls back to legacy fullsize+suv logic if no terms configured.
    """
    _cls  = CAR_CLASS_EQUIVALENTS.get(ACTIVE_CAR_CLASS, {})
    terms = _cls.get("sixt_title_terms", [])
    t = title.lower()
    if terms:
        return any(term in t for term in terms)
    # Legacy fallback: must have both "fullsize" (or variant) AND "suv"
    is_fullsize = "fullsize" in t or "full size" in t or "full-size" in t
    is_suv = "suv" in t
    return is_fullsize and is_suv


async def _scrape_sixt_page(page, label: str = "SIXT") -> Dict:
    """
    Core SIXT betafunnel scraper — shared by check_sixt() and nearby price fetches.
    Assumes the page has already navigated to the betafunnel URL.
    Returns a make_result() dict.
    """
    await dismiss_popups(page)

    # SIXT betafunnel uses data-testid="rent-offer-list-tile" for each vehicle card.
    # Wait for at least one tile to render (betafunnel is fully JS-rendered).
    await page.wait_for_selector(
        "[data-testid='rent-offer-list-tile']",
        timeout=TIMEOUT_MS,
    )
    # Extra settle time — prices load slightly after the card structure
    await page.wait_for_timeout(8000)

    print(f"  [{label}] Page: '{await page.title()}' @ {page.url}")

    cards = await page.query_selector_all("[data-testid='rent-offer-list-tile']")
    print(f"  [{label}] Found {len(cards)} offer tiles")

    if not cards:
        print(f"  [{label}] No offer tiles found — dumping first 2000 chars of body:")
        body_snippet = (await page.inner_text("body"))[:2000]
        print(body_snippet)
        return make_result("SIXT", error="No offer tiles found on page")

    print(f"  [{label}] All offer titles found:")
    all_titles = []
    for card in cards:
        text = (await card.inner_text()).strip()
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        title = lines[0] if lines else "(empty)"
        all_titles.append((title, lines))
        print(f"         • {title}")

    best_price: Optional[float] = None
    best_model = ""
    best_class = ""

    for title, lines in all_titles:
        if not _sixt_is_fullsize(title):
            continue
        # SIXT shows both per-day and total price in the card.
        # Collect all valid prices and take the largest (= total, not daily rate).
        card_prices = []
        for line in lines:
            price = parse_price(line)
            if price and 200 < price < 10_000:
                card_prices.append(price)
        if card_prices:
            card_total = max(card_prices)
            if best_price is None or card_total < best_price:
                best_price = card_total
                best_model = title
                best_class = "Full Size SUV"

    if best_price is None:
        return make_result("SIXT", error="No Full Size SUV found in results")

    return make_result(
        "SIXT",
        car_class=best_class,
        model=best_model,
        price=best_price,
        url=page.url,
    )


async def check_sixt(playwright) -> Dict:  # noqa: ARG001 (playwright unused — pure API)
    """
    Fetch SIXT Full Size SUV prices via direct gRPC-JSON API calls (no browser).

    Flow:
      1. Look up the SIXT branch for BOOKING["airport_code"] in locations_db.json.
      2. SelectLocation API → session-specific location_selection_id UUID.
      3. GetOfferRecommendationsV2 API → all offers with prices.
      4. Filter for Full Size SUV (ACRISS[1] == 'F', not compact/economy/minivan).
      5. Return cheapest match.
    """
    if not SIXT_LOCATION:
        airport = BOOKING["airport_code"]
        print(f"  [SIXT] No location data for {airport} in locations_db.json — skipping.")
        return make_result("SIXT", error=f"No SIXT location configured for {airport}")

    branch_id = SIXT_LOCATION["branch_id"]
    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}"
    return_dt  = f"{BOOKING['return_date']}T{BOOKING['return_time']}"

    print(f"  [SIXT] Fetching offers via API for {branch_id}  {pickup_dt} → {return_dt}")
    try:
        offers = await asyncio.to_thread(
            _sixt_get_offers, branch_id, pickup_dt, return_dt
        )
    except Exception as exc:
        return make_result("SIXT", error=f"API error: {exc}")

    if offers is None:
        return make_result("SIXT", error="SelectLocation or GetOfferRecommendationsV2 API failed")

    print(f"  [SIXT] {len(offers)} offers returned")
    for o in offers:
        acriss = o.get("offer_acriss_code", "")
        title  = o.get("car_info", {}).get("title", "")
        total  = (o.get("price_total") or {}).get("gross", {}).get("value")
        print(f"         {acriss:6s}  {title:<45s}  ${total:>8.2f}" if total else f"         {acriss:6s}  {title}")

    best = _sixt_best_fullsize_suv(offers)
    if not best:
        return make_result("SIXT", error="No Full Size SUV found in SIXT offers")

    acriss = best.get("offer_acriss_code", "")
    title  = best.get("car_info", {}).get("title", "")
    total  = best["_total"]
    print(f"  [SIXT] Best FSS: {acriss} {title}  ${total:.2f}")
    return make_result(
        "SIXT",
        car_class="Full Size SUV",
        model=title,
        price=total,
        url="https://www.sixt.com/car-rental/usa/",
    )


# ─────────────────────────────────────────────────────────────────────────────
# BRIGHT DATA — DIRECT PROVIDER PRICE EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_direct_suv(page, provider: str) -> Dict:
    """
    Generic Full Size SUV price extractor for direct provider results pages.
    Works across Hertz, Dollar, Thrifty (MUI card layout) and
    Enterprise/National/Alamo (EH SPA layout).

    Strategy:
    1. Collect all text blocks from the rendered page.
    2. Slide a 3-line window; if a line looks like a vehicle class and a nearby
       line looks like a price → record it.
    3. Return the cheapest match whose class label passes is_fullsize_suv().
    """
    await page.wait_for_timeout(5000)   # let the SPA finish rendering

    # Try to scroll-load more results
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    body_text = await page.inner_text("body")
    lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]

    if DEBUG_CARDS:
        print(f"  [{provider}] First 40 body lines:")
        for ln in lines[:40]:
            print(f"    {ln}")

    best_price: Optional[float] = None
    best_model = ""

    # Slide a window: look for a price within ±4 lines of a class label
    for i, line in enumerate(lines):
        if not is_fullsize_suv(line):
            continue
        window = lines[max(0, i - 4): i + 8]
        for w in window:
            p = parse_price(w)
            if p and 100 < p < 15_000:
                if best_price is None or p < best_price:
                    best_price = p
                    best_model = line
                break  # take first (closest) price in window

    if best_price is None:
        return make_result(provider, error="No Full Size SUV found in direct results")

    return make_result(
        provider,
        car_class="Full Size SUV",
        model=best_model,
        price=best_price,
        url=page.url,
    )


async def _check_direct_with_bd_fallback(playwright, provider: str, direct_url: str,
                                          wait_selector: Optional[str] = None) -> Dict:
    """
    Attempt a direct price check via Bright Data (or local browser if CDP not set).
    Falls back to Kayak if:
      - BRIGHT_DATA_CDP_URL is not set, OR
      - the direct check raises an exception, OR
      - the direct check returns an error result.

    Args:
        playwright:     Playwright instance passed down from the caller.
        provider:       Canonical provider name ("Hertz", "National", etc.)
        direct_url:     Full URL to navigate to for the results page.
        wait_selector:  Optional CSS selector to wait for before extracting prices.
                        If None, uses a 10-second wait + body text extraction.
    """
    if not BRIGHT_DATA_CDP_URL:
        # Bright Data not configured — skip straight to Kayak (existing behaviour)
        return await _check_from_kayak(playwright, provider)

    print(f"  [{provider}] Trying direct URL via Bright Data...")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        await page.goto(direct_url, timeout=TIMEOUT_MS * 2, wait_until="domcontentloaded")
        await dismiss_popups(page)
        print(f"  [{provider}] Page: '{await page.title()}' @ {page.url[:80]}")

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=90_000)
            except Exception:
                print(f"  [{provider}] wait_selector '{wait_selector}' timed out — extracting anyway")

        result = await _extract_direct_suv(page, provider)

        if result.get("error"):
            print(f"  [{provider}] Direct check returned error: {result['error']}")
            print(f"  [{provider}] Falling back to Kayak...")
            return await _check_from_kayak(playwright, provider)

        return result

    except Exception as exc:
        print(f"  [{provider}] Direct check exception: {exc!s:.120} — falling back to Kayak")
        return await _check_from_kayak(playwright, provider)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: HERTZ
# ─────────────────────────────────────────────────────────────────────────────

async def check_hertz(playwright) -> Dict:
    """
    Hertz — direct API via Bright Data browser session.

    Discovered API (confirmed via network capture):
      GET https://api.hertz.io/vehicle-rates?rental_type=LEISURE&brand=HERTZ&...

    Flow:
    1. Register page.route() handler for api.hertz.io/vehicle-rates BEFORE navigation.
    2. Navigate to HERTZ_RESULTS_URL — the page automatically fetches vehicle-rates.
    3. Route handler intercepts the response, reads the full JSON, fulfills the request.
    4. Extract Full Size SUV (sipp FFAR/FFDR or Fullsize+body SUV) with the payment type
       set by HERTZ_RATE_TYPE (derived from BOOKING["payment_type"]; None = any rate).
    5. Returns ERROR on failure — does NOT fall back to Kayak.

    No Bearer token required — the page fetches it via client_credentials on load.
    The route intercept captures the response transparently without blocking the page.
    """
    if not BRIGHT_DATA_CDP_URL:
        msg = "Bright Data not configured — Hertz requires direct API check"
        print(f"  [Hertz] {msg}")
        return make_result("Hertz", error=msg)
    if not HERTZ_RESULTS_URL:
        airport = BOOKING["airport_code"]
        msg = f"No Hertz station for {airport} in locations_db.json"
        print(f"  [Hertz] {msg}")
        return make_result("Hertz", error=msg)

    print("  [Hertz] Direct API via Bright Data (intercepting api.hertz.io/vehicle-rates)...")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        # Capture the vehicle-rates JSON response using page.on('response').
        # This is a passive observer — no route interception, no fulfill race condition.
        # Use asyncio.Event to wait until the body has been read rather than a fixed sleep.
        vehicle_data: list = []
        vehicle_ready = asyncio.Event()

        async def _on_response(response):
            if "vehicle-rates" not in response.url:
                return
            try:
                body = await response.body()
                parsed = json.loads(body.decode("utf-8"))
                vehicle_data.extend(parsed if isinstance(parsed, list) else [parsed])
                print(f"  [Hertz] Captured vehicle-rates: {len(vehicle_data)} vehicles")
            except Exception as e:
                print(f"  [Hertz] Parse error on vehicle-rates: {e!s:.80}")
            finally:
                vehicle_ready.set()  # always signal, even on error

        page.on("response", _on_response)

        await page.goto(HERTZ_RESULTS_URL, wait_until="domcontentloaded", timeout=90_000)
        # Wait for at least one MUI card to confirm the React app has hydrated and
        # fired its XHR calls, then give the response observer up to 60s total.
        try:
            await page.wait_for_selector(
                "[class*='MuiCard'], [class*='vehicle'], [class*='VehicleCard']",
                timeout=45_000,
            )
        except Exception:
            pass  # continue regardless — observer may still fire
        try:
            await asyncio.wait_for(vehicle_ready.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            raise Exception("vehicle-rates response not seen within 60s — API call did not fire")

        if not vehicle_data:
            raise Exception("vehicle-rates response captured but contained no data")

        # ── Identify Full Size SUVs ────────────────────────────────────────────
        # SIPP codes come from CAR_CLASS_EQUIVALENTS[ACTIVE_CAR_CLASS]["hertz_sipp_codes"].
        # Fallback: Fullsize category + SUV body type.
        _active_cls = CAR_CLASS_EQUIVALENTS.get(ACTIVE_CAR_CLASS, {})
        HERTZ_FULLSIZE_SUV_SIPP: Set[str] = set(
            _active_cls.get("hertz_sipp_codes", {"FFAR", "FFDR"})
        )

        def _is_hertz_fullsize_suv(vehicle: dict) -> bool:
            sipp = vehicle.get("sipp_code", "")
            if sipp in HERTZ_FULLSIZE_SUV_SIPP:
                return True
            cat  = (vehicle.get("vehicle_category") or "").lower()
            body = vehicle.get("vehicle_body_type") or []
            return cat == "fullsize" and "SUV" in body

        best_price: Optional[float] = None
        best_name = ""
        for v in vehicle_data:
            if not _is_hertz_fullsize_suv(v):
                continue
            name = v.get("vehicle_display_name") or v.get("sipp_code") or "Full Size SUV"
            for rate in v.get("pricing", {}).values():
                # Filter by payment type when a preference is set
                if HERTZ_RATE_TYPE and rate.get("rate_type") != HERTZ_RATE_TYPE:
                    continue
                total = rate.get("approximate_total")
                if total is None:
                    continue
                try:
                    price = float(total)
                except (TypeError, ValueError):
                    continue
                if price > 0 and (best_price is None or price < best_price):
                    best_price = price
                    best_name = name

        if best_price is None:
            suvs = [v.get("sipp_code") for v in vehicle_data if "SUV" in str(v.get("vehicle_body_type", []))]
            rate_label = HERTZ_RATE_TYPE or "any rate"
            raise Exception(f"No Full Size SUV {rate_label} rate found. SUV sipp codes: {suvs}")

        rate_label = HERTZ_RATE_TYPE or "any rate"
        print(f"  [Hertz] Best Full Size SUV: {best_name} ({rate_label}) @ ${best_price:.2f}")
        return make_result(
            "Hertz",
            car_class="Full Size SUV",
            model=best_name,
            price=best_price,
            url=HERTZ_RESULTS_URL,
        )

    except Exception as exc:
        err = str(exc)[:150]
        print(f"  [Hertz] API check failed: {err}")
        return make_result("Hertz", error=err)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: AVIS
# ─────────────────────────────────────────────────────────────────────────────

async def check_avis(playwright) -> Dict:
    """
    Avis — direct results URL via Bright Data (falls back to local browser if
    BRIGHT_DATA_CDP_URL is not set).  Routing through Bright Data eliminates the
    intermittent bot-blocking that occurred with a local Chromium launch.

    AVIS_RESULTS_URL is pre-built from BOOKING config at module load time.
    Results page uses <article> elements for each vehicle card.
    Cookie-seeding retry logic is preserved: if the direct URL redirects away
    from /vehicle-availability, we seed a session via the Avis homepage then retry.
    """
    browser = await get_browser(playwright)
    page, ctx = await _new_bd_page(browser)

    try:
        # Tight per-goto timeouts: on a healthy BD session Avis loads in ~15 s.
        # Three gotos at 60 s each + 90 s selector = 270 s worst-case, which
        # exceeds the 240 s asyncio cap and produces a confusing timeout error.
        # 30 s caps mean worst-case: 30 + 30 + 5 + 30 + 30 = 125 s — well within 240 s.
        _AVIS_GOTO_TIMEOUT = 30_000

        print("  [Avis] Loading direct results URL via Bright Data...")
        await page.goto(AVIS_RESULTS_URL, timeout=_AVIS_GOTO_TIMEOUT, wait_until="domcontentloaded")
        await dismiss_popups(page)
        print(f"  [Avis] Page: '{await page.title()}' @ {page.url}")

        # If redirected away from vehicle-availability, seed a cookie then retry the direct URL
        if "vehicle-availability" not in page.url:
            print(f"  [Avis] Redirected to {page.url[:60]} — seeding cookie and retrying...")
            await page.goto("https://www.avis.com/en/home", timeout=_AVIS_GOTO_TIMEOUT, wait_until="domcontentloaded")
            await dismiss_popups(page)
            await page.wait_for_timeout(5000)
            await page.goto(AVIS_RESULTS_URL, timeout=_AVIS_GOTO_TIMEOUT, wait_until="domcontentloaded")
            await dismiss_popups(page)
            print(f"  [Avis] Retry page: '{await page.title()}' @ {page.url[:60]}")

        # After retry, if still not on the results page, fail immediately (bot-blocked)
        if "vehicle-availability" not in page.url:
            return make_result("Avis", error="Bot-blocked — redirected away from results page")

        # Wait for vehicle article cards to appear.
        # Avis cards typically render within a few seconds of domcontentloaded;
        # 30 s is generous. If they still aren't there, try a broad article selector.
        try:
            await page.wait_for_selector("article[data-testid*='vehicle'], article:not([data-aue-type])", timeout=30_000)
        except Exception:
            # Broader fallback — any article on availability page
            if "vehicle-availability" in page.url:
                await page.wait_for_selector("article", timeout=15_000)
            else:
                raise

        await page.wait_for_timeout(3000)
        return await _extract_cheapest_suv(page, "Avis")

    except Exception as exc:
        return make_result("Avis", error=str(exc)[:100])
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: BUDGET
# ─────────────────────────────────────────────────────────────────────────────

async def check_budget(playwright) -> Dict:
    """
    Budget — same ABG platform as Avis (BUDGET_RESULTS_URL is AVIS_RESULTS_URL
    with brand=budget and www.budget.com substituted).

    Strategy mirrors check_avis() exactly:
      1. Connect via Bright Data (falls back to local browser if BD not configured).
      2. Navigate directly to BUDGET_RESULTS_URL.
      3. If redirected away from /vehicle-availability, seed a session cookie by
         visiting the Budget homepage, then retry the direct URL.
      4. Wait for <article> vehicle cards (same DOM structure as Avis).
      5. Extract cheapest Full Size SUV via _extract_cheapest_suv().
      6. Fall back to Kayak only if the direct approach fails entirely.

    Routing through Bright Data matches Avis and eliminates intermittent bot-blocking.
    """
    browser = await get_browser(playwright)
    page, ctx = await _new_bd_page(browser)

    try:
        print("  [Budget] Loading direct results URL...")
        print(f"  [Budget] URL: {BUDGET_RESULTS_URL[:120]}")
        # 20 s cap: budget.com through Bright Data consistently fails to load
        # within the global 60 s window, so fail fast and fall through to Kayak.
        await page.goto(BUDGET_RESULTS_URL, timeout=20_000, wait_until="domcontentloaded")
        await dismiss_popups(page)
        print(f"  [Budget] Landed: '{await page.title()}' @ {page.url[:80]}")

        # If redirected away from vehicle-availability, seed cookies then retry
        if "vehicle-availability" not in page.url:
            print(f"  [Budget] Not on results page ({page.url[:60]}) — seeding cookie via homepage...")
            await page.goto("https://www.budget.com/en/home", timeout=20_000, wait_until="domcontentloaded")
            await dismiss_popups(page)
            await page.wait_for_timeout(5000)
            await page.goto(BUDGET_RESULTS_URL, timeout=20_000, wait_until="domcontentloaded")
            await dismiss_popups(page)
            print(f"  [Budget] After retry: '{await page.title()}' @ {page.url[:80]}")

        if "vehicle-availability" not in page.url:
            print(f"  [Budget] Still not on results page after retry — falling back to Kayak")
            return await _check_from_kayak(playwright, "Budget")

        # Poll every 2 s (up to 40 s) for EITHER vehicle cards OR i18n broken render.
        # Budget through Bright Data sometimes renders bare translation keys
        # ("lbl.res.step2.avisfirst.*") instead of vehicle cards — this typically
        # becomes visible after ~5–15 s of JS hydration, well after domcontentloaded.
        # Polling lets us bail to Kayak within 2 s of the i18n content appearing
        # rather than waiting out the full 30 s + 15 s selector timeouts.
        print("  [Budget] Polling for vehicle cards (i18n detection every 2 s, 40 s max)...")
        _poll_start = time.monotonic()
        _article_found = False
        for _poll_i in range(20):   # 20 × 2 s = 40 s ceiling
            _state = await page.evaluate(
                "() => {"
                "  const t = document.body.innerText;"
                "  if (t.includes('lbl.res.step2') || t.includes('msg.res.step2')"
                "      || t.includes('avisfirst.checkAvailabilityFormat')) return 'i18n';"
                "  const sel = 'article[data-testid*=\"vehicle\"], article:not([data-aue-type])';"
                "  if (document.querySelector(sel)) return 'article';"
                "  if (document.querySelector('article')) return 'broad';"
                "  return 'waiting';"
                "}"
            )
            _elapsed = time.monotonic() - _poll_start
            if _state == 'i18n':
                print(f"  [Budget] i18n render detected at {_elapsed:.1f}s — falling back to Kayak")
                return await _check_from_kayak(playwright, "Budget")
            if _state == 'article':
                print(f"  [Budget] Vehicle article cards found at {_elapsed:.1f}s.")
                _article_found = True
                break
            if _state == 'broad':
                print(f"  [Budget] Broad article cards found at {_elapsed:.1f}s.")
                _article_found = True
                break
            await page.wait_for_timeout(2000)

        if not _article_found:
            _elapsed = time.monotonic() - _poll_start
            print(f"  [Budget] No article cards after {_elapsed:.1f}s — extracting body text anyway")

        await page.wait_for_timeout(2000)
        result = await _extract_cheapest_suv(page, "Budget")

        if result.get("error"):
            print(f"  [Budget] Extraction error: {result['error']} — falling back to Kayak")
            return await _check_from_kayak(playwright, "Budget")

        print(f"  [Budget] Direct result: {result.get('car_class')} {result.get('model')} ${result.get('price')}")
        return result

    except Exception as exc:
        print(f"  [Budget] Exception: {str(exc)[:120]} — falling back to Kayak")
        return await _check_from_kayak(playwright, "Budget")
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ENTERPRISE HOLDINGS — SHARED API SESSION (Enterprise / National / Alamo)
# ─────────────────────────────────────────────────────────────────────────────

def _ehi_extract_best(car_classes: list, provider: str) -> tuple:
    """Return (best_price, best_name) from a list of car-class dicts, or (None, '')."""
    best_price: Optional[float] = None
    best_name = ""
    for cc in car_classes:
        name = cc.get("name", "")
        total = cc.get("total")
        if total is None:
            continue
        try:
            price = float(total)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if is_fullsize_suv(name):
            if best_price is None or price < best_price:
                best_price = price
                best_name = name
    return best_price, best_name


async def _ehi_enterprise_api(browser, loc_cfg: Dict, t0: float) -> None:
    """
    Enterprise via enterprise-ewt API (original approach, unchanged logic).
    Loads enterprise.com once to establish Incapsula cookies, then POSTs to
    enterprise-ewt/reservations/initiate with brand=ENTERPRISE.
    """
    home_url = "https://www.enterprise.com/en/home.html"
    api_url  = f"{EH_API_BASE}/reservations/initiate"
    ctx = None
    try:
        page, ctx = await _new_bd_page(browser)
        await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_selector("input", timeout=15_000)
        except Exception:
            await page.wait_for_timeout(5_000)
        print(f"  [Enterprise] Session ready  [{time.monotonic()-t0:.1f}s]")

        loc_obj = {
            "airport_code":    loc_cfg["airport_code"],
            "location_type":   "BRANCH",
            "my_location":     False,
            "gps":             loc_cfg["gps"],
            "name":            loc_cfg["name"],
            "country_code":    loc_cfg["country_code"],
            "group_branch_id": loc_cfg["group_branch_id"],
            "type":            "BRANCH",
            "id":              loc_cfg["id"],
            "time_zone_id":    loc_cfg["time_zone_id"],
        }
        initiate_body = {
            "pickup_location_id":                loc_cfg["id"],
            "return_location":                   loc_obj,
            "renter_age":                        BOOKING["driver_age"],
            "pickup_time":                       f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}",
            "return_location_id":                loc_cfg["id"],
            "pickup_location":                   loc_obj,
            "renter_age_label":                  f"{BOOKING['driver_age']}+",
            "return_time":                       f"{BOOKING['return_date']}T{BOOKING['return_time']}",
            "applied_vehicle_class_filters":     [],
            "country_of_residence_code":         "US",
            "enable_north_american_prepay_rates": False,
            "view_currency_code":                "USD",
            "check_if_no_vehicles_available":    True,
            "check_if_oneway_allowed":           True,
        }
        initiate_body_json = json.dumps(initiate_body)

        t_brand = time.monotonic()
        js = f"""async () => {{
            const body = {initiate_body_json};
            const r = await fetch('{api_url}', {{
                method: 'POST',
                headers: {{
                    'content-type': 'application/json',
                    'accept': 'application/json, text/plain, */*',
                    'brand': 'ENTERPRISE',
                    'channel': 'WEB',
                    'locale': 'en_US',
                    'page_type': 'home',
                    'sofresh': 'SOCLEAN',
                }},
                credentials: 'include',
                body: JSON.stringify(body),
            }});
            const d = await r.json();
            const classes = d?.session?.gbo?.reservation?.car_classes
                         || d?.session?.analytics?.gbo?.reservation?.car_classes
                         || [];
            const EHI_CODE_NAMES = {{{_EHI_CODE_NAMES_JS}}};
            return classes.map(c => ({{
                code:   c.code,
                name:   c.name || EHI_CODE_NAMES[c.code] || '',
                status: c.status || '',
                total:  c?.charges?.{EHI_CHARGE_KEY}?.total_price_view?.amount,
            }}));
        }}"""
        car_classes = await page.evaluate(js)
        elapsed = time.monotonic() - t_brand
        print(f"  [Enterprise] {len(car_classes)} classes  [{elapsed:.1f}s]")

        best_price, best_name = _ehi_extract_best(car_classes, "Enterprise")
        if best_price is None:
            raise Exception(f"No Full Size SUV in {len(car_classes)} classes")

        print(f"  [Enterprise] Best: {best_name} @ ${best_price:.2f}")
        _ehi_cache["Enterprise"] = make_result(
            "Enterprise",
            car_class="Full Size SUV",
            model=best_name,
            price=best_price,
            url=home_url,
        )
    except Exception as exc:
        print(f"  [Enterprise] API error: {str(exc)[:120]} — will form-fill")
        _ehi_cache["Enterprise"] = None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass


async def _ehi_national_api(browser, loc_cfg: Dict, t0: float) -> None:
    """
    National via gma-national/reservations/initiate direct API.

    Confirmed working approach (probe_form_v25 Strategy 6):
    1. Navigate nationalcar.com to establish session cookies (no form fill needed).
    2. POST to gma-national/reservations/initiate from the page context using
       credentials: 'include' so session cookies are sent.
    3. Payload MUST include BOTH pickup_location: {id} object AND top-level
       pickup_location_id string — omitting the top-level field causes
       CROS_RES_PICKUP_LOCATION_REQUIRED.
    4. pickup_time / return_time: full ISO datetime "YYYY-MM-DDTHH:MM".
    5. loc_cfg["national_id"] is the National/Alamo GMA location ID for this
       airport — may differ from the Enterprise location ID (loc_cfg["id"]).
    6. Response path: d.gma.gbo.reservation.car_classes
    7. Do NOT fill the form: autocomplete click navigates the SPA and disrupts
       subsequent page.evaluate fetch calls (TypeError: Failed to fetch).
    """
    home_url = "https://www.nationalcar.com/en/car-rental.html"
    api_url  = "https://prd-east.webapi.nationalcar.com/gma-national/reservations/initiate"
    # Use the National-specific GMA location ID (stored separately from Enterprise ID)
    loc_id   = loc_cfg.get("national_id", loc_cfg["id"])
    ctx      = None

    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}"

    initiate_body = {
        "pickup_location":    {"id": loc_id},
        "return_location":    {"id": loc_id},
        "pickup_location_id": loc_id,
        "return_location_id": loc_id,
        "pickup_time":        pickup_dt,
        "return_time":        return_dt,
        "renter_age":         BOOKING["driver_age"],
        "rate_type":          EHI_CHARGE_KEY,
        "country_of_residence": "US",
        "locale":             "en_US",
        "cor":                "US",
    }
    initiate_body_json = json.dumps(initiate_body)

    try:
        page, ctx = await _new_bd_page(browser)
        # Navigate to establish session cookies — no form fill (autocomplete click
        # disrupts the SPA and breaks subsequent fetch() calls in page context).
        await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3_000)
        print(f"  [National] Session ready (loc_id={loc_id})  [{time.monotonic()-t0:.1f}s]")

        # Direct API call — minimal headers matching working probe_form_v25.
        t_brand = time.monotonic()
        js = f"""async () => {{
            const body = {initiate_body_json};
            const r = await fetch('{api_url}', {{
                method: 'POST',
                credentials: 'include',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }},
                body: JSON.stringify(body),
            }});
            const d = await r.json();
            // National response path: d.gma.gbo.reservation.car_classes
            const classes = d?.gma?.gbo?.reservation?.car_classes
                          || d?.session?.gbo?.reservation?.car_classes
                          || d?.session?.analytics?.gbo?.reservation?.car_classes
                          || [];
            const msgs = (d?.messages || []).map(m => m.code + ':' + (m.tech_message || m.message || '').slice(0, 80));
            const EHI_CODE_NAMES = {{{_EHI_CODE_NAMES_JS}}};
            return {{
                classes: classes.map(c => ({{
                    code:   c.code,
                    name:   c.name || EHI_CODE_NAMES[c.code] || '',
                    status: c.status || '',
                    total:  c?.charges?.{EHI_CHARGE_KEY}?.total_price_view?.amount,
                }})),
                msgs: msgs,
                status: r.status,
            }};
        }}"""
        raw = await page.evaluate(js)
        car_classes = raw.get("classes", []) if isinstance(raw, dict) else []
        api_msgs    = raw.get("msgs", [])    if isinstance(raw, dict) else []
        api_status  = raw.get("status", 0)   if isinstance(raw, dict) else 0
        elapsed = time.monotonic() - t_brand
        print(f"  [National] {len(car_classes)} classes  status={api_status}  [{elapsed:.1f}s]")
        if api_msgs:
            print(f"  [National] API messages: {api_msgs[:4]}")

        best_price, best_name = _ehi_extract_best(car_classes, "National")
        if best_price is None:
            raise Exception(
                f"No Full Size SUV in {len(car_classes)} classes"
                + (f" | msgs={api_msgs[:2]}" if api_msgs else "")
            )

        print(f"  [National] Best: {best_name} @ ${best_price:.2f}")
        _ehi_cache["National"] = make_result(
            "National",
            car_class="Full Size SUV",
            model=best_name,
            price=best_price,
            url=home_url,
        )
    except Exception as exc:
        print(f"  [National] API error: {str(exc)[:120]} — will form-fill")
        _ehi_cache["National"] = None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass


async def _ehi_alamo_form(playwright, loc_cfg: Dict, t0: float) -> None:
    """
    Alamo via gma-alamo/reservations/initiate direct API.

    Opens its own Bright Data browser session to avoid the 2-domain limit
    that applies when sharing a browser with Enterprise + National.

    Mirrors _ehi_national_api — no form fill, just navigate for cookies then POST:
    1. Navigate alamo.com/en/reserve.html#/start to establish session cookies.
    2. POST to gma-alamo/reservations/initiate from page context using
       credentials: 'include' so session cookies are sent.
    3. Payload uses national_id (same GMA location ID as National for this airport).
    4. pickup_time / return_time: full ISO datetime "YYYY-MM-DDTHH:MM".
    5. Response path: d.gma.gbo.reservation.car_classes
    """
    home_url = "https://www.alamo.com/en/reserve.html#/start"
    api_url  = "https://prd-east.webapi.alamo.com/gma-alamo/reservations/initiate"
    loc_id   = loc_cfg.get("alamo_id", loc_cfg.get("national_id", loc_cfg["id"]))
    browser  = None
    ctx      = None

    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}"

    initiate_body = {
        "pickup_location":    {"id": loc_id},
        "return_location":    {"id": loc_id},
        "pickup_location_id": loc_id,
        "return_location_id": loc_id,
        "pickup_time":        pickup_dt,
        "return_time":        return_dt,
        "renter_age":         BOOKING["driver_age"],
        "rate_type":          EHI_CHARGE_KEY,
        "one_way_rental":     False,
        "check_if_no_vehicles_available": False,
        "car_class_codes":    [],
        "country_of_residence": "US",
        "locale":             "en_US",
        "cor":                "US",
    }
    initiate_body_json = json.dumps(initiate_body)

    try:
        # Open own browser session (alamo.com would exceed the 2-domain limit
        # if sharing the Enterprise+National browser)
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        # Navigate to establish session cookies — no form fill needed.
        await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3_000)
        print(f"  [Alamo] Session ready (loc_id={loc_id})  [{time.monotonic()-t0:.1f}s]")

        # Direct API call — mirrors National approach, minimal headers.
        t_brand = time.monotonic()
        js = f"""async () => {{
            const body = {initiate_body_json};
            const r = await fetch('{api_url}', {{
                method: 'POST',
                credentials: 'include',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }},
                body: JSON.stringify(body),
            }});
            const d = await r.json();
            // Alamo response path mirrors National: d.gma.gbo.reservation.car_classes
            const classes = d?.gma?.gbo?.reservation?.car_classes
                          || d?.session?.gbo?.reservation?.car_classes
                          || d?.session?.analytics?.gbo?.reservation?.car_classes
                          || [];
            const msgs = (d?.messages || []).map(m => m.code + ':' + (m.tech_message || m.message || '').slice(0, 80));
            const EHI_CODE_NAMES = {{{_EHI_CODE_NAMES_JS}}};
            return {{
                classes: classes.map(c => ({{
                    code:   c.code,
                    name:   c.name || EHI_CODE_NAMES[c.code] || '',
                    status: c.status || '',
                    total:  c?.charges?.{EHI_CHARGE_KEY}?.total_price_view?.amount,
                }})),
                msgs: msgs,
                status: r.status,
            }};
        }}"""
        raw = await page.evaluate(js)
        car_classes = raw.get("classes", []) if isinstance(raw, dict) else []
        api_msgs    = raw.get("msgs", [])    if isinstance(raw, dict) else []
        api_status  = raw.get("status", 0)   if isinstance(raw, dict) else 0
        elapsed = time.monotonic() - t_brand
        print(f"  [Alamo] {len(car_classes)} classes  status={api_status}  [{elapsed:.1f}s]")
        if api_msgs:
            print(f"  [Alamo] API messages: {api_msgs[:4]}")

        best_price, best_name = _ehi_extract_best(car_classes, "Alamo")
        if best_price is None:
            raise Exception(
                f"No Full Size SUV in {len(car_classes)} classes"
                + (f" | msgs={api_msgs[:2]}" if api_msgs else "")
            )

        print(f"  [Alamo] Best: {best_name} @ ${best_price:.2f}")
        _ehi_cache["Alamo"] = make_result(
            "Alamo",
            car_class="Full Size SUV",
            model=best_name,
            price=best_price,
            url=home_url,
        )
    except Exception as exc:
        print(f"  [Alamo] API error: {str(exc)[:120]} — will form-fill")
        _ehi_cache["Alamo"] = None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


async def _check_ehi_all(playwright) -> None:
    """
    Open ONE Bright Data browser session and populate _ehi_cache for all three
    EHI brands sequentially, each on its own page context.

    Enterprise:  unchanged — enterprise-ewt API from enterprise.com page.
    National:    new — nationalcar.com form + gma-national/reservations/initiate
                 (ISO datetime pickup_time, top-level pickup_location_id).
    Alamo:       new — alamo.com form-fill + session/current extraction.

    Called exclusively through _check_ehi_brand(), which holds _ehi_lock so
    this runs at most once per process.
    """
    airport = BOOKING["airport_code"]
    loc_cfg = EH_LOCATION_CONFIG.get(airport)

    if not loc_cfg:
        print(f"  [EHI] No location config for {airport} — all brands fall back to form-fill")
        for brand in ("Enterprise", "National", "Alamo"):
            _ehi_cache[brand] = None
        return

    print(f"  [EHI] Shared session: {airport} — Enterprise + National + Alamo...")
    t0 = time.monotonic()
    browser = None

    try:
        browser = await get_browser(playwright)

        await _ehi_enterprise_api(browser, loc_cfg, t0)
        await _ehi_national_api(browser, loc_cfg, t0)
        await browser.close()   # close before Alamo opens its own browser (2-domain limit)
        browser = None
        await _ehi_alamo_form(playwright, loc_cfg, t0)

        print(f"  [EHI] All brands done  [{time.monotonic()-t0:.1f}s total]")

    except Exception as exc:
        print(f"  [EHI] Session setup failed: {str(exc)[:120]} — all brands form-fill")
        for brand in ("Enterprise", "National", "Alamo"):
            _ehi_cache.setdefault(brand, None)
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


async def _check_ehi_brand(playwright, provider: str, home_url: str) -> Dict:
    """
    Dispatcher for a single EHI brand.

    On the first call (from whichever of Enterprise/National/Alamo arrives first
    when asyncio.gather runs them concurrently), acquires _ehi_lock and calls
    _check_ehi_all() which populates _ehi_cache for all three brands in one
    shared BD session.  Subsequent callers wait for the lock then read from cache.

    Falls back to _check_eh_brand_direct (form-fill) when:
      - Bright Data not configured
      - No EH location config for this airport
      - The shared session failed for this specific brand (cache value is None)
    """
    if not BRIGHT_DATA_CDP_URL or not EH_LOCATION_CONFIG.get(BOOKING["airport_code"]):
        return await _check_eh_brand_direct(playwright, provider, home_url)

    lock = _get_ehi_lock()
    async with lock:
        if not _ehi_cache:
            await _check_ehi_all(playwright)

    cached = _ehi_cache.get(provider)
    if cached is not None:
        return cached
    # None sentinel → this brand's fetch/form failed; fall back to form-fill
    return await _check_eh_brand_direct(playwright, provider, home_url)


# ─────────────────────────────────────────────────────────────────────────────
# ENTERPRISE HOLDINGS — SHARED FORM-FILL HELPER (National / Enterprise / Alamo)
# ─────────────────────────────────────────────────────────────────────────────

async def _check_eh_brand_direct(playwright, provider: str, home_url: str) -> Dict:
    """
    Enterprise Holdings brands (Enterprise, National, Alamo) — form-fill via Bright Data.

    Flow (confirmed from live browser session):
    1. Navigate to {brand}/en/home.html — booking widget is on this page.
    2. Dismiss cookie/modal overlays.
    3. Call _fill_enterprise_group_form() which:
       - Types airport code into #search-autocomplete__input-PICKUP
       - Selects first dropdown result
       - Sets pickup/return dates via calendar
       - Clicks "CHECK AVAILABILITY" / "Browse Vehicles"
    4. Wait for navigation to {brand}/en/reserve.html#car_select
    5. Extract vehicle prices from body text.

    Returns ERROR on any failure — does NOT fall back to Kayak.
    (National/Enterprise/Alamo are not in the Kayak session.)
    """
    if not BRIGHT_DATA_CDP_URL:
        msg = f"Bright Data not configured — {provider} requires direct EHI API check"
        print(f"  [{provider}] {msg}")
        return make_result(provider, error=msg)

    print(f"  [{provider}] Trying direct form-fill via Bright Data → {home_url}")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        # NOTE: Enterprise Holdings results page (reserve.html#car_select) is
        # session-based — there is NO bookmarkable URL with date/location params.
        # Confirmed via live browser inspection: the booking widget submits via
        # React internal state; the resulting URL is always just:
        #   https://www.{brand}.com/en/reserve.html#car_select
        # with no query string. Loading this URL cold returns the homepage.
        # Therefore form-filling is the only viable approach.
        #
        # Hard limit: _fill_enterprise_group_form can take up to ~60s (calendar
        # navigation + React fiber injection). Wrap in a 90s timeout so it can
        # never cause a >90s hang.

        await page.goto(home_url, timeout=60_000, wait_until="domcontentloaded")
        await dismiss_popups(page)
        await page.wait_for_timeout(2000)
        print(f"  [{provider}] Loaded: '{await page.title()}'")

        # Fill the EH booking form with hard 90s timeout
        try:
            await asyncio.wait_for(_fill_enterprise_group_form(page, brand=provider), timeout=90)
        except asyncio.TimeoutError:
            raise Exception("Form fill timed out after 90s — EH booking widget did not respond")

        # Wait for the SPA to navigate to the vehicle selection page
        try:
            await page.wait_for_url("**/reserve.html**", timeout=20_000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        print(f"  [{provider}] After submit: {page.url[:80]}")

        # Check we landed on the results page (not still on home)
        if "/home" in page.url or "reserve" not in page.url:
            raise Exception(
                f"Form submit did not navigate to results — URL: {page.url[:60]}\n"
                f"  Note: Enterprise Holdings (Enterprise/National/Alamo) reserve.html#car_select\n"
                f"  requires an active browser session; no direct URL bypass exists."
            )

        # Wait for vehicle cards to populate
        try:
            await page.wait_for_selector(
                "[class*='VehicleCard'], [class*='vehicle-card'], [class*='vehicleCard'], "
                "[data-testid*='vehicle'], [class*='car-class']",
                timeout=45_000,
            )
        except Exception:
            print(f"  [{provider}] Vehicle card selector timed out — extracting anyway")

        result = await _extract_direct_suv(page, provider)
        if result.get("error"):
            err = result["error"]
            print(f"  [{provider}] Direct error: {err}")
            return make_result(provider, error=err)
        return result

    except Exception as exc:
        err_msg = str(exc)[:150]
        print(f"  [{provider}] EH direct exception: {err_msg}")
        if "session" in err_msg.lower() or "Form submit" in err_msg or "Form fill" in err_msg:
            print(f"  [{provider}] ℹ️  EHI requires a form session — no direct URL shortcut.")
        return make_result(provider, error=err_msg)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: NATIONAL
# ─────────────────────────────────────────────────────────────────────────────

async def check_national(playwright) -> Dict:
    """
    National Car Rental — nationalcar.com form fill + gma-national direct API.
    Confirmed working: ISO datetime pickup_time + top-level pickup_location_id.
    Response path: d.gma.gbo.reservation.car_classes.
    One BD browser is shared with Enterprise and Alamo via _check_ehi_all().
    Returns ERROR on failure — no Kayak fallback.
    """
    return await _check_ehi_brand(
        playwright, "National", "https://www.nationalcar.com/en/car-rental.html"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: ENTERPRISE
# ─────────────────────────────────────────────────────────────────────────────

async def check_enterprise(playwright) -> Dict:
    """
    Enterprise Rent-A-Car — enterprise-ewt API from enterprise.com (unchanged approach).
    Pricing from d.session.gbo.reservation.car_classes (brand=ENTERPRISE).
    One BD browser is shared with National and Alamo via _check_ehi_all().
    Returns ERROR on failure — no Kayak fallback.
    """
    return await _check_ehi_brand(
        playwright, "Enterprise", "https://www.enterprise.com/en/home.html"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: ALAMO
# ─────────────────────────────────────────────────────────────────────────────

async def check_alamo(playwright) -> Dict:
    """
    Alamo — alamo.com/en/reserve.html#/start form-fill + session/current.
    Confirmed working: form fill → Go → d.gma.gbo.reservation.car_classes.
    One BD browser is shared with Enterprise and National via _check_ehi_all().
    Returns ERROR on failure — no Kayak fallback.
    """
    return await _check_ehi_brand(
        playwright, "Alamo", "https://www.alamo.com/en/reserve.html#/start"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: DOLLAR
# ─────────────────────────────────────────────────────────────────────────────

async def check_dollar(playwright) -> Dict:
    """
    Dollar Car Rental — tries direct Hertz Holdings URL via Bright Data first.

    Dollar has its own station codes, separate from Hertz (e.g. LGA: Dollar=LGAO01).
    When the station is closed/invalid, returns N/A rather than falling back to
    Kayak (which would give misleading pricing from a different airport).

    Direct URL: DOLLAR_RESULTS_URL (Hertz Holdings platform, station from DB).
    """
    if not BRIGHT_DATA_CDP_URL:
        print("  [Dollar] No Bright Data — returning N/A.")
        return make_result("Dollar", error="Dollar requires Bright Data for direct URL checks", na=True)
    if not DOLLAR_RESULTS_URL:
        airport = BOOKING["airport_code"]
        print(f"  [Dollar] No Dollar station for {airport} in locations_db.json — returning N/A.")
        return make_result("Dollar", error=f"No Dollar station configured for {airport}", na=True)

    print(f"  [Dollar] Trying direct URL via Bright Data (station={DOLLAR_STATION_CODE})...")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        # Dollar's /us/en/book/vehicles page loads correctly but shows a cookie
        # consent wall first. We need domcontentloaded (not just commit) so the
        # React SPA hydrates and the cookie banner is rendered before we try to
        # dismiss it.
        try:
            await page.goto(DOLLAR_RESULTS_URL, timeout=60_000, wait_until="domcontentloaded")
        except Exception as goto_exc:
            raise Exception(f"goto timed out or failed: {str(goto_exc)[:60]}")

        # Immediate URL check — if we're not on the vehicle results path, bail now.
        await page.wait_for_timeout(2000)
        final_url = page.url
        print(f"  [Dollar] Landed at: {final_url[:80]}")

        not_results = (
            "book/vehicles" not in final_url
            and "vehicle" not in final_url.lower()
        )
        if not_results:
            # Known-closed or invalid location → N/A, no Kayak fallback
            if "locationClosed" in final_url or "unavailableReason" in final_url:
                import urllib.parse as _up
                reason = _up.parse_qs(_up.urlparse(final_url).query).get("unavailableReason", ["locationClosed"])[0]
                msg = f"Dollar station closed ({reason})"
                print(f"  [Dollar] {msg}")
                return make_result("Dollar", error=msg, na=True)
            raise Exception(f"Not a vehicle results page: {final_url[:60]}")

        # Dismiss cookie banner — Dollar uses "Accept Cookies" button text.
        # Explicitly try Dollar-specific cookie button first, then fall through to
        # the generic dismiss_popups which also covers "Accept Cookies".
        for _cookie_sel in [
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept')",
            "#onetrust-accept-btn-handler",
        ]:
            try:
                _btn = page.locator(_cookie_sel).first
                if await _btn.is_visible(timeout=3000):
                    await _btn.click(timeout=4000)
                    print(f"  [Dollar] Cookie banner dismissed via: {_cookie_sel}")
                    await page.wait_for_timeout(5000)  # wait for vehicles to load after cookie
                    break
            except Exception:
                pass
        else:
            # Generic dismiss as fallback
            await dismiss_popups(page)
            await page.wait_for_timeout(3000)

        # Quick check for location-unavailable errors before burning 20 s on a selector.
        # Dollar shows e.g. "5 - INVALID PICKUP LOCATION" when the station code is wrong
        # or the location is closed.  Detect it early and bail cleanly.
        await page.wait_for_timeout(1500)
        _dollar_err = await page.evaluate(
            "() => {"
            "  const t = document.body.innerText.toUpperCase();"
            "  if (t.includes('INVALID PICKUP LOCATION'))  return 'INVALID PICKUP LOCATION';"
            "  if (t.includes('INVALID DROPOFF LOCATION')) return 'INVALID DROPOFF LOCATION';"
            "  if (t.includes('LOCATION IS NOT AVAILABLE')) return 'LOCATION NOT AVAILABLE';"
            "  if (t.includes('LOCATION NOT AVAILABLE'))   return 'LOCATION NOT AVAILABLE';"
            "  if (t.includes('NOT AVAILABLE AT THIS LOCATION')) return 'NOT AVAILABLE AT THIS LOCATION';"
            "  return null;"
            "}"
        )
        if _dollar_err:
            msg = f"Dollar location error: {_dollar_err} (station={DOLLAR_STATION_CODE})"
            print(f"  [Dollar] {msg}")
            return make_result("Dollar", error=msg, na=True)

        # Wait for vehicle cards — Dollar uses its own CSS classes, so also try text-based wait
        try:
            await page.wait_for_selector(
                "[class*='vehicle'], [class*='Vehicle'], [class*='MuiCard'], "
                "[class*='car-card'], [class*='CarCard']",
                timeout=20_000,
            )
        except Exception:
            print("  [Dollar] Vehicle card selector timed out — extracting anyway")

        result = await _extract_direct_suv(page, "Dollar")
        if result.get("error"):
            raise Exception(result["error"])
        return result

    except Exception as exc:
        err = str(exc)[:100]
        print(f"  [Dollar] Direct check failed ({err}) — returning N/A")
        return make_result("Dollar", error=err)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER: THRIFTY
# ─────────────────────────────────────────────────────────────────────────────

async def check_thrifty(playwright) -> Dict:
    """
    Thrifty Car Rental — same Hertz Holdings platform as Dollar.

    Thrifty has its own station codes in locations_db.json (separate from Hertz/Dollar).
    When no Thrifty station exists at the airport, returns N/A cleanly.
    """
    if not BRIGHT_DATA_CDP_URL:
        print("  [Thrifty] No Bright Data — returning N/A.")
        return make_result("Thrifty", error="Thrifty requires Bright Data for direct URL checks", na=True)
    if not THRIFTY_RESULTS_URL:
        airport = BOOKING["airport_code"]
        print(f"  [Thrifty] No Thrifty station for {airport} in locations_db.json — returning N/A.")
        return make_result("Thrifty", error=f"No Thrifty station configured for {airport}", na=True)

    print(f"  [Thrifty] Trying direct URL via Bright Data (station={THRIFTY_STATION_CODE})...")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)
        try:
            await page.goto(THRIFTY_RESULTS_URL, timeout=60_000, wait_until="domcontentloaded")
        except Exception as goto_exc:
            raise Exception(f"goto timed out or failed: {str(goto_exc)[:60]}")

        await page.wait_for_timeout(2000)
        final_url = page.url
        print(f"  [Thrifty] Landed at: {final_url[:80]}")
        if "book/vehicles" not in final_url and "vehicle" not in final_url.lower():
            # Known-closed or invalid location → N/A, no Kayak fallback
            if "locationClosed" in final_url or "unavailableReason" in final_url:
                import urllib.parse as _up
                reason = _up.parse_qs(_up.urlparse(final_url).query).get("unavailableReason", ["locationClosed"])[0]
                msg = f"Thrifty station closed ({reason})"
                print(f"  [Thrifty] {msg}")
                return make_result("Thrifty", error=msg, na=True)
            raise Exception(f"Not a vehicle results page: {final_url[:60]}")

        # Dismiss Thrifty cookie banner (same platform as Dollar)
        for _cookie_sel in [
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept')",
            "#onetrust-accept-btn-handler",
        ]:
            try:
                _btn = page.locator(_cookie_sel).first
                if await _btn.is_visible(timeout=3000):
                    await _btn.click(timeout=4000)
                    print(f"  [Thrifty] Cookie banner dismissed via: {_cookie_sel}")
                    await page.wait_for_timeout(5000)
                    break
            except Exception:
                pass
        else:
            await dismiss_popups(page)
            await page.wait_for_timeout(3000)

        try:
            await page.wait_for_selector(
                "[class*='vehicle'], [class*='Vehicle'], [class*='MuiCard'], "
                "[class*='car-card'], [class*='CarCard']",
                timeout=20_000,
            )
        except Exception:
            print("  [Thrifty] Vehicle card selector timed out — extracting anyway")

        result = await _extract_direct_suv(page, "Thrifty")
        if result.get("error"):
            raise Exception(result["error"])
        return result

    except Exception as exc:
        err = str(exc)[:100]
        print(f"  [Thrifty] Direct check failed ({err}) — returning N/A")
        return make_result("Thrifty", error=err)
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATOR: KAYAK (shared source for Hertz, Budget, National, Enterprise,
#                    Alamo, Dollar, Thrifty — individual sites are bot-blocked)
# ─────────────────────────────────────────────────────────────────────────────

_CAR_MODEL_SKIP = {
    "share", "save", "compare", "search", "see details",
    "edit search form", "go to next result", "go to previous result",
    "go to price", "more information", "free cancellation",
    "view deal", "book", "total",
}

def _get_car_model(lines: List[str], suv_line_idx: int) -> str:
    """
    Walk back from the SUV class line to find the car model name.
    Skips UI control words and returns empty string if none found within 3 lines.
    """
    for offset in range(1, 4):
        idx = suv_line_idx - offset
        if idx < 0:
            break
        candidate = lines[idx].strip()
        cl = candidate.lower()
        if cl in _CAR_MODEL_SKIP or cl.startswith("go to") or cl.startswith("avis ") or cl.startswith("enjoy "):
            continue
        if candidate and not candidate.startswith("$"):
            return candidate
    return ""


def _parse_kayak_body(body: str, url: str) -> Dict[str, Dict]:
    """
    Parse Kayak full-page body text to extract provider → result.

    Kayak result card structure (confirmed from live DOM):
      [Car model name]            ← 1-2 lines above SUV class
      [or similar Full-size SUV]  ← our anchor (is_fullsize_suv match)
      [seats] [bags] [doors]
      [LGA: New York LaGuardia]
      [Shuttle | Airport terminal]
      [rating]  [label]
      [Free cancellation?]
      [More information]
      [Compare]
      [$total]
      [Total]
      [View Deal | Book]          ← deal marker
      [Booking source 1]          ← provider name lines follow
      [$price1]
      [Booking source 2]
      [$price2]
      ...
      [Book direct:]              (optional)
      [$X with ProviderName]      (optional — "Book direct: $460 with Hertz")
      [Go to next result]

    Strategy: find Full Size SUV class line, look FORWARD for providers.
    """
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    results: Dict[str, Dict] = {}
    FORWARD = 35  # lines to scan after SUV anchor

    for i, line in enumerate(lines):
        if not is_fullsize_suv(line):
            continue

        forward = lines[i:min(len(lines), i + FORWARD)]

        # Find deal marker
        deal_idx = next(
            (j for j, ln in enumerate(forward) if ln in ("View Deal", "Book")),
            None,
        )
        if deal_idx is None:
            continue

        # Booking source lines appear after "View Deal"/"Book"
        booking = forward[deal_idx + 1:]

        # Collect all booking-source prices to find the cheapest for this card
        booking_prices: List[float] = []

        j = 0
        while j < len(booking):
            bl = booking[j]
            bl_lower = bl.lower().strip()

            # Stop at next-result marker
            if "go to" in bl_lower or "show more" in bl_lower:
                break

            # Pattern 1: "Book direct:" / "$X with ProviderName"
            if bl_lower in ("book direct:", "book direct"):
                if j + 1 < len(booking):
                    m = re.search(r'\$[\d,]+(?:\.\d+)?\s+with\s+(.+)', booking[j + 1], re.IGNORECASE)
                    if m:
                        pname = m.group(1).strip().lower()
                        canonical = _KAYAK_NAME_MAP.get(pname)
                        if canonical and canonical in _KAYAK_TARGETS:
                            price = parse_price(booking[j + 1])
                            if price and 100 < price < 8000:
                                if canonical not in results or price < results[canonical]["price"]:
                                    car_model = _get_car_model(lines, i)
                                    r = make_result(
                                        canonical,
                                        car_class="Full Size SUV",
                                        model=(car_model or line)[:40],
                                        price=price,
                                        url=url,
                                    )
                                    r["kayak_class_raw"] = line  # exact class label from Kayak
                                    results[canonical] = r
                j += 1
                continue

            # Pattern 2: "[Provider name]" / "[$price]" alternating pairs
            if j + 1 < len(booking):
                price = parse_price(booking[j + 1])
                if price and 100 < price < 8000:
                    # Track for cheapest-overall calculation
                    booking_prices.append(price)
                    # Check if it's one of our target providers
                    canonical = _KAYAK_NAME_MAP.get(bl_lower)
                    if canonical and canonical in _KAYAK_TARGETS:
                        if canonical not in results or price < results[canonical]["price"]:
                            car_model = _get_car_model(lines, i)
                            r = make_result(
                                canonical,
                                car_class="Full Size SUV",
                                model=(car_model or line)[:40],
                                price=price,
                                url=url,
                            )
                            r["kayak_class_raw"] = line  # exact class label from Kayak
                            results[canonical] = r

            j += 1

        # Track cheapest Full Size SUV from ANY source (OTA or direct)
        # This is stored under "__kayak_best__" and surfaced as the "Kayak" provider.
        if booking_prices:
            cheapest = min(booking_prices)
            if "__kayak_best__" not in results or cheapest < results["__kayak_best__"]["price"]:
                car_model = _get_car_model(lines, i)
                r = make_result(
                    "Kayak",
                    car_class="Full Size SUV",
                    model=(car_model or line)[:40],
                    price=cheapest,
                    url=url,
                )
                r["kayak_class_raw"] = line  # exact class label from Kayak
                results["__kayak_best__"] = r

    return results


def _kayak_on_results_page(url: str) -> bool:
    """True if the URL looks like a Kayak search-results page, not the homepage."""
    # Results URL is typically: kayak.com/cars/LGA/2026-05-02/2026-05-06/t/fullsize?...
    # or includes a session token segment after the airport/dates
    return (
        "kayak.com/cars/" in url
        and url.rstrip("/") != "https://www.kayak.com/cars"
        and len(url) > len("https://www.kayak.com/cars/LGA/")
    )


async def _fill_kayak_form(page) -> None:
    """
    Fill the Kayak car search form and submit.
    Used as a fallback when the direct search URL redirects to the homepage.
    """
    print("  [Kayak] Filling search form...")
    await page.wait_for_timeout(2000)

    # — Pickup location —
    # Kayak's location input varies; try several selectors
    loc_filled = False
    for sel in [
        "input[placeholder*='Airport']",
        "input[placeholder*='airport']",
        "input[placeholder*='location']",
        "input[placeholder*='city']",
        "input[aria-label*='ickup']",
        "input[aria-label*='ocation']",
        "[data-testid*='location'] input",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.click(timeout=3000)
                await el.fill("")
                await page.keyboard.type(BOOKING["airport_code"], delay=120)
                await page.wait_for_timeout(2500)
                # Select first autocomplete option
                for opt_sel in ["[role='option']:first-child", "[class*='option']:first-child",
                                "[class*='suggest']:first-child", "li[class*='result']:first-child"]:
                    try:
                        opt = page.locator(opt_sel).first
                        if await opt.is_visible(timeout=1500):
                            await opt.click(timeout=3000)
                            print(f"  [Kayak] Location selected via {sel}")
                            loc_filled = True
                            break
                    except Exception:
                        pass
                if not loc_filled:
                    await page.keyboard.press("ArrowDown")
                    await page.keyboard.press("Enter")
                    loc_filled = True
                break
        except Exception:
            continue

    if not loc_filled:
        print("  [Kayak] WARNING: Could not fill location input")

    await page.wait_for_timeout(1000)

    # — Pickup date — use JS to set a date input or click a calendar button —
    _pu = BOOKING["pickup_date"]   # "2026-05-02"
    _re = BOOKING["return_date"]   # "2026-05-06"

    # Try to find pickup/return date inputs
    for pu_sel, pu_val in [("input[name*='pickup'][type='date']", _pu),
                            ("input[placeholder*='Pick-up']", _pu),
                            ("input[aria-label*='ickup']", _pu)]:
        try:
            el = page.locator(pu_sel).first
            if await el.is_visible(timeout=1500):
                await el.fill(pu_val)
                print(f"  [Kayak] Set pickup date via {pu_sel}")
                break
        except Exception:
            pass

    for re_sel, re_val in [("input[name*='dropoff'][type='date']", _re),
                            ("input[name*='return'][type='date']", _re),
                            ("input[placeholder*='Drop-off']", _re),
                            ("input[aria-label*='rop']", _re)]:
        try:
            el = page.locator(re_sel).first
            if await el.is_visible(timeout=1500):
                await el.fill(re_val)
                print(f"  [Kayak] Set return date via {re_sel}")
                break
        except Exception:
            pass

    await page.wait_for_timeout(500)

    # — Submit —
    for submit_sel in ["button[type='submit']", "button:has-text('Search')",
                       "input[type='submit']", "[aria-label*='earch']"]:
        try:
            btn = page.locator(submit_sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                print(f"  [Kayak] Clicked search button ({submit_sel})")
                break
        except Exception:
            pass

    # Wait for navigation to results
    try:
        await page.wait_for_url(
            lambda url: _kayak_on_results_page(url),
            timeout=30_000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(5000)


async def _dump_kayak_raw_body(ctx, label: str, url: str) -> None:
    """
    Open a fresh browser tab, navigate to url, and print the first 3000 chars of
    page body text.  Used as last-resort diagnosis when a Kayak tab returns empty
    even after relaxed retry.
    """
    pg = await ctx.new_page()
    await _apply_stealth(pg)
    try:
        await pg.goto(url, timeout=TIMEOUT_MS * 2, wait_until="domcontentloaded")
        await pg.wait_for_timeout(8000)
        await dismiss_popups(pg)
        body = await pg.inner_text("body")
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        print(f"\n  [Kayak/{label}] RAW BODY DUMP ({len(lines)} lines, first 60):")
        for ln in lines[:60]:
            print(f"    {ln}")
        # Also print any lines containing "SUV", "full", or price-like values
        suv_lines = [(i, ln) for i, ln in enumerate(lines)
                     if any(kw in ln.lower() for kw in ["suv", "full size", "fullsize", "suburban"])]
        if suv_lines:
            print(f"  [Kayak/{label}] SUV-related lines found:")
            for i, ln in suv_lines[:15]:
                print(f"    [{i:4d}] {ln}")
        else:
            print(f"  [Kayak/{label}] No SUV/full-size lines found in page body")
    except Exception as exc:
        print(f"  [Kayak/{label}] Raw dump failed: {exc}")
    finally:
        try:
            await pg.close()
        except Exception:
            pass


async def _fetch_kayak_results(playwright) -> Dict[str, Dict]:
    """
    Fetch Kayak results in PARALLEL browser tabs within one shared browser context.
    All searches apply BOOKING filter preferences via _build_kayak_fs_param().

    Tabs opened (max 4):
      1. "best"    — all agencies, load all results → __kayak_best__
      2. "Budget"  — agency-filtered (caragency=budget)
      3. "Dollar"  — agency-filtered (caragency=dollar)
      4. "Thrifty" — agency-filtered (caragency=thrifty)

    Hertz / National / Enterprise / Alamo are NOT fetched here — each has a
    working direct API check and returns ERROR on failure instead of Kayak fallback.

    Results cached in _kayak_cache; Budget/Dollar/Thrifty share one session.
    Tabs are staggered by KAYAK_TAB_STAGGER_S seconds to reduce bot-detection risk.
    """
    global _kayak_cache
    if _kayak_cache is not None:
        return _kayak_cache

    _pu = BOOKING["pickup_date"]
    _re = BOOKING["return_date"]
    if not KAYAK_LOCATION_ID:
        print(f"  [Kayak] No Kayak location ID for {BOOKING['airport_code']} — cannot fetch results.")
        _kayak_cache = {}
        return _kayak_cache
    base = f"https://www.kayak.com/cars/{KAYAK_LOCATION_ID}/{_pu}/{_re}"

    KAYAK_TAB_STAGGER_S = 2.0   # seconds between opening each parallel tab

    browser = await get_browser(playwright)   # routes through Bright Data CDP on server
    # Bright Data CDP: must reuse existing context — creating a new one
    # with custom headers raises "forbidden" errors. Local: create a stealth ctx.
    ctx = browser.contexts[0] if (BRIGHT_DATA_CDP_URL and browser.contexts) else await _new_context(browser)
    _ctx_owned = not (BRIGHT_DATA_CDP_URL and browser.contexts)
    results: Dict[str, Dict] = {}

    async def _fetch_one(
        label: str, url: str, load_all: bool = False, delay_s: float = 0.0
    ) -> Dict[str, Dict]:
        """
        Open a browser tab, navigate to url, parse results, close tab.
        delay_s staggers tab-opening to reduce Kayak bot-detection risk.
        Always returns a dict (empty on failure).
        """
        if delay_s:
            await asyncio.sleep(delay_s)
        pg = await ctx.new_page()
        if _ctx_owned:
            await _apply_stealth(pg)   # skip in CDP mode — Bright Data forbids header overrides
        try:
            print(f"  [Kayak/{label}] Loading: {url}")
            await pg.goto(url, timeout=TIMEOUT_MS * 2, wait_until="domcontentloaded")
            await pg.wait_for_timeout(6000)
            await dismiss_popups(pg)
            print(f"  [Kayak/{label}] Page: '{await pg.title()}' @ {pg.url[:80]}")

            if load_all:
                for attempt in range(10):
                    await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await pg.wait_for_timeout(1500)
                    try:
                        more_btn = pg.get_by_text("Show more results", exact=True).first
                        if await more_btn.is_visible(timeout=1500):
                            await more_btn.click(timeout=3000)
                            print(f"  [Kayak/{label}] Clicked 'Show more results' "
                                  f"(attempt {attempt + 1})")
                            await pg.wait_for_timeout(2000)
                        else:
                            break
                    except Exception:
                        break
            else:
                await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await pg.wait_for_timeout(3000)
                try:
                    more_btn = pg.get_by_text("Show more results", exact=True).first
                    if await more_btn.is_visible(timeout=1500):
                        await more_btn.click(timeout=3000)
                        await pg.wait_for_timeout(2000)
                        await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await pg.wait_for_timeout(2000)
                except Exception:
                    pass

            await dismiss_popups(pg)
            body       = await pg.inner_text("body")
            body_lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
            print(f"  [Kayak/{label}] Body: {len(body)} chars, {len(body_lines)} lines")

            for j, ln in enumerate(body_lines):
                if is_fullsize_suv(ln):
                    snippet = body_lines[max(0, j - 1):j + 20]
                    print(f"  [Kayak/{label}] SUV @ line {j}: {snippet}")

            return _parse_kayak_body(body, pg.url)

        except Exception as exc:
            print(f"  [Kayak/{label}] Exception: {exc}")
            return {}
        finally:
            try:
                await pg.close()
            except Exception:
                pass

    try:
        # Build all 8 (label, url, load_all, delay) tuples.
        # Agency tabs include: full-filter URL + relaxed URL (no carpolicies=cancel).
        # The relaxed URL is used as a fallback if the full-filter tab returns empty.
        fs_all    = _build_kayak_fs_param()                        # no agency filter
        all_url   = f"{base}?sort=rank_a&fs={fs_all}"
        # fetch_specs: (label, url, relaxed_url_or_None, load_all, delay)
        fetch_specs = [("best", all_url, None, True, 0.0)]         # tab 0: all results

        for idx, (provider, slug) in enumerate(_KAYAK_AGENCY_SLUGS.items(), start=1):
            fs_full    = _build_kayak_fs_param(agency_slug=slug, with_free_cancel=True)
            fs_relaxed = _build_kayak_fs_param(agency_slug=slug, with_free_cancel=False)
            url_full    = f"{base}?sort=rank_a&fs={fs_full}"
            url_relaxed = f"{base}?sort=rank_a&fs={fs_relaxed}"
            fetch_specs.append((provider, url_full, url_relaxed, False, idx * KAYAK_TAB_STAGGER_S))

        # ── Launch all tabs in parallel ────────────────────────────────────
        print(f"  [Kayak] Launching {len(fetch_specs)} parallel tabs "
              f"(staggered {KAYAK_TAB_STAGGER_S}s each)...")
        tasks = [
            _fetch_one(label, url, load_all, delay)
            for label, url, _relaxed, load_all, delay in fetch_specs
        ]
        all_parsed = await asyncio.gather(*tasks, return_exceptions=True)

        # ── Merge results (pass 1) ─────────────────────────────────────────
        empty_agencies: List[tuple] = []   # (label, relaxed_url) for retry pass

        for (label, _url, relaxed_url, _load_all, _delay), parsed in zip(fetch_specs, all_parsed):
            if isinstance(parsed, Exception):
                print(f"  [Kayak/{label}] Task raised exception: {parsed}")
                parsed = {}

            if label == "best":
                if "__kayak_best__" in parsed:
                    results["__kayak_best__"] = parsed["__kayak_best__"]
                    r = results["__kayak_best__"]
                    raw = r.get("kayak_class_raw", "")
                    print(f"  [Kayak] best: ${r['price']}  model='{r['model']}'  "
                          f"class='{raw}'")
                else:
                    print("  [Kayak] best: no Full Size SUV on unfiltered page")
            else:
                best = parsed.get("__kayak_best__") or parsed.get(label)
                if best:
                    results[label] = {**best, "provider": label}
                    raw = results[label].get("kayak_class_raw", "")
                    print(f"  [Kayak] {label}: ${results[label]['price']}"
                          f"  model='{results[label]['model']}'  class='{raw}'")
                else:
                    print(f"  [Kayak] {label}: empty — will retry without carpolicies=cancel")
                    if relaxed_url:
                        empty_agencies.append((label, relaxed_url))

        # ── Retry pass: empty agency tabs without carpolicies=cancel ──────
        if empty_agencies:
            print(f"\n  [Kayak] Retrying {len(empty_agencies)} empty tabs "
                  f"(relaxed filter — no free-cancel requirement)...")
            # Print raw body of first empty tab for diagnosis before retry
            _debug_label, _debug_url = empty_agencies[0]
            retry_tasks = [
                _fetch_one(f"{lbl}[relaxed]", url, False, i * 1.5)
                for i, (lbl, url) in enumerate(empty_agencies)
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)

            for (label, _url), parsed in zip(empty_agencies, retry_results):
                if isinstance(parsed, Exception):
                    print(f"  [Kayak/{label}] Retry exception: {parsed}")
                    parsed = {}
                best = parsed.get("__kayak_best__") or parsed.get(label)
                if best:
                    results[label] = {**best, "provider": label,
                                      "note": "no free-cancel filter applied"}
                    raw = results[label].get("kayak_class_raw", "")
                    print(f"  [Kayak] {label} (relaxed): ${results[label]['price']}"
                          f"  model='{results[label]['model']}'  class='{raw}'")
                else:
                    print(f"  [Kayak] {label}: still empty after relaxed retry")
                    # ── Last resort: dump raw page body snippet for diagnosis ──
                    await _dump_kayak_raw_body(ctx, f"{label}[raw]", _url)

        _kayak_cache = results

    except Exception as exc:
        print(f"  [Kayak] Exception in outer fetch: {exc}")
        _kayak_cache = {}
    finally:
        try:
            if not _ctx_owned:  # don't close the CDP default context we don't own
                pass
            else:
                await ctx.close()
        except Exception:
            pass
        await browser.close()

    return _kayak_cache


async def _check_from_kayak(playwright, provider: str) -> Dict:
    """Return a provider's result from the shared Kayak cache."""
    cache = await _fetch_kayak_results(playwright)
    result = cache.get(provider)
    if result:
        return result
    return make_result(provider, error="No Full Size SUV found on Kayak for this agency")


def _sanity_check_kayak_prices(cache: Dict[str, Dict]) -> None:
    """
    Flag any Kayak result whose price is more than 3× the cheapest result found.
    Prints a warning with the model/price so the match can be manually verified.
    A suspiciously high price usually means the wrong car class was matched.
    """
    prices = {
        k: v["price"]
        for k, v in cache.items()
        if v.get("price") and k != "__kayak_best__"
    }
    if len(prices) < 2:
        return  # not enough data points to compare

    min_price = min(prices.values())
    threshold = min_price * 3.0

    suspicious = {p: pr for p, pr in prices.items() if pr > threshold}
    if not suspicious:
        return

    print(f"\n  ⚠️  SANITY CHECK — {len(suspicious)} suspiciously high Kayak prices "
          f"(>{3}× cheapest ${min_price:.0f}):")
    for provider, price in sorted(suspicious.items(), key=lambda x: x[1], reverse=True):
        entry = cache[provider]
        raw_cls = entry.get("kayak_class_raw", "")
        print(f"      {provider:<12} ${price:.0f}  model='{entry.get('model', '?')}'  "
              f"kayak_class='{raw_cls}'  "
              f"→ verify at {entry.get('url', '')[:60]}")


async def check_kayak(playwright) -> Dict:
    """
    Return the cheapest Full Size SUV available on Kayak from any booking source.
    This covers the gap when individual providers (Hertz, Budget, etc.) are blocked
    or not listed directly on Kayak — OTA resellers often offer lower rates.
    """
    print("  [Kayak Best] Fetching cheapest Full Size SUV from Kayak...")
    cache = await _fetch_kayak_results(playwright)
    best = cache.get("__kayak_best__")
    if best:
        return {**best, "provider": "Kayak"}
    return make_result("Kayak", error="No Full Size SUV found on Kayak")


async def fetch_nearby_kayak_prices(
    playwright,
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch the cheapest Full Size SUV price at each nearby airport via Kayak.
    Uses the same dates and filters as the main search.

    Args:
        nearby_locations: output of discover_nearby_locations() — we read
                          the "Kayak" sub-list for kayak_location_id values.
    Returns:
        {airport_code: cheapest_price_float}  — empty dict if nothing found.
    """
    kayak_locs = nearby_locations.get("Kayak", [])
    if not kayak_locs:
        print("  [NearbyKayak] No Kayak nearby locations to fetch.")
        return {}

    _pu = BOOKING["pickup_date"]
    _re = BOOKING["return_date"]
    # Use a relaxed filter (no carpolicies=cancel) for nearby airports —
    # smaller inventory means the strict filter often returns empty.
    fs = _build_kayak_fs_param(with_free_cancel=False)

    browser = await get_browser(playwright)   # routes through Bright Data CDP on server
    # Bright Data CDP: must reuse existing context — creating a new one
    # with custom headers raises "forbidden" errors. Local: create a stealth ctx.
    _cdp_mode = bool(BRIGHT_DATA_CDP_URL and browser.contexts)
    ctx = browser.contexts[0] if _cdp_mode else await _new_context(browser)
    prices: Dict[str, float] = {}

    async def _fetch_airport(loc: Dict, delay_s: float) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        lkey   = loc.get("location_key") or loc["airport_code"]
        loc_id = loc["kayak_location_id"]
        url    = f"https://www.kayak.com/cars/{loc_id}/{_pu}/{_re}?sort=rank_a&fs={fs}"
        pg     = await ctx.new_page()
        if not _cdp_mode:
            await _apply_stealth(pg)   # skip in CDP mode — Bright Data forbids header overrides
        try:
            print(f"  [NearbyKayak/{lkey}] Loading: {url}")
            await pg.goto(url, timeout=TIMEOUT_MS * 2, wait_until="domcontentloaded")
            await pg.wait_for_timeout(7000)
            await dismiss_popups(pg)
            # Scroll and try to load more results
            await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pg.wait_for_timeout(2500)
            try:
                more = pg.get_by_text("Show more results", exact=True).first
                if await more.is_visible(timeout=1500):
                    await more.click(timeout=3000)
                    await pg.wait_for_timeout(2000)
            except Exception:
                pass

            body   = await pg.inner_text("body")
            parsed = _parse_kayak_body(body, pg.url)
            best   = parsed.get("__kayak_best__")
            if best and best.get("price"):
                prices[lkey] = best["price"]
                raw = best.get("kayak_class_raw", "")
                print(f"  [NearbyKayak/{lkey}] Best: ${best['price']:.2f}"
                      f"  model='{best.get('model', '')}'"
                      f"  class='{raw}'")
            else:
                print(f"  [NearbyKayak/{lkey}] No Full Size SUV found — printing page snippet:")
                blines = [ln.strip() for ln in body.split("\n") if ln.strip()]
                for ln in blines[:30]:
                    print(f"    {ln}")
        except Exception as exc:
            print(f"  [NearbyKayak/{lkey}] Exception: {exc}")
        finally:
            try:
                await pg.close()
            except Exception:
                pass

    tasks = [_fetch_airport(loc, i * 2.5) for i, loc in enumerate(kayak_locs)]
    await asyncio.gather(*tasks, return_exceptions=True)

    try:
        if not _cdp_mode:   # don't close the CDP default context we don't own
            await ctx.close()
    except Exception:
        pass
    await browser.close()

    return prices


# ─────────────────────────────────────────────────────────────────────────────
# NEARBY PRICE FETCHERS — HERTZ & EHI
# ─────────────────────────────────────────────────────────────────────────────

def _build_hertz_url(station_code: str) -> str:
    """Build a Hertz betafunnel URL for an arbitrary station code."""
    return (
        "https://www.hertz.com/us/en/book/vehicles"
        "?pid={station}"
        "&pdate={pickup_date}T{pickup_time}:00"
        "&did={station}"
        "&ddate={return_date}T{return_time}:00"
        "&pCountryCode=US"
        "&age={age}"
    ).format(
        station=station_code,
        pickup_date=BOOKING["pickup_date"],
        pickup_time=BOOKING["pickup_time"],
        return_date=BOOKING["return_date"],
        return_time=BOOKING["return_time"],
        age=BOOKING["driver_age"],
    )


def _hertz_extract_best(vehicle_data: list) -> tuple:
    """
    Extract cheapest Full Size SUV price from Hertz vehicle-rates data.
    Returns (price_float, model_str) or (None, "").
    """
    _active_cls = CAR_CLASS_EQUIVALENTS.get(ACTIVE_CAR_CLASS, {})
    sipp_codes: Set[str] = set(_active_cls.get("hertz_sipp_codes", {"FFAR", "FFDR"}))

    def _is_fullsize(v: dict) -> bool:
        sipp = v.get("sipp_code", "")
        if sipp in sipp_codes:
            return True
        cat  = (v.get("vehicle_category") or "").lower()
        body = v.get("vehicle_body_type") or []
        return cat == "fullsize" and "SUV" in body

    best_price: Optional[float] = None
    best_name = ""
    for v in vehicle_data:
        if not _is_fullsize(v):
            continue
        name = v.get("vehicle_display_name") or v.get("sipp_code") or "Full Size SUV"
        for rate in v.get("pricing", {}).values():
            if HERTZ_RATE_TYPE and rate.get("rate_type") != HERTZ_RATE_TYPE:
                continue
            total = rate.get("approximate_total")
            if total is None:
                continue
            try:
                price = float(total)
            except (TypeError, ValueError):
                continue
            if price > 0 and (best_price is None or price < best_price):
                best_price = price
                best_name = name
    return best_price, best_name


async def _fetch_one_hertz_station(
    playwright,
    station: str,
    location_key: str,
    display_name: str = "",
) -> tuple:
    """
    Fetch Hertz vehicle-rates for a single station in its own Bright Data context.
    Returns (location_key, best_price_or_None).
    """
    url  = _build_hertz_url(station)
    label = display_name or location_key
    print(f"  [NearbyHertz/{label}] station={station}  {url[:80]}")
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        vehicle_data: list = []
        ready = asyncio.Event()

        async def _on_resp(response, _ev=ready, _vd=vehicle_data):
            if "vehicle-rates" not in response.url:
                return
            try:
                body = await response.body()
                parsed = json.loads(body.decode("utf-8"))
                _vd.extend(parsed if isinstance(parsed, list) else [parsed])
            except Exception:
                pass
            finally:
                _ev.set()

        page.on("response", _on_resp)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            try:
                await asyncio.wait_for(ready.wait(), timeout=40.0)
            except asyncio.TimeoutError:
                print(f"  [NearbyHertz/{label}] vehicle-rates not seen within 40s")
            page.remove_listener("response", _on_resp)

            best_price, best_name = _hertz_extract_best(vehicle_data)
            if best_price:
                print(f"  [NearbyHertz/{label}] Best: {best_name} @ ${best_price:.2f}")
                return location_key, best_price
            else:
                sipp_list = [v.get("sipp_code") for v in vehicle_data]
                print(f"  [NearbyHertz/{label}] No Full Size SUV found "
                      f"(sipp codes seen: {sipp_list[:6]})")
                return location_key, None
        except Exception as exc:
            page.remove_listener("response", _on_resp)
            print(f"  [NearbyHertz/{label}] Error: {exc!s:.100}")
            return location_key, None
    except Exception as exc:
        print(f"  [NearbyHertz/{label}] Session setup failed: {exc!s:.100}")
        return location_key, None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass


async def fetch_nearby_hertz_prices(
    playwright,
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch Hertz Full Size SUV prices at the closest nearby locations (airports
    and city branches).  Each station gets its own Bright Data browser context
    and runs in parallel via asyncio.gather.

    Args:
        nearby_locations : output of discover_nearby_locations() — reads "Hertz" sub-list.
    Returns:
        {location_key: cheapest_price_float}
    """
    hertz_locs = nearby_locations.get("Hertz", [])
    if not hertz_locs:
        print("  [NearbyHertz] No Hertz nearby locations to fetch.")
        return {}
    if not BRIGHT_DATA_CDP_URL:
        print("  [NearbyHertz] No Bright Data — skipping nearby Hertz prices.")
        return {}

    tasks = [
        _fetch_one_hertz_station(
            playwright,
            loc["station_code"],
            loc["location_key"],
            loc.get("name", ""),
        )
        for loc in hertz_locs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    prices: Dict[str, float] = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        key, price = r
        if price is not None:
            if key not in prices or price < prices[key]:
                prices[key] = price

    return prices


async def fetch_nearby_ehi_prices(
    playwright,
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch Enterprise Full Size SUV prices at the closest nearby locations
    (airports and city branches) using one shared Bright Data browser session.

    Opens enterprise.com once to establish the Incapsula fingerprint, then makes
    one /reservations/initiate POST per nearby location — reusing the same page
    and its cookies/headers.

    Args:
        nearby_locations : output of discover_nearby_locations() — reads "EHI" sub-list.
    Returns:
        {location_key: cheapest_enterprise_price_float}
    """
    ehi_locs = nearby_locations.get("EHI", [])
    if not ehi_locs:
        print("  [NearbyEHI] No EHI nearby locations to fetch.")
        return {}
    if not BRIGHT_DATA_CDP_URL:
        print("  [NearbyEHI] No Bright Data — skipping nearby EHI prices.")
        return {}

    prices: Dict[str, float] = {}
    browser = None
    ctx = None
    try:
        browser = await get_browser(playwright)
        page, ctx = await _new_bd_page(browser)

        print("  [NearbyEHI] Establishing session via enterprise.com/en/home.html...")
        home_url = "https://www.enterprise.com/en/home.html"
        await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_selector("input", timeout=15_000)
        except Exception:
            await page.wait_for_timeout(5_000)
        print(f"  [NearbyEHI] Session ready at {page.url[:60]}")

        api_url = f"{EH_API_BASE}/reservations/initiate"

        for loc in ehi_locs:
            lkey    = loc["location_key"]
            gbid    = loc["group_branch_id"]
            loc_id  = loc["location_id"]
            airport_code = loc["airport_code"]

            # Look up the full location config from the DB (we need gps, time_zone_id etc.)
            # Match by group_branch_id alone — works for both airport and city branches.
            db_entry = next(
                (e for e in (_LOCATIONS_DB_CACHE or [])
                 if e.get("provider") == "EHI"
                 and e.get("group_branch_id") == gbid),
                None,
            )
            if not db_entry:
                print(f"  [NearbyEHI/{lkey}] DB entry not found for {gbid} — skipping")
                continue

            loc_obj = {
                "airport_code":    airport_code,
                "location_type":   "BRANCH",
                "my_location":     False,
                "gps":             db_entry.get("gps") or {
                    "latitude":  db_entry.get("lat", 0),
                    "longitude": db_entry.get("lng", 0),
                },
                "name":            db_entry.get("name", lkey),
                "country_code":    db_entry.get("country_code", "US"),
                "group_branch_id": gbid,
                "type":            "BRANCH",
                "id":              loc_id,
                "time_zone_id":    db_entry.get("time_zone_id", "America/New_York"),
            }
            body = {
                "pickup_location_id":                loc_id,
                "return_location":                   loc_obj,
                "renter_age":                        BOOKING["driver_age"],
                "pickup_time":                       f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}",
                "return_location_id":                loc_id,
                "pickup_location":                   loc_obj,
                "renter_age_label":                  f"{BOOKING['driver_age']}+",
                "return_time":                       f"{BOOKING['return_date']}T{BOOKING['return_time']}",
                "applied_vehicle_class_filters":     [],
                "country_of_residence_code":         "US",
                "enable_north_american_prepay_rates": False,
                "view_currency_code":                "USD",
                "check_if_no_vehicles_available":    True,
                "check_if_oneway_allowed":           True,
            }
            body_json = json.dumps(body)

            print(f"  [NearbyEHI/{lkey}] POST /reservations/initiate  branch={gbid}")
            try:
                js = f"""async () => {{
                    const body = {body_json};
                    const r = await fetch('{api_url}', {{
                        method: 'POST',
                        headers: {{
                            'content-type': 'application/json',
                            'accept': 'application/json, text/plain, */*',
                            'brand': 'ENTERPRISE',
                            'channel': 'WEB',
                            'locale': 'en_US',
                            'page_type': 'home',
                            'sofresh': 'SOCLEAN',
                        }},
                        credentials: 'include',
                        body: JSON.stringify(body),
                    }});
                    const d = await r.json();
                    const classes = d?.session?.gbo?.reservation?.car_classes
                                 || d?.session?.analytics?.gbo?.reservation?.car_classes
                                 || [];
                    const EHI_CODE_NAMES = {{{_EHI_CODE_NAMES_JS}}};
                    return classes.map(c => ({{
                        code:   c.code,
                        name:   c.name || EHI_CODE_NAMES[c.code] || '',
                        status: c.status || '',
                        total:  c?.charges?.{EHI_CHARGE_KEY}?.total_price_view?.amount,
                    }}));
                }}"""
                car_classes = await page.evaluate(js)
                print(f"  [NearbyEHI/{lkey}] {len(car_classes)} classes returned")
                best_price, best_name = _ehi_extract_best(car_classes, f"Enterprise@{lkey}")
                if best_price:
                    prices[lkey] = best_price
                    print(f"  [NearbyEHI/{lkey}] Best: {best_name} @ ${best_price:.2f}")
                else:
                    codes = [c.get("code") for c in car_classes]
                    print(f"  [NearbyEHI/{lkey}] No Full Size SUV (codes: {codes[:8]})")
            except Exception as exc:
                print(f"  [NearbyEHI/{lkey}] Error: {exc!s:.120}")

    except Exception as exc:
        print(f"  [NearbyEHI] Session setup failed: {exc!s:.100}")
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass

    return prices


async def fetch_nearby_sixt_prices(
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch the cheapest Full Size SUV price at each nearby SIXT branch via the
    pure gRPC-JSON API (no browser required).

    City branches in the DB typically have empty branch_id and are skipped with
    a diagnostic message.  Airport branches with a non-empty branch_id are priced.

    Args:
        nearby_locations : output of discover_nearby_locations() — reads "SIXT" sub-list.
    Returns:
        {location_key: cheapest_fss_price_float}
    """
    sixt_locs = nearby_locations.get("SIXT", [])
    if not sixt_locs:
        print("  [NearbySIXT] No SIXT nearby locations to fetch.")
        return {}

    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}"
    return_dt  = f"{BOOKING['return_date']}T{BOOKING['return_time']}"

    # Skip entries with no branch_id (can't call API without it)
    seen_keys: set = set()
    priceable: List[Dict] = []
    for loc in sixt_locs:
        lkey      = loc.get("location_key", "")
        branch_id = loc.get("branch_id", "")
        if not branch_id:
            label = loc.get("name") or lkey
            print(f"  [NearbySIXT/{label}] Skipping — no branch_id in DB (city branch)")
            continue
        if lkey not in seen_keys:
            seen_keys.add(lkey)
            priceable.append(loc)

    prices: Dict[str, float] = {}
    for loc in priceable:
        lkey      = loc["location_key"]
        branch_id = loc["branch_id"]
        label     = loc.get("name") or lkey
        print(f"  [NearbySIXT/{label}] Fetching via API (branch={branch_id})...")
        try:
            offers = await asyncio.to_thread(_sixt_get_offers, branch_id, pickup_dt, return_dt)
            if offers is None:
                print(f"  [NearbySIXT/{label}] API call failed — skipping")
                continue
            best = _sixt_best_fullsize_suv(offers)
            if best:
                total  = best["_total"]
                title  = best.get("car_info", {}).get("title", "")
                acriss = best.get("offer_acriss_code", "")
                prices[lkey] = total
                print(f"  [NearbySIXT/{label}] Best FSS: {acriss} {title}  ${total:.2f}")
            else:
                print(f"  [NearbySIXT/{label}] No Full Size SUV in {len(offers)} offers")
        except Exception as exc:
            print(f"  [NearbySIXT/{label}] Error: {exc!s:.100}")

    return prices


# ─────────────────────────────────────────────────────────────────────────────
# SHARED FORM HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _try_fill_input(page, selectors: List[str], value: str) -> bool:
    """
    Attempt to fill a text input using a prioritised list of CSS selectors.
    Returns True on first success, False if none matched.
    """
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=5000)
            await el.click()
            await el.fill(value)
            return True
        except Exception:
            continue
    return False


async def _try_click(page, selectors: List[str]) -> bool:
    """
    Click the first element that matches any selector in the list.
    Returns True on first success.
    """
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=5000)
            await el.click()
            return True
        except Exception:
            continue
    return False


def _ordinal_day(n: int) -> str:
    """Return day number with English ordinal suffix: 1 → '1st', 2 → '2nd', etc."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


async def _pick_abg_calendar_date(page, date_str: str) -> None:
    """
    Click the correct day in the Avis/Budget inline calendar.
    Day buttons have aria-label like "Saturday, May 2nd, 2026".
    Navigation uses "Go to the Next Month" button.
    """
    from datetime import datetime as _dt
    target = _dt.strptime(date_str, "%Y-%m-%d")
    month_name = target.strftime("%B")   # "May"
    year       = str(target.year)        # "2026"
    ordinal    = _ordinal_day(target.day)
    weekday    = target.strftime("%A")   # "Saturday"
    # Build multiple formats in case the site varies slightly
    candidates = [
        f"{weekday}, {month_name} {ordinal}, {year}",       # "Saturday, May 2nd, 2026"
        f"{weekday}, {month_name} {target.day}, {year}",    # "Saturday, May 2, 2026" (no ordinal)
        f"{month_name} {target.day}",                        # "May 2"
        f"{month_name} {ordinal}, {year}",                   # "May 2nd, 2026"
    ]

    # Navigate months until correct month/year shown (max 14 clicks forward)
    for _ in range(14):
        # Try clicking the target day — it may already be visible
        for lbl in candidates:
            try:
                btn = page.locator(f"button[aria-label='{lbl}']").first
                if await btn.is_visible(timeout=800):
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(400)
                    return
            except Exception:
                pass
        # Not found yet — click Next Month
        try:
            next_btn = page.locator("button[aria-label='Go to the Next Month']").first
            await next_btn.click(timeout=5000)
            await page.wait_for_timeout(500)
        except Exception:
            break


async def _fill_abg_form(page) -> None:
    """
    Fill the Avis/Budget (ABG Holdings) MUI-based search form.
    Confirmed selectors from live DOM inspection:
      Location : #_r_c_   (MuiFilledInput, placeholder "Enter pick-up location or zip code")
      Pickup dt: #_r_e_   (MuiFilledInput — clicking opens inline calendar)
      Return dt: #_r_g_   (same)
      Submit   : button[aria-label="Show cars"]  (NOT "Show Vehicles")
    Calendar days: button[aria-label="Saturday, May 2nd, 2026"] format.
    """
    await page.wait_for_timeout(2000)

    # Accept cookie banner if present (Avis/Budget use "Agree" button)
    for cookie_sel in ["button:has-text('Agree')", "button#onetrust-accept-btn-handler",
                        "button:has-text('Accept All')", "button:has-text('Accept Cookies')"]:
        try:
            btn = page.locator(cookie_sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=3000)
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass

    # — Location — use JS to click + type to bypass Playwright actionability/cookie checks —
    # Native React setter + InputEvent is needed to trigger MUI Autocomplete fetch
    _airport = BOOKING["airport_code"]
    await page.evaluate(f"""
        const loc = document.getElementById('_r_c_');
        if (loc) {{
            loc.focus();
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(loc, '{_airport}');
            loc.dispatchEvent(new InputEvent('input', {{data: 'A', inputType: 'insertText', bubbles: true}}));
        }}
    """)
    await page.wait_for_timeout(2000)
    # Pick first autocomplete option (La Guardia Airport appears as second option)
    clicked = await _try_click(page, [
        "[role='option']:nth-child(2)",   # skip "Use current location"
        "[role='option']:first-child",
        "[class*='MuiAutocomplete-option']:first-child",
    ])
    if not clicked:
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(800)

    # — Pickup date — use JS click to open calendar —
    await page.evaluate("const el = document.getElementById('_r_e_'); if(el) el.click();")
    await page.wait_for_timeout(800)
    await _pick_abg_calendar_date(page, BOOKING["pickup_date"])

    # — Return date —
    await page.evaluate("const el = document.getElementById('_r_g_'); if(el) el.click();")
    await page.wait_for_timeout(800)
    await _pick_abg_calendar_date(page, BOOKING["return_date"])
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)

    # — Submit: confirmed button text is "Show cars" —
    await page.evaluate("""
        const btns = Array.from(document.querySelectorAll('button'));
        const show = btns.find(b => (b.innerText||'').toLowerCase().includes('show cars') || (b.getAttribute('aria-label')||'').toLowerCase().includes('show cars'));
        if (show) show.click();
    """)


async def _pick_calendar_date(page, date_str: str, toggle_id: str, day_label_fmt: str = "") -> None:
    """
    Click the National/Enterprise/Alamo date toggle, navigate to the correct month,
    then click the target day.

    Confirmed from live DOM inspection of nationalcar.com:
      - Calendar container: #dateContainerId
      - Caption (month/year): <caption> inside #dateContainerId
      - Day buttons: button.date-selector__day  with aria-label="May 2" (Month Day, no year)
      - Next month button: button.date-selector__control-btn with text "Next"
    """
    from datetime import datetime as _dt
    target = _dt.strptime(date_str, "%Y-%m-%d")
    month_name = target.strftime("%B")   # "May"
    day_num    = str(target.day)         # "2" (no leading zero)
    year       = str(target.year)        # "2026"
    # National uses "Month Day" format (e.g. "May 2") with no year
    target_label = f"{month_name} {day_num}"

    # Calendar is already open (caller used JS click); just wait for it to render
    await page.wait_for_timeout(500)

    # National shows 2 months at once. Try clicking the target day directly first.
    # If not visible, navigate forward/back.
    for _ in range(14):
        # Try all aria-label patterns for the target day
        for lbl in [
            target_label,                         # "May 2" ← National confirmed
            f"{month_name} {day_num}, {year}",    # "May 2, 2026"
            f"{day_num} {month_name} {year}",     # "2 May 2026"
        ]:
            try:
                el = page.locator(f"button.date-selector__day[aria-label='{lbl}']").first
                if await el.is_visible(timeout=800):
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(400)
                    return
            except Exception:
                pass

        # Day not visible — check current header to decide direction
        try:
            all_headers = await page.locator("#dateContainerId caption").all_inner_texts()
            header_text = " ".join(all_headers)
        except Exception:
            header_text = ""

        # If target month is in header text, try a broader click (force)
        if month_name in header_text and year in header_text:
            for lbl in [target_label, f"{month_name} {day_num}, {year}"]:
                try:
                    el = page.locator(f"[aria-label='{lbl}']").first
                    await el.click(force=True, timeout=3000)
                    await page.wait_for_timeout(400)
                    return
                except Exception:
                    pass
            break  # Month visible but day not clickable — stop

        # Navigate: click Last control-btn (Next month)
        try:
            next_btns = await page.locator("button.date-selector__control-btn").all()
            if next_btns:
                await next_btns[-1].click(timeout=5000)
                await page.wait_for_timeout(400)
        except Exception:
            break


async def _fill_enterprise_group_form(page, brand: str = "National") -> None:
    """
    Fill the National / Enterprise / Alamo search form.

    Confirmed selectors from live DOM inspection (2026-04-15):
      National/Alamo  location : #search-autocomplete__input-PICKUP
      Enterprise      location : #pickupLocationTextBox  (name='location-search')
      Pickup date: #date-time__pickup-toggle  (button[aria-label="Pick Up Date"] → calendar)
      Return date: #date-time__return-toggle  (button[aria-label="Return Date"] → calendar)
      Submit (National/Alamo): button.booking-widget__go-cta  ("CHECK AVAILABILITY")
      Submit (Enterprise):     button[type='submit'], button:has-text('Select My Car')
    Calendar: #dateContainerId, days are button.date-selector__day[aria-label="May 2"].
    Next month: last button.date-selector__control-btn.
    """
    # Select the correct location input ID based on brand
    _is_enterprise = brand.lower() == "enterprise"
    _loc_input_id = "pickupLocationTextBox" if _is_enterprise else "search-autocomplete__input-PICKUP"
    await page.wait_for_timeout(3000)

    # Dismiss any cookie / sign-in modal that may block interaction
    for dismiss_sel in [
        "button#onetrust-accept-btn-handler",
        "button[aria-label='Close the modal']",
        "button:has-text('Continue As Guest')",
        "button:has-text('CLOSE')",
        "button:has-text('Accept All')",
    ]:
        try:
            btn = page.locator(dismiss_sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(500)
        except Exception:
            pass

    async def _dismiss_eh_modals():
        """
        Aggressively dismiss Enterprise Holdings sign-in / cookie modals.
        Uses both Playwright clicks and JS DOM surgery to remove blocking overlays.
        """
        # Click-based dismissal
        selectors = [
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Close the modal']",
            "button:has-text('Continue As Guest')",
            "button:has-text('CLOSE')",
            "button:has-text('Accept All')",
            "button:has-text('No Thanks')",
            "button:has-text('Skip')",
            "[class*='modal'] button[aria-label*='lose']",
            "[class*='dialog'] button[aria-label*='lose']",
            "button.close", "a.close",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(400)
            except Exception:
                pass

        # Nuclear JS: remove ALL modal/overlay/curtain elements and restore scrolling
        try:
            await page.evaluate("""
                // Remove overlays, backdrops, curtains
                const removeSelectors = [
                    '[class*="modal-backdrop"]', '[class*="overlay"]',
                    '[class*="curtain"]', '[class*="login-modal"]',
                    '[class*="signin-modal"]', '[class*="sign-in"]',
                    '[id*="modal"]', '[class*="modal"]:not(button):not(a)',
                    '[role="dialog"]',
                ];
                removeSelectors.forEach(sel => {
                    try { document.querySelectorAll(sel).forEach(el => {
                        // Only remove if it's blocking (has backdrop/overlay styling)
                        const style = window.getComputedStyle(el);
                        if (style.position === 'fixed' || style.position === 'absolute') {
                            el.remove();
                        }
                    }); } catch(e) {}
                });
                // Restore body scroll
                document.body.style.overflow = '';
                document.body.style.paddingRight = '';
                document.body.classList.remove('modal-open', 'no-scroll', 'overflow-hidden');
                // Remove any ::before/::after backdrop pseudo-elements via class
                document.documentElement.classList.remove('modal-open');
            """)
        except Exception:
            pass

    await _dismiss_eh_modals()

    # — Location — wait for the autocomplete input to be visible, then type —
    loc_input = page.locator(f"#{_loc_input_id}")
    try:
        await loc_input.wait_for(state="visible", timeout=15_000)
    except Exception:
        print("  [form] Location input still not visible after modal dismissal — trying JS remove of overlays")
        await page.evaluate("""
            document.querySelectorAll('div[role="dialog"], [class*="modal"], [class*="overlay"]')
                .forEach(el => { el.style.display='none'; el.remove(); });
            document.body.style.overflow = '';
        """)
        await page.wait_for_timeout(1000)

    # Type location and immediately dump suggestion structure (single clean attempt)
    print("  [form] Clicking location input...")
    await loc_input.click(force=True, timeout=10_000)
    await page.wait_for_timeout(500)
    await page.keyboard.type(BOOKING["airport_code"], delay=150)
    # Wait up to 5s for suggestions to appear via API response
    await page.wait_for_timeout(3000)

    # Dump suggestion DOM structure to understand what elements are available
    sugg_info = await page.evaluate("""
        (locInputId) => {
            var res = {inputValue: '', found: []};
            var input = document.getElementById(locInputId);
            if (input) res.inputValue = input.value;
            var selectors = [
                'button.search-autocomplete__result--featured',
                '.search-autocomplete__results button',
                '.search-autocomplete__result',
                '[role="option"]', '[role="listbox"] > *',
                '[class*="autocomplete"] li', '[class*="autocomplete"] button',
            ];
            for (var s of selectors) {
                var els = document.querySelectorAll(s);
                if (els.length > 0 && els[0].offsetHeight > 0) {
                    var el = els[0];
                    var pk = Object.keys(el).find(function(k){return k.startsWith('__reactProps');});
                    var handlers = pk ? Object.keys(el[pk]).filter(function(k){return k.startsWith('on');}).join(',') : 'none';
                    res.found.push({sel: s, tag: el.tagName, cls: el.className.substring(0,60), text: el.textContent.trim().substring(0,40), handlers: handlers});
                    break;
                }
            }
            return res;
        }
    """, _loc_input_id)
    print(f"  [form] Suggestion DOM: {sugg_info}")

    loc_val = ""
    suggestions_found = bool(sugg_info.get("found"))

    if suggestions_found:
        found = sugg_info["found"][0]
        sel = found["sel"]
        handlers = found.get("handlers", "")

        # Strategy A: React fiber — directly call the suggestion's React event handlers
        fiber_result = await page.evaluate(f"""
            (function() {{
                var el = document.querySelector('{sel}');
                if (!el) return 'element gone';
                var pk = Object.keys(el).find(function(k){{return k.startsWith('__reactProps');}});
                if (!pk) return 'no reactProps; keys=' + Object.keys(el).filter(function(k){{return k.startsWith('__');}}).join(',');
                var p = el[pk];
                var fakeEvt = {{preventDefault:function(){{}},stopPropagation:function(){{}},persist:function(){{}},target:el,currentTarget:el,nativeEvent:{{type:'mousedown',button:0,bubbles:true}}}};
                var called=[];
                if(typeof p.onMouseDown==='function'){{p.onMouseDown(fakeEvt);called.push('onMouseDown');}}
                fakeEvt.nativeEvent={{type:'mouseup',button:0}};
                if(typeof p.onMouseUp==='function'){{p.onMouseUp(fakeEvt);called.push('onMouseUp');}}
                fakeEvt.nativeEvent={{type:'click',button:0}};
                if(typeof p.onClick==='function'){{p.onClick(fakeEvt);called.push('onClick');}}
                return 'called='+called.join(',')+'|handlers='+Object.keys(p).filter(function(k){{return k.startsWith('on');}}).join(',');
            }})()
        """)
        print(f"  [form] Fiber result: {fiber_result}")
        await page.wait_for_timeout(2000)
        try:
            loc_val = await loc_input.input_value(timeout=2000)
            print(f"  [form] Location after fiber: '{loc_val}'")
        except Exception:
            pass

    if not loc_val and suggestions_found:
        # Strategy B: keyboard ArrowDown+Enter (input still focused after typing)
        await loc_input.click(force=True)
        await page.wait_for_timeout(200)
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(600)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)
        try:
            loc_val = await loc_input.input_value(timeout=2000)
            print(f"  [form] Location after ArrowDown+Enter: '{loc_val}'")
        except Exception:
            pass

    if not loc_val and suggestions_found:
        # Strategy C: Playwright native .click() on suggestion
        sel = sugg_info["found"][0]["sel"]
        try:
            await page.locator(sel).first.click(timeout=5000)
            await page.wait_for_timeout(2000)
            loc_val = await loc_input.input_value(timeout=2000)
            print(f"  [form] Location after Playwright click: '{loc_val}'")
        except Exception as e:
            print(f"  [form] Playwright click failed: {e}")

    if not loc_val:
        print(f"  [form] WARNING: Location selection failed (suggestions_found={suggestions_found})")

    # — Pickup date via calendar —
    print("  [form] Opening pickup date calendar...")
    await page.evaluate("const el = document.getElementById('date-time__pickup-toggle'); if(el) el.click();")
    await page.wait_for_timeout(500)
    await _pick_calendar_date(page, BOOKING["pickup_date"], "date-time__pickup-toggle")
    print(f"  [form] Pickup date set")

    # — Return date via calendar —
    print("  [form] Opening return date calendar...")
    await page.evaluate("const el = document.getElementById('date-time__return-toggle'); if(el) el.click();")
    await page.wait_for_timeout(500)
    await _pick_calendar_date(page, BOOKING["return_date"], "date-time__return-toggle")
    print(f"  [form] Return date set")

    # Final modal dismissal before submit
    await _dismiss_eh_modals()

    # — Submit —
    print("  [form] Checking submit button state...")
    # National/Alamo use button.booking-widget__go-cta; Enterprise uses a different submit
    if _is_enterprise:
        # Enterprise booking widget submit patterns (priority order)
        _submit_sel = (
            "button.start-res-btn, button[data-ui-path*='search'], "
            "button:has-text('Select My Car'), button:has-text('Reserve'), "
            "button[type='submit'], form button[class*='cta']"
        )
    else:
        _submit_sel = "button.booking-widget__go-cta"
    submit_btn = page.locator(_submit_sel).first
    submit_visible = await submit_btn.is_visible(timeout=5000)
    submit_disabled = await page.evaluate(
        "(sel) => { var b=document.querySelector(sel); return b ? b.disabled : 'not found'; }",
        _submit_sel.split(",")[0].strip()  # use first selector for disabled check
    )
    print(f"  [form] Submit visible: {submit_visible}, disabled: {submit_disabled}")

    if submit_disabled:
        # Button is disabled because location branch not set in React state.
        # Try JS-native setter approach to force the location input value into React state.
        print("  [form] Submit disabled — trying native React setter to force location value...")
        await page.evaluate("""
            ([locInputId, airportCode]) => {
                var inp = document.getElementById(locInputId);
                if (!inp) return;
                var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(inp, airportCode);
                inp.dispatchEvent(new Event('input', {bubbles: true, cancelable: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
            }
        """, [_loc_input_id, BOOKING["airport_code"]])
        await page.wait_for_timeout(3000)
        # Try clicking first suggestion that appears
        for sel in ["button.search-autocomplete__result--featured",
                    ".search-autocomplete__results button:first-child"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click(timeout=3000)
                    print(f"  [form] Clicked suggestion via Playwright: {sel}")
                    break
            except Exception:
                pass
        await page.wait_for_timeout(2000)
        submit_disabled2 = await page.evaluate(
            "(sel) => { var b=document.querySelector(sel); return b ? b.disabled : 'not found'; }",
            _submit_sel.split(",")[0].strip()
        )
        print(f"  [form] Submit disabled after retry: {submit_disabled2}")

    # Intercept history navigation
    await page.evaluate("""
        (function() {
            window.__historyPushes = [];
            var op = window.history.pushState.bind(window.history);
            window.history.pushState = function(s,t,u) { window.__historyPushes.push('push:'+String(u||'')); return op(s,t,u); };
            var or_ = window.history.replaceState.bind(window.history);
            window.history.replaceState = function(s,t,u) { window.__historyPushes.push('replace:'+String(u||'')); return or_(s,t,u); };
            window.addEventListener('hashchange', function(e){ window.__historyPushes.push('hash:'+e.newURL.substring(0,80)); });
        })()
    """)
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text[:100]) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append("PAGEERR:" + str(e)[:100]))

    # Strategy A: React fiber onClick on the submit button (bypasses DOM click events)
    fiber_submit_result = await page.evaluate("""
        (submitSel) => {
            var btn = document.querySelector(submitSel);
            if (!btn) return 'no submit button';
            var pk = Object.keys(btn).find(function(k){return k.startsWith('__reactProps');});
            if (!pk) return 'no reactProps on submit';
            var p = btn[pk];
            var handlers = Object.keys(p).filter(function(k){return k.startsWith('on');}).join(',');
            var fakeEvt = {preventDefault:function(){},stopPropagation:function(){},persist:function(){},target:btn,currentTarget:btn,nativeEvent:{type:'click',button:0}};
            var called = [];
            if (typeof p.onClick === 'function') { p.onClick(fakeEvt); called.push('onClick'); }
            if (typeof p.onMouseUp === 'function') { p.onMouseUp(fakeEvt); called.push('onMouseUp'); }
            return 'handlers='+handlers+'|called='+called.join(',');
        }
    """, _submit_sel.split(",")[0].strip())
    print(f"  [form] Submit fiber result: {fiber_submit_result}")
    await page.wait_for_timeout(2000)
    history_after_fiber = await page.evaluate("window.__historyPushes || []")
    print(f"  [form] history after fiber submit: {history_after_fiber}")
    print(f"  [form] URL after fiber submit: {page.url[:80]}")

    if "/home" in page.url:
        # Strategy B: DOM click
        await submit_btn.click(force=True, timeout=10000)
        print("  [form] Submit DOM-clicked — waiting 3s for reaction...")
        await page.wait_for_timeout(3000)
        print(f"  [form] URL 3s after submit: {page.url[:80]}")
        history_after_click = await page.evaluate("window.__historyPushes || []")
        print(f"  [form] history after DOM click: {history_after_click}")

    if console_errors:
        print(f"  [form] Console errors: {console_errors[:3]}")


async def _pick_daypicker_date_open(page, date_str: str) -> None:
    """
    Navigate and pick a date in an already-open Dollar/Thrifty DayPicker.
    Assumes the DayPicker popup is already visible (triggered externally).
    Confirmed aria-label format: "Sat May 02 2026" (%a %b %d %Y).
    Navigation: button[aria-label="Next Month"].
    """
    from datetime import datetime as _dt
    target = _dt.strptime(date_str, "%Y-%m-%d")
    month_name = target.strftime("%B")   # "May"
    year       = str(target.year)        # "2026"
    target_label = target.strftime("%a %b %d %Y")  # "Sat May 02 2026"

    # Navigate to correct month (max 12 attempts)
    for _ in range(12):
        try:
            header = await page.locator(".DayPicker-Caption").first.inner_text(timeout=3000)
        except Exception:
            break
        if month_name in header and year in header:
            break
        try:
            await page.locator("button[aria-label='Next Month']").first.click(timeout=5000)
            await page.wait_for_timeout(400)
        except Exception:
            break

    await page.locator(f".DayPicker-Day[aria-label='{target_label}']").click(timeout=5000)
    await page.wait_for_timeout(400)

    # Click Apply if it appears
    try:
        apply = page.get_by_role("button", name="Apply")
        if await apply.is_visible(timeout=2000):
            await apply.click(timeout=3000)
    except Exception:
        pass


async def _pick_daypicker_date(page, date_str: str, trigger_id: str) -> None:
    """
    Click a Dollar/Thrifty DayPicker calendar date.
    Confirmed DOM: .DayPicker-Day elements with aria-label="Wed Apr 01 2026" format.
    Navigate via [aria-label="Next Month"] button until the right month is shown.
    """
    from datetime import datetime as _dt
    target = _dt.strptime(date_str, "%Y-%m-%d")
    month_name = target.strftime("%B")   # "May"
    year       = str(target.year)        # "2026"

    # Open the calendar — input is readOnly so use force=True to bypass actionability checks
    await page.locator(f"#{trigger_id}").click(timeout=10000, force=True)
    await page.wait_for_timeout(800)

    # Navigate to correct month (max 12 attempts)
    for _ in range(12):
        try:
            header = await page.locator(".DayPicker-Caption").first.inner_text(timeout=3000)
        except Exception:
            break
        if month_name in header and year in header:
            break
        await page.locator("[aria-label='Next Month']").first.click(timeout=5000)
        await page.wait_for_timeout(400)

    # aria-label format on Dollar/Thrifty: "Wed Apr 01 2026"  (3-letter weekday, 3-letter month, 2-digit day)
    target_label = target.strftime("%a %b %d %Y")  # "Sat May 02 2026"
    await page.locator(f".DayPicker-Day[aria-label='{target_label}']").click(timeout=5000)
    await page.wait_for_timeout(400)

    # Click Apply if it appears
    try:
        apply = page.get_by_role("button", name="Apply")
        if await apply.is_visible(timeout=2000):
            await apply.click(timeout=3000)
    except Exception:
        pass


async def _pick_react_datepicker_date(page, date_str: str) -> None:
    """
    Pick a date in an already-open react-datepicker calendar (Alamo).
    Day aria-label format: "Choose Saturday, May 2nd, 2026".
    Navigation: .react-datepicker__navigation--next (aria-label="Next Month").
    Header: .react-datepicker__current-month  e.g. "May 2026".
    """
    from datetime import datetime as _dt
    target = _dt.strptime(date_str, "%Y-%m-%d")
    month_name = target.strftime("%B")   # "May"
    year       = str(target.year)        # "2026"
    ordinal    = _ordinal_day(target.day)
    weekday    = target.strftime("%A")   # "Saturday"
    target_label = f"Choose {weekday}, {month_name} {ordinal}, {year}"

    # Navigate to correct month
    for _ in range(14):
        try:
            header = await page.locator(".react-datepicker__current-month").first.inner_text(timeout=3000)
        except Exception:
            break
        if month_name in header and year in header:
            break
        try:
            await page.locator(".react-datepicker__navigation--next").first.click(timeout=5000)
            await page.wait_for_timeout(400)
        except Exception:
            break

    # Click the day
    await page.locator(f".react-datepicker__day[aria-label='{target_label}']").click(timeout=5000)
    await page.wait_for_timeout(400)


async def _fill_alamo_form(page) -> None:
    """
    Fill the Alamo search form.
    Confirmed from live DOM inspection:
      Location   : #pickupLocation  (text input, aria-label="Location...")
      Pickup date: #pickupDate  (button → react-datepicker)
      Return date: #returnDate  (button → react-datepicker)
      Submit     : button[aria-label="Search"]
    react-datepicker day: aria-label="Choose Saturday, May 2nd, 2026"
    Nav: .react-datepicker__navigation--next (aria-label="Next Month")
    """
    await page.wait_for_timeout(3000)

    # Accept cookie banner if present
    for cookie_sel in ["button#onetrust-accept-btn-handler", "button:has-text('Accept All')",
                        "button:has-text('CLOSE')", "button:has-text('Accept')"]:
        try:
            btn = page.locator(cookie_sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=3000)
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass

    # — Location — force click + keyboard type to trigger autocomplete —
    print("  [Alamo form] Typing location...")
    loc = page.locator("#pickupLocation")
    # Check if the element exists first
    loc_exists = await loc.count() > 0
    print(f"  [Alamo form] #pickupLocation found: {loc_exists}")
    if not loc_exists:
        # Try alternate selectors
        for alt in ["input[aria-label*='ocation']", "input[placeholder*='ocation']", "input[name*='ocation']"]:
            if await page.locator(alt).count() > 0:
                loc = page.locator(alt).first
                print(f"  [Alamo form] Using alternate selector: {alt}")
                break

    await loc.click(force=True, timeout=TIMEOUT_MS)
    await page.keyboard.type(BOOKING["airport_code"], delay=120)
    await page.wait_for_timeout(2000)

    # Use keyboard selection (more reliable for React autocompletes)
    for sel in ["[class*='locationSuggestions'] li:first-child",
                "[class*='suggestions'] li:first-child",
                "[role='option']:first-child",
                "[class*='suggestion']:first-child"]:
        try:
            if await page.locator(sel).first.is_visible(timeout=1000):
                print(f"  [Alamo form] Suggestion visible ({sel})")
                break
        except Exception:
            pass

    # Use JS dispatchEvent to select suggestion (same fix as National/Enterprise)
    clicked_via_js = False
    for sel in ["[class*='locationSuggestions'] li:first-child",
                "[class*='suggestions'] li:first-child",
                "[role='option']:first-child",
                "[class*='suggestion']:first-child"]:
        try:
            if await page.locator(sel).first.is_visible(timeout=1000):
                clicked_via_js = await page.evaluate(f"""
                    (() => {{
                        const el = document.querySelector('{sel}');
                        if (!el) return false;
                        el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                        el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                        el.click();
                        return true;
                    }})()
                """)
                break
        except Exception:
            pass

    if not clicked_via_js:
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(300)
        await page.keyboard.press("Tab")
    await page.wait_for_timeout(1500)

    try:
        loc_val = await loc.input_value(timeout=1000)
        print(f"  [Alamo form] Location field after selection: '{loc_val}'")
    except Exception:
        pass

    # — Pickup date — click button to open react-datepicker —
    print("  [Alamo form] Opening pickup date...")
    pickup_btn = await page.evaluate("(function() { return document.getElementById('pickupDate') !== null; })()")
    print(f"  [Alamo form] #pickupDate found: {pickup_btn}")
    await page.evaluate("(function() { var el = document.getElementById('pickupDate'); if(el) el.click(); })()")
    await page.wait_for_timeout(800)
    cal_visible = await page.locator(".react-datepicker").first.is_visible(timeout=2000) if await page.locator(".react-datepicker").count() > 0 else False
    print(f"  [Alamo form] react-datepicker visible: {cal_visible}")
    await _pick_react_datepicker_date(page, BOOKING["pickup_date"])
    print("  [Alamo form] Pickup date set")

    # — Return date —
    print("  [Alamo form] Opening return date...")
    await page.evaluate("(function() { var el = document.getElementById('returnDate'); if(el) el.click(); })()")
    await page.wait_for_timeout(800)
    await _pick_react_datepicker_date(page, BOOKING["return_date"])
    print("  [Alamo form] Return date set")

    # Dump form state to diagnose why submit might not navigate
    try:
        form_state = await page.evaluate("""
            (function() {
                var loc = document.getElementById('pickupLocation');
                var pu = document.getElementById('pickupDate');
                var re = document.getElementById('returnDate');
                var btn = document.querySelector('button[aria-label="Search"]');
                return {
                    location: loc ? loc.value : 'missing',
                    pickupDate: pu ? (pu.value || pu.textContent || pu.innerText || '').trim().substring(0,30) : 'missing',
                    returnDate: re ? (re.value || re.textContent || re.innerText || '').trim().substring(0,30) : 'missing',
                    submitDisabled: btn ? btn.disabled : 'missing',
                    submitText: btn ? btn.textContent.trim().substring(0,20) : 'missing',
                };
            })()
        """)
        print(f"  [Alamo form] State: {form_state}")
    except Exception as e:
        print(f"  [Alamo form] State check failed: {e}")

    # — Submit — intercept network requests to see what fires —
    submit_found = await page.evaluate("(function() { var btn = document.querySelector('button[aria-label=\"Search\"]'); return btn !== null; })()")
    submit_disabled = await page.evaluate("(function() { var btn = document.querySelector('button[aria-label=\"Search\"]'); return btn ? btn.disabled : 'not found'; })()")
    print(f"  [Alamo form] Submit button found: {submit_found}, disabled: {submit_disabled}")

    # Intercept history.pushState to see if React Router navigation is attempted
    await page.evaluate("""
        (function() {
            window.__historyPushes = [];
            var orig = window.history.pushState.bind(window.history);
            window.history.pushState = function(state, title, url) {
                window.__historyPushes.push(String(url || ''));
                return orig(state, title, url);
            };
            var origReplace = window.history.replaceState.bind(window.history);
            window.history.replaceState = function(state, title, url) {
                window.__historyPushes.push('replace:' + String(url || ''));
                return origReplace(state, title, url);
            };
        })()
    """)

    # Capture requests that fire after submit
    captured_urls = []
    def _capture(req):
        url = req.url
        if any(kw in url.lower() for kw in ["reservat", "vehicle", "avail", "search", "booking", "/api/"]):
            captured_urls.append(f"{req.method} {url[:120]}")
    page.on("request", _capture)

    # Use Playwright locator.click() — triggers React's synthetic events properly
    try:
        await page.locator('button[aria-label="Search"]').first.click(timeout=8000)
        print("  [Alamo form] Submit clicked via Playwright")
    except Exception:
        # Fallback to JS click
        await page.evaluate("(function() { var btn = document.querySelector('button[aria-label=\"Search\"]'); if (btn) btn.click(); })()")
        print("  [Alamo form] Submit clicked via JS fallback")

    await page.wait_for_timeout(3000)
    print(f"  [Alamo form] URL 3s after submit: {page.url[:80]}")
    try:
        history_pushes = await page.evaluate("window.__historyPushes || []")
        print(f"  [Alamo form] history.push calls: {history_pushes}")
    except Exception:
        pass
    if captured_urls:
        print(f"  [Alamo form] API requests fired: {captured_urls[:5]}")
    else:
        print("  [Alamo form] No matching API requests captured")


async def _fill_dollar_thrifty_form(page) -> None:
    """
    Fill the Dollar / Thrifty MUI-based search form.
    Confirmed selectors from live DOM inspection:
      Location   : #locationInput  (MuiAutocomplete)
      Pickup date: #dateTimePickerTriggerFrom  (readOnly input → click opens DayPicker)
      Return date: #dateTimePickerTriggerTo    (readOnly input → click opens DayPicker)
      Submit     : #submitButton  ("View Vehicles")
    The date inputs are readOnly so we use JS click + force=True.
    """
    await page.wait_for_timeout(2000)

    # — Location (MUI Autocomplete) — force click + keyboard type to trigger React state —
    loc = page.locator("#locationInput")
    await loc.click(force=True, timeout=TIMEOUT_MS)
    await page.keyboard.type(BOOKING["airport_code"], delay=80)
    await page.wait_for_timeout(2000)
    # MUI Autocomplete listbox
    suggestion_clicked = await _try_click(page, [
        "[class*='MuiAutocomplete-listbox'] li:first-child",
        "[role='listbox'] li:first-child",
        "[role='option']:first-child",
    ])
    if not suggestion_clicked:
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)

    # — Pickup date via DayPicker —
    # Use JS click to open — the input is readOnly and Playwright blocks fill() on readOnly fields
    await page.evaluate("document.getElementById('dateTimePickerTriggerFrom').click()")
    await page.wait_for_timeout(800)
    await _pick_daypicker_date_open(page, BOOKING["pickup_date"])

    # — Return date via DayPicker —
    await page.evaluate("document.getElementById('dateTimePickerTriggerTo').click()")
    await page.wait_for_timeout(800)
    await _pick_daypicker_date_open(page, BOOKING["return_date"])

    # — Submit —
    await page.locator("#submitButton").click(timeout=10000)


# ─────────────────────────────────────────────────────────────────────────────
# GENERIC RESULT EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_cheapest_suv(page, provider: str) -> Dict:
    """
    Generic vehicle card scraper used by all browser-based providers.
    Iterates over vehicle cards on the results page, filters for Full Size SUV,
    and returns the cheapest option found.

    Falls back to full-page text parsing if no structured cards are found.
    """
    # Ordered list of card selectors — more specific first
    card_selectors = [
        "article",                  # Avis, Budget (each vehicle is an <article> element)
        ".MuiCard-root",            # Hertz, Dollar, Thrifty (Material-UI)
        "[class*='vehicle-card']",
        "[class*='VehicleCard']",
        "[class*='vehicleCard']",
        "[class*='vehicle-tile']",
        "[class*='vehicle-item']",
        "[class*='car-card']",
        "[class*='car-tile']",
        "[class*='car-result']",
        "[class*='offer-card']",
        "[class*='result-item']",
        "li[class*='vehicle']",
        "article[class*='vehicle']",
        "div[class*='car']",
    ]

    cards = []
    used_sel = ""
    for sel in card_selectors:
        found = await page.query_selector_all(sel)
        if found:
            cards = found
            used_sel = sel
            break

    if not cards:
        # Last resort — parse full page body text
        body = await page.inner_text("body")
        return _parse_suv_from_body(body, provider, page.url)

    if DEBUG_CARDS:
        print(f"  [{provider}] Found {len(cards)} cards via '{used_sel}'")

    best_price: Optional[float] = None
    best_model = ""
    best_class = ""

    # Words that indicate a card is a location / summary widget, not a vehicle listing.
    # If the first line of a card matches one of these, skip it.
    # NOTE: "pickup" is intentionally excluded — "Full-Size Pickup" is a vehicle category.
    # NOTE: "return" is excluded too — too broad (e.g. "Non-refundable" on vehicle cards).
    LOCATION_KEYWORDS = {
        "airport", "terminal",
        "drop-off", "dropoff",
        "reserve", "reservation", "booking",
        "forgot username", "enroll now", "already a member",  # National/Enterprise login modal
        "need a rental car",  # Dollar/Thrifty homepage search form
    }

    for card in cards:
        try:
            text = (await card.inner_text()).strip()
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if not lines:
                continue

            # Skip cards whose first line is a location / summary header
            first_lower = lines[0].lower()
            if any(kw in first_lower for kw in LOCATION_KEYWORDS):
                if DEBUG_CARDS:
                    print(f"  [{provider}]  skip (location card): {lines[0][:60]}")
                continue

            # Skip cards that don't mention any Full Size SUV keyword
            if not any(is_fullsize_suv(ln) for ln in lines):
                if DEBUG_CARDS and len(lines) > 0:
                    print(f"  [{provider}]  skip (no SUV match): {lines[0][:60]}")
                continue

            # Collect all valid prices in this card, take the LARGEST (= est. total, not daily).
            # Daily rates are always lower than multi-day totals, so max() gives the total.
            card_prices = []
            for line in lines:
                price = parse_price(line)
                if price and 100 < price < 10_000:
                    card_prices.append(price)

            if card_prices:
                card_total = max(card_prices)
                if best_price is None or card_total < best_price:
                    best_price = card_total
                    # Use the SUV-matching line as model name (more informative than lines[0])
                    suv_line = next((ln for ln in lines if is_fullsize_suv(ln)), "")
                    best_class = suv_line or "Full Size SUV"
                    # Model: first line that isn't the class line itself; fall back to class
                    non_class = [ln for ln in lines if ln != suv_line and not is_fullsize_suv(ln)]
                    best_model = non_class[0] if non_class else suv_line
                    if DEBUG_CARDS:
                        print(f"  [{provider}]  match: class={best_class[:40]}  price=${card_total:.2f}")
            else:
                if DEBUG_CARDS:
                    print(f"  [{provider}]  SUV card but no valid price: {lines[:3]}")

        except Exception:
            continue

    if best_price is None:
        if DEBUG_CARDS:
            # Print the first 5 card texts to help diagnose the mismatch
            print(f"  [{provider}] No SUV matched. First 5 card snippets:")
            for i, card in enumerate(cards[:5]):
                try:
                    snippet = (await card.inner_text()).strip()[:120].replace("\n", " | ")
                    print(f"    card[{i}]: {snippet}")
                except Exception:
                    pass
        return make_result(provider, error="No Full Size SUV found on results page")

    return make_result(
        provider,
        car_class=best_class,
        model=best_model,
        price=best_price,
        url=page.url,
    )


def _parse_suv_from_body(body: str, provider: str, url: str) -> Dict:
    """
    Last-resort parser: scan full page body text for Full Size SUV price mentions.
    Used when structured card selectors match nothing.

    Looks for a FULLSIZE_SUV_KEYWORDS match, then scans the next 10 lines for a price.
    Skips matches that only contain sedan keywords (Full-Size without SUV).
    """
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]

    if DEBUG_CARDS:
        # Show first occurrence of any suv-ish lines for diagnosis
        suv_lines = [(i, ln) for i, ln in enumerate(lines) if "suv" in ln.lower() or "full" in ln.lower()][:8]
        if suv_lines:
            print(f"  [{provider}] Body SUV-ish lines:")
            for i, ln in suv_lines:
                print(f"    [{i}] {ln[:100]}")

    best_price: Optional[float] = None
    best_class = ""

    for i, line in enumerate(lines):
        if not is_fullsize_suv(line):
            continue
        # Search the next few lines for a price
        for j in range(i + 1, min(i + 12, len(lines))):
            price = parse_price(lines[j])
            if price and 100 < price < 5000:
                if best_price is None or price < best_price:
                    best_price = price
                    best_class = line
                break

    if best_price is None:
        return make_result(provider, error="No Full Size SUV found (text fallback)")

    return make_result(provider, car_class=best_class, price=best_price, url=url)


# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER_FUNCS = {
    "SIXT":       check_sixt,
    "Hertz":      check_hertz,
    "Avis":       check_avis,
    "Budget":     check_budget,
    "National":   check_national,
    "Enterprise": check_enterprise,
    "Alamo":      check_alamo,
    "Dollar":     check_dollar,
    "Thrifty":    check_thrifty,
    "Kayak":      check_kayak,
}


async def check_provider(playwright, provider: str) -> Dict:
    """
    Run a single provider check with a hard 300-second asyncio timeout.
    Returns a result dict (with error key set) if anything goes wrong.
    """
    func = PROVIDER_FUNCS.get(provider)
    if func is None:
        return make_result(provider, error="No implementation for this provider")

    try:
        return await asyncio.wait_for(func(playwright), timeout=300.0)
    except asyncio.TimeoutError:
        return make_result(provider, error="Timed out after 300s")
    except Exception as exc:
        return make_result(provider, error=str(exc)[:100])


# ─────────────────────────────────────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────────────────────────────────────

_CSV_FIELDS = ["timestamp", "provider", "car_class", "model", "price", "saving", "status"]


def log_result(result: Dict, saving: Optional[float]) -> None:
    """Append one result row to the CSV log, creating headers if the file is new."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "provider":  result["provider"],
            "car_class": result.get("car_class", ""),
            "model":     result.get("model", ""),
            "price":     f"{result['price']:.2f}" if result.get("price") else "",
            "saving":    f"{saving:.2f}" if saving is not None else "",
            "status":    ("ERROR: " + result["error"]) if result.get("error") else "OK",
        })


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

def print_results(results: List[Dict]) -> None:
    """Print the formatted comparison table with best-deal summary."""
    booked   = BOOKING["booked_price"]
    airport  = BOOKING["airport_code"]
    pu_date  = BOOKING["pickup_date"]
    re_date  = BOOKING["return_date"]
    sep      = "=" * 90
    col_sep  = "─" * 90

    # Derive rental duration in days
    try:
        from datetime import date as _date
        d1 = _date.fromisoformat(pu_date)
        d2 = _date.fromisoformat(re_date)
        days = (d2 - d1).days
    except Exception:
        days = "?"

    # Active filters summary (only confirmed-working params)
    filter_parts = []
    if BOOKING.get("free_cancellation"):
        filter_parts.append("free cancel")
    if BOOKING.get("min_passengers") and int(BOOKING["min_passengers"]) >= 5:
        filter_parts.append(f"≥{BOOKING['min_passengers']} seats")
    if BOOKING.get("unlimited_mileage"):
        filter_parts.append("unlimited miles")
    pay = (BOOKING.get("payment_type") or "").upper()
    if pay:
        pay_label = {"PREPAID": "prepaid", "PAY_LATER": "pay-later"}.get(pay, pay.lower())
        filter_parts.append(f"prefer {pay_label} [note: Kayak filter not applied]")
    filter_str = " | ".join(filter_parts) if filter_parts else "none"

    print(f"\n{sep}")
    print(f"PRICE MONITOR — {airport}  {pu_date} → {re_date}  ({days} days)")
    print(f"Your booking : {BOOKING['provider']} {BOOKING['car_class']}  ${booked:.2f}")
    print(f"Filters      : {filter_str}")
    print(sep)
    print(f"{'Provider':<14} {'Class':<20} {'Model':<24} {'Price':>8}   {'vs Booked':>10}   Source")
    print(col_sep)

    best_saving:   Optional[float] = None
    best_provider: Optional[str]   = None
    has_kayak      = False

    for r in results:
        provider = r["provider"]

        if r.get("error"):
            # Strip newlines — Playwright exceptions include a "Call log:" block
            err_short = r["error"].replace("\n", " ").replace("\r", "")[:55]
            status = "N/A" if r.get("na") else "ERROR"
            print(f"{provider:<14} {status:<20} {err_short}")
            continue

        price     = r.get("price")
        car_class = (r.get("car_class") or "Full Size SUV")[:18]
        model     = (r.get("model")     or "")[:22]
        via_kayak = "kayak.com" in (r.get("url") or "")
        source    = "Kayak*" if via_kayak else "direct"
        if via_kayak:
            has_kayak = True

        if price:
            price_str  = f"${price:>8.2f}"
            saving     = booked - price
            if abs(saving) < 0.01:
                saving_str = f"{'(booked)':>10}"
            elif saving >= MIN_SAVING:
                saving_str = f"{'✓ save $' + f'{saving:.2f}':>10}"
            elif saving > 0:
                saving_str = f"{'save $' + f'{saving:.2f}':>10}"
            else:
                saving_str = f"{'↑ +$' + f'{abs(saving):.2f}':>10}"

            if saving >= MIN_SAVING and (best_saving is None or saving > best_saving):
                best_saving   = saving
                best_provider = provider
        else:
            price_str  = f"{'N/A':>9}"
            saving_str = f"{'N/A':>10}"

        print(f"{provider:<14} {car_class:<20} {model:<24} {price_str}   {saving_str}   {source}")

        # Always show raw Kayak class label for Kayak-sourced results so the
        # user can verify the match quality (e.g. "Premium SUV" vs "Full-size SUV").
        raw_cls = r.get("kayak_class_raw", "")
        if via_kayak and raw_cls:
            print(f"{'':14} ↳ Kayak class: '{raw_cls}'")

    print(sep)
    if best_provider and best_saving is not None:
        winner_price = next(r["price"] for r in results if r["provider"] == best_provider)
        print(f"BEST DEAL ► {best_provider} — ${winner_price:.2f}   save ${best_saving:.2f} vs your booking")
    else:
        print("No provider found a cheaper Full Size SUV than your booked price.")
    if has_kayak:
        print("  * price sourced via Kayak aggregator (OTA — may differ from direct booking)")
    print(f"{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def _write_json_results(
    results: List[Dict],
    nearby_rows: List[Dict],
    total_seconds: float,
) -> None:
    """
    Write latest_results.json next to price_monitor.py.

    Structure:
        booking        — static booking config from BOOKING dict
        last_checked   — ISO-8601 timestamp of this run
        runtime_seconds
        results        — one entry per provider (main table)
        nearby         — nearby location rows from Phase 3
        summary        — best direct / best OTA headline numbers
    """
    booked = BOOKING["booked_price"]

    # ── booking section ──────────────────────────────────────────────────────
    booking_section = {
        "provider":          BOOKING.get("provider"),
        "car_class":         BOOKING.get("car_class"),
        "location":          BOOKING.get("location"),
        "airport_code":      BOOKING.get("airport_code"),
        "pickup_date":       BOOKING.get("pickup_date"),
        "pickup_time":       BOOKING.get("pickup_time"),
        "return_date":       BOOKING.get("return_date"),
        "return_time":       BOOKING.get("return_time"),
        "booked_price":      booked,
        "payment_type":      BOOKING.get("payment_type"),
        "free_cancellation": bool(BOOKING.get("free_cancellation")),
    }

    # ── results section ──────────────────────────────────────────────────────
    results_section = []
    for r in results:
        price = r.get("price")
        saving = round(booked - price, 2) if price is not None else None
        via_kayak = "kayak.com" in (r.get("url") or "")
        source = "Kayak" if via_kayak else "direct"
        if r.get("error"):
            status = "na" if r.get("na") else "error"
        else:
            status = "ok"
        results_section.append({
            "provider":    r.get("provider"),
            "car_class":   r.get("car_class") or "",
            "model":       r.get("model") or "",
            "price":       round(price, 2) if price is not None else None,
            "saving":      saving,
            "source":      source,
            "status":      status,
            "booking_url": r.get("url") or None,
        })

    # ── nearby section ───────────────────────────────────────────────────────
    nearby_section = []
    for row in nearby_rows:
        price = row.get("loc_price")
        nearby_section.append({
            "location_name":  row.get("name", ""),
            "airport_code":   row.get("airport", "") or None,
            "location_key":   row.get("location_key", ""),
            "distance_miles": row.get("dist"),
            "cab_fare":       row.get("cab"),
            "best_price":     round(price, 2) if price is not None else None,
            "best_provider":  row.get("price_src") or None,
            "net_saving":     round(row["net_saving"], 2) if row.get("net_saving") is not None else None,
            "is_deal":        bool(row.get("deal")),
        })

    # ── summary section ──────────────────────────────────────────────────────
    # "direct" = provider had a direct URL (not Kayak-sourced)
    direct = [r for r in results_section if r["source"] == "direct" and r["price"] is not None]
    ota    = [r for r in results_section if r["source"] == "Kayak"  and r["price"] is not None]

    def _best(lst):
        if not lst:
            return None, None, None
        b = max(lst, key=lambda x: (x["saving"] or -9999))
        return b["provider"], b["price"], b["saving"]

    bd_prov, bd_price, bd_save = _best(direct)
    bo_prov, bo_price, bo_save = _best(ota)

    summary_section = {
        "best_direct_provider": bd_prov,
        "best_direct_price":    round(bd_price, 2) if bd_price is not None else None,
        "best_direct_saving":   round(bd_save, 2)  if bd_save  is not None else None,
        "best_ota_provider":    bo_prov,
        "best_ota_price":       round(bo_price, 2) if bo_price is not None else None,
        "best_ota_saving":      round(bo_save, 2)  if bo_save  is not None else None,
    }

    # ── assemble and write ───────────────────────────────────────────────────
    payload = {
        "booking":         booking_section,
        "last_checked":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "runtime_seconds": round(total_seconds, 1),
        "results":         results_section,
        "nearby":          nearby_section,
        "summary":         summary_section,
    }

    out_path = Path(__file__).parent / "latest_results.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Results written to latest_results.json")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_supabase() -> "SupabaseClient":
    if not _SUPABASE_AVAILABLE:
        raise RuntimeError("supabase package not installed — run: pip install supabase")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables must be set")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def load_booking_from_supabase(booking_id: str) -> dict:
    sb = get_supabase()
    result = sb.table("bookings").select("*").eq("id", booking_id).single().execute()
    b = result.data
    if not b:
        raise ValueError(f"Booking {booking_id} not found in Supabase")
    return {
        "provider":           b["provider"],
        "car_class":          b["car_class"] or "Full Size SUV",
        "acriss_code":        b.get("acriss_code") or "GFAR",
        "location":           b.get("location") or f"{b['airport_code']} Airport",
        "airport_code":       b["airport_code"],
        "pickup_date":        str(b["pickup_date"]),
        "pickup_time":        b.get("pickup_time") or "12:00",
        "return_date":        str(b["return_date"]),
        "return_time":        b.get("return_time") or "12:00",
        "booked_price":       float(b["booked_price"]),
        "driver_age":         int(b.get("driver_age") or 31),
        "payment_type":       b.get("payment_type") or "PAY_LATER",
        "free_cancellation":  b.get("free_cancellation", True),
        "unlimited_mileage":  False,
        "transmission":       "AUTOMATIC",
        "min_passengers":     5,
        "ac_required":        True,
        "pickup_lat":         float(b.get("pickup_lat") or 0),
        "pickup_lng":         float(b.get("pickup_lng") or 0),
    }


def save_results_to_supabase(booking_id: str, results_data: dict) -> None:
    try:
        sb = get_supabase()
        sb.table("price_results").insert({
            "booking_id":       booking_id,
            "checked_at":       datetime.now().isoformat(),
            "runtime_seconds":  results_data.get("runtime_seconds"),
            "results":          results_data.get("results"),
            "nearby":           results_data.get("nearby"),
            "summary":          results_data.get("summary"),
        }).execute()
        print(f"Results saved to Supabase for booking {booking_id}")
    except Exception as e:
        print(f"Failed to save to Supabase: {e}")


def _reinitialize_location_constants() -> None:
    """Re-run all module-level location lookups after BOOKING has been updated."""
    global SIXT_LOCATION
    global HERTZ_STATION_CODE, HERTZ_RESULTS_URL
    global DOLLAR_STATION_CODE, DOLLAR_RESULTS_URL
    global THRIFTY_STATION_CODE, THRIFTY_RESULTS_URL
    global KAYAK_LOCATION_ID
    global EH_LOCATION_CONFIG
    global ACTIVE_CAR_CLASS

    airport = BOOKING["airport_code"]

    # SIXT
    _sixt_loc = _db_lookup("SIXT", airport)
    if _sixt_loc:
        _sixt_title = (
            _sixt_loc.get("name") or
            _sixt_loc.get("title", "").replace("+", " ")
        ).strip()
        SIXT_LOCATION = {
            "branch_id": f"BRANCH:{_sixt_loc['location_id']}",
            "title":     _sixt_title,
        }
    else:
        SIXT_LOCATION = None

    # Hertz
    _hertz_loc = _db_lookup("Hertz", airport)
    HERTZ_STATION_CODE = _hertz_loc["station_code"] if _hertz_loc else None
    if HERTZ_STATION_CODE:
        HERTZ_RESULTS_URL = (
            "https://www.hertz.com/us/en/book/vehicles"
            "?pid={station}"
            "&pdate={pickup_date}T{pickup_time}:00"
            "&did={station}"
            "&ddate={return_date}T{return_time}:00"
            "&pCountryCode=US"
            "&age={age}"
        ).format(
            station=HERTZ_STATION_CODE,
            pickup_date=BOOKING["pickup_date"],
            pickup_time=BOOKING["pickup_time"],
            return_date=BOOKING["return_date"],
            return_time=BOOKING["return_time"],
            age=BOOKING["driver_age"],
        )
    else:
        HERTZ_RESULTS_URL = None

    # Dollar / Thrifty
    _dollar_loc  = _db_lookup("Dollar",  airport)
    _thrifty_loc = _db_lookup("Thrifty", airport)
    DOLLAR_STATION_CODE  = _dollar_loc["station_code"]  if _dollar_loc  else None
    THRIFTY_STATION_CODE = _thrifty_loc["station_code"] if _thrifty_loc else None
    if DOLLAR_STATION_CODE:
        DOLLAR_RESULTS_URL = _DOLLAR_THRIFTY_URL_TMPL.format(
            base="https://www.dollar.com",
            station=DOLLAR_STATION_CODE,
            pickup_date=BOOKING["pickup_date"],
            pickup_time=BOOKING["pickup_time"],
            return_date=BOOKING["return_date"],
            return_time=BOOKING["return_time"],
            age=BOOKING["driver_age"],
        )
    else:
        DOLLAR_RESULTS_URL = None
    if THRIFTY_STATION_CODE:
        THRIFTY_RESULTS_URL = _DOLLAR_THRIFTY_URL_TMPL.format(
            base="https://www.thrifty.com",
            station=THRIFTY_STATION_CODE,
            pickup_date=BOOKING["pickup_date"],
            pickup_time=BOOKING["pickup_time"],
            return_date=BOOKING["return_date"],
            return_time=BOOKING["return_time"],
            age=BOOKING["driver_age"],
        )
    else:
        THRIFTY_RESULTS_URL = None

    # Kayak
    _kayak_loc = _db_lookup("Kayak", airport)
    KAYAK_LOCATION_ID = _kayak_loc["location_id"] if _kayak_loc else None

    # EHI (Enterprise / National / Alamo)
    _ehi_loc = _db_lookup("EHI", airport)
    EH_LOCATION_CONFIG = {}
    if _ehi_loc:
        EH_LOCATION_CONFIG[airport] = {
            "id":              _ehi_loc["location_id"],
            "national_id":     _ehi_loc.get("national_gma_id", _ehi_loc["location_id"]),
            "alamo_id":        _ehi_loc.get("alamo_gma_id", _ehi_loc.get("national_gma_id", _ehi_loc["location_id"])),
            "group_branch_id": _ehi_loc["group_branch_id"],
            "name":            _ehi_loc["name"],
            "airport_code":    _ehi_loc["airport_code"],
            "country_code":    _ehi_loc["country_code"],
            "gps":             _ehi_loc["gps"],
            "time_zone_id":    _ehi_loc["time_zone_id"],
        }

    # Active car class from acriss_code (if provided in booking)
    acriss = BOOKING.get("acriss_code", "GFAR")
    if acriss in CAR_CLASS_EQUIVALENTS:
        ACTIVE_CAR_CLASS = acriss

    print(
        f"  [Config] Reinitialized for {airport} — "
        f"SIXT={'✓' if SIXT_LOCATION else '✗'}  "
        f"Hertz={'✓' if HERTZ_STATION_CODE else '✗'}  "
        f"Kayak={'✓' if KAYAK_LOCATION_ID else '✗'}  "
        f"EHI={'✓' if EH_LOCATION_CONFIG else '✗'}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    global BD_SEMAPHORE
    BD_SEMAPHORE = asyncio.Semaphore(BD_MAX_CONCURRENT)

    from playwright.async_api import async_playwright

    # ── Supabase / CLI argument handling ────────────────────────────────────
    parser = argparse.ArgumentParser(description="Car rental price monitor")
    parser.add_argument("--booking-id", type=str, default=None,
                        help="Supabase booking UUID — loads booking from Supabase instead of hardcoded BOOKING dict")
    args, _unknown = parser.parse_known_args()   # _unknown absorbs unrecognised flags (e.g. --test-ehi)

    booking_id: Optional[str] = args.booking_id

    if booking_id:
        print(f"Loading booking {booking_id} from Supabase...")
        booking_data = load_booking_from_supabase(booking_id)
        BOOKING.update(booking_data)
        _reinitialize_location_constants()
    # ─────────────────────────────────────────────────────────────────────────

    t_wall = time.monotonic()

    # ── Bright Data debug ────────────────────────────────────────────────────
    print(f"\n[DEBUG] BRIGHT_DATA_CDP_URL = {BRIGHT_DATA_CDP_URL!r}")
    if BRIGHT_DATA_CDP_URL:
        print("[DEBUG] Bright Data mode ACTIVE — providers will attempt direct URLs first")
    else:
        print("[DEBUG] Bright Data mode INACTIVE — all Kayak-backed providers will use Kayak")
    # ─────────────────────────────────────────────────────────────────────────

    _pay   = BOOKING.get("payment_type") or "any"
    _fc    = "yes" if BOOKING.get("free_cancellation") else "no"
    _seats = BOOKING.get("min_passengers") or "any"
    _um    = "yes" if BOOKING.get("unlimited_mileage") else "no"
    print(f"\n>>  Price Monitor — {len(PROVIDERS)} providers | "
          f"{BOOKING['airport_code']} | "
          f"{BOOKING['pickup_date']} → {BOOKING['return_date']}")
    print(f"   Reference : {BOOKING['provider']} {BOOKING['car_class']} "
          f"@ ${BOOKING['booked_price']:.2f}")
    print(f"   Kayak fs= : carclass=SUV"
          + (";carpolicies=cancel" if BOOKING.get("free_cancellation") else "")
          + (";unlimitedmileage=1" if BOOKING.get("unlimited_mileage") else "")
          + "  (carcapacity filter removed — too restrictive)")
    print(f"   Pref      : payment={_pay} (informational — not applied as Kayak filter)  "
          f"free_cancel={_fc}  seats≥{_seats}  unlimited_miles={_um}\n")

    # results_map: provider → (result_dict, elapsed_seconds)
    results_map: Dict[str, tuple] = {}

    async with async_playwright() as pw:

        # ── Helper: run one provider check and store result ────────────────
        async def _timed_check(provider: str) -> None:
            t0     = time.monotonic()
            result = await check_provider(pw, provider)
            elapsed = time.monotonic() - t0
            price  = result.get("price")
            saving = (BOOKING["booked_price"] - price) if price else None
            log_result(result, saving)
            results_map[provider] = (result, elapsed)
            if result.get("error"):
                err_oneline = result["error"].replace("\n", " ").replace("\r", "")[:120]
                if result.get("na"):
                    print(f"  —  {provider}: N/A — {err_oneline}  [{elapsed:.1f}s]")
                else:
                    print(f"  ✗  {provider}: ERROR — {err_oneline}  [{elapsed:.1f}s]")
            else:
                note = (f"  → save ${saving:.2f} ✓" if saving and saving >= MIN_SAVING else "")
                print(f"  ✓  {provider}: ${price:.2f}{note}  [{elapsed:.1f}s]")

        # ── Phase 0: nearby-location discovery (instant — reads static DB) ───
        print("Phase 0 — Nearby location discovery (airports + city branches, from locations_db.json)")
        nearby_locations = discover_nearby_locations(BOOKING)
        print()

        # ── Phase 1: Kayak pre-fetch + SIXT + Avis run in parallel ─────────
        # Kayak opens 1 (best) + len(_KAYAK_AGENCY_SLUGS) tabs simultaneously;
        # SIXT and Avis each open their own independent browser, so all three
        # groups run truly concurrently.
        _kayak_tab_count = 1 + len(_KAYAK_AGENCY_SLUGS)  # best + agency tabs
        print(f"Phase 1 — Kayak ({_kayak_tab_count} parallel tabs) + SIXT + Avis  [running concurrently]")
        await asyncio.gather(
            _fetch_kayak_results(pw),  # populates _kayak_cache
            _timed_check("SIXT"),
            _timed_check("Avis"),
        )
        print()

        # Report cache status and run sanity check on Kayak prices
        cache = _kayak_cache or {}
        found = [p for p in _KAYAK_TARGETS if p in cache]
        print(f"  Kayak cache: {len(found)}/{len(_KAYAK_TARGETS)} agencies  "
              f"best=${cache.get('__kayak_best__', {}).get('price', 'n/a')}")
        _sanity_check_kayak_prices(cache)
        print()

        # ── Phase 2: remaining providers from cache (all instant) ──────────
        # Hertz, Budget, National, Enterprise, Alamo, Dollar, Thrifty, Kayak
        # all read from _kayak_cache — zero additional browser time needed.
        remaining = [p for p in PROVIDERS if p not in ("SIXT", "Avis")]
        print(f"Phase 2 — {len(remaining)} providers from Kayak cache  [parallel]")
        await asyncio.gather(*[_timed_check(p) for p in remaining])
        print()

        # ── Phase 3: nearby airport price lookups (Kayak + Hertz + EHI + SIXT) ─
        # Kayak/Hertz/EHI each open their own Bright Data browser session.
        # SIXT uses the pure API (no browser needed) — runs concurrently with the rest.
        # Prices are merged into nearby_prices, taking min per airport.
        # nearby_sources tracks which provider gave the best (cheapest) price.
        nearby_prices:  Dict[str, float] = {}
        nearby_sources: Dict[str, str]   = {}
        phase3_tasks   = []
        phase3_labels  = []
        phase3_sources = []   # human-readable source name per task, same order as tasks
        if nearby_locations.get("Kayak"):
            n = len(nearby_locations["Kayak"])
            phase3_tasks.append(fetch_nearby_kayak_prices(pw, nearby_locations))
            phase3_labels.append(f"Kayak/{n}")
            phase3_sources.append("Kayak")
        if nearby_locations.get("Hertz") and BRIGHT_DATA_CDP_URL:
            n = len(nearby_locations["Hertz"])
            phase3_tasks.append(fetch_nearby_hertz_prices(pw, nearby_locations))
            phase3_labels.append(f"Hertz/{n}")
            phase3_sources.append("Hertz")
        if nearby_locations.get("EHI") and BRIGHT_DATA_CDP_URL:
            n = len(nearby_locations["EHI"])
            phase3_tasks.append(fetch_nearby_ehi_prices(pw, nearby_locations))
            phase3_labels.append(f"EHI/{n}")
            phase3_sources.append("EHI")
        if nearby_locations.get("SIXT"):
            n = len(nearby_locations["SIXT"])
            phase3_tasks.append(fetch_nearby_sixt_prices(nearby_locations))
            phase3_labels.append(f"SIXT/{n} via API")
            phase3_sources.append("SIXT")

        if phase3_tasks:
            print(f"Phase 3 — Nearby airport prices  [{' | '.join(phase3_labels)}]")
            phase3_results = await asyncio.gather(*phase3_tasks, return_exceptions=True)

            # Collect all per-provider results for the debug table
            all_provider_prices: Dict[str, Dict[str, float]] = {}  # {src: {code: price}}
            for pr, src in zip(phase3_results, phase3_sources):
                if isinstance(pr, Exception):
                    print(f"  [Phase3] {src} raised an exception: {pr}")
                    continue
                if isinstance(pr, dict):
                    all_provider_prices[src] = pr
                    for code, price in pr.items():
                        if code not in nearby_prices or price < nearby_prices[code]:
                            nearby_prices[code] = price
                            nearby_sources[code] = src

            # Debug summary — which providers were checked at each location
            all_codes = sorted({c for prices in all_provider_prices.values() for c in prices})
            if all_codes:
                src_cols = list(all_provider_prices.keys())
                header = f"  {'Location':<32}" + "".join(f"  {s:<12}" for s in src_cols) + "  Best"
                print(f"\n  [Phase3 Debug] Provider prices per nearby location:")
                print(f"  {'-' * (len(header) - 2)}")
                print(header)
                print(f"  {'-' * (len(header) - 2)}")
                for code in all_codes:
                    row = f"  {code:<32}"
                    for s in src_cols:
                        p = all_provider_prices[s].get(code)
                        row += f"  {'${:.2f}'.format(p) if p else 'N/A':<12}"
                    best_p = nearby_prices.get(code)
                    best_s = nearby_sources.get(code, "?")
                    row += f"  ${best_p:.2f} ({best_s})" if best_p else "  N/A"
                    print(row)
                print(f"  {'-' * (len(header) - 2)}\n")

            print(f"  Phase 3 done — {len(nearby_prices)} locations priced")
            print()

    # Reconstruct in canonical PROVIDERS order
    results = [results_map[p][0] for p in PROVIDERS if p in results_map]

    total = time.monotonic() - t_wall
    print(f"Total runtime: {total:.1f}s\n")

    print_results(results)

    # ── Nearby-location opportunities ──────────────────────────────────────
    nearby_rows: List[Dict] = []
    if nearby_locations:
        nearby_rows = _print_nearby_opportunities(
            results, nearby_locations, nearby_prices, nearby_sources
        ) or []

    print(f"Results logged to: {os.path.abspath(LOG_FILE)}")

    # ── JSON output ──────────────────────────────────────────────────────────
    results_data = _write_json_results(results, nearby_rows, total)

    # ── Git commit + push ────────────────────────────────────────────────────
    import subprocess as _sp
    _repo = Path(__file__).parent
    try:
        _sp.run(
            ["git", "add", "latest_results.json"],
            cwd=_repo, check=True, capture_output=True,
        )
        _sp.run(
            ["git", "commit", "-m",
             f"Price update {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=_repo, check=True, capture_output=True,
        )
        _sp.run(
            ["git", "push"],
            cwd=_repo, check=True, capture_output=True,
        )
        print("Results pushed to GitHub")
    except Exception as _e:
        print(f"Git push skipped: {_e}")

    # ── Supabase results upload ──────────────────────────────────────────────
    if booking_id:
        save_results_to_supabase(booking_id, results_data)


def _print_nearby_opportunities(
    results: List[Dict],
    nearby_locations: Dict[str, List[Dict]],
    nearby_prices: Optional[Dict[str, float]] = None,
    nearby_sources: Optional[Dict[str, str]] = None,
) -> None:
    """
    Show nearby locations (airports AND city/neighbourhood branches) where the
    net saving exceeds MIN_SAVING.

    Net saving = (your booked price) − (price at nearby location) − (cab fare)

    Deduplicates by location_key across all providers.  The Airport column is
    blank for non-airport branches.  The "Best price" column shows the cheapest
    price found across ALL providers (Kayak, Hertz, EHI, SIXT) for each location.

    Args:
        results         : main provider results (for reference, not used in table)
        nearby_locations: output of discover_nearby_locations()
        nearby_prices   : {location_key: best_price} merged across all Phase 3 sources.
        nearby_sources  : {location_key: source_name} — which provider gave the best price.
    """
    booked  = BOOKING["booked_price"]
    airport = BOOKING["airport_code"]
    price_by_key: Dict[str, float] = dict(nearby_prices or {})
    source_by_key: Dict[str, str]  = dict(nearby_sources or {})

    rows = []
    seen_keys: set = set()

    for provider, location_list in nearby_locations.items():
        for loc in location_list:
            lkey = loc.get("location_key", "") or loc.get("airport_code", "")
            if not lkey:
                continue
            if lkey in seen_keys:
                continue
            seen_keys.add(lkey)

            dist         = loc.get("distance_miles", 0)
            cab          = loc.get("cab_fare") or _cab_fare(dist)
            name         = loc.get("name", lkey)
            airport_code = loc.get("airport_code", "")   # blank for city branches
            is_airport   = bool(loc.get("is_airport", bool(airport_code)))
            loc_price    = price_by_key.get(lkey)
            price_src    = source_by_key.get(lkey, "")

            if loc_price is not None:
                net_saving = booked - loc_price - cab
                suspiciously_cheap = loc_price < (booked * 0.25)
                deal = net_saving >= MIN_SAVING and not suspiciously_cheap
            else:
                net_saving = None
                suspiciously_cheap = False
                deal = False

            rows.append({
                "name":               name,
                "airport":            airport_code,
                "location_key":       lkey,
                "is_airport":         is_airport,
                "dist":               dist,
                "cab":                cab,
                "loc_price":          loc_price,
                "price_src":          price_src,
                "net_saving":         net_saving,
                "deal":               deal,
                "suspiciously_cheap": suspiciously_cheap,
            })

    if not rows:
        return

    # Sort: deals first (by net saving desc), then by distance
    rows.sort(key=lambda x: (-x["net_saving"] if x["net_saving"] is not None else -9999, x["dist"]))

    sep = "─" * 100
    print(f"\n{sep}")
    print(f"NEARBY LOCATIONS  (net saving = booked ${booked:.2f} − location price − cab fare)")
    print(f"  Shows airports and city branches within {NEARBY_RADIUS_MILES}mi of {airport}.  "
          f"Deals require net saving ≥ ${MIN_SAVING:.0f}.")
    print(sep)
    print(f"{'Code':<6} {'Location':<30} {'Miles':>5}  {'Cab':>6}  "
          f"{'Best price':>10}  {'Via':>6}  {'Net saving':>11}  {'Deal?'}")
    print(sep)

    for r in rows:
        if r["loc_price"] is not None:
            price_str = f"${r['loc_price']:>7.2f}"
            src_str   = f"{r['price_src']:>6}" if r["price_src"] else f"{'?':>6}"
        else:
            price_str = f"{'unknown':>8}"
            src_str   = f"{'':>6}"
        if r["net_saving"] is not None:
            ns = r["net_saving"]
            if ns >= MIN_SAVING:
                net_str = f"✓ ${ns:>6.2f}"
            elif ns > 0:
                net_str = f"  ${ns:>6.2f}"
            else:
                net_str = f" −${abs(ns):>6.2f}"
        else:
            net_str = f"{'unknown':>10}"
        deal_str = "✓ YES" if r["deal"] else ("⚠ verify" if r.get("suspiciously_cheap") else "—")
        # Show IATA code for airports; blank for city branches
        code_col = r["airport"] if r["is_airport"] else ""
        print(f"{code_col:<6} {r['name'][:30]:<30} {r['dist']:>4.1f}mi "
              f"  ${r['cab']:>4.0f}  {price_str}  {src_str}  {net_str:>11}  {deal_str}")

    print(sep)
    deals = [r for r in rows if r["deal"]]
    suspicious = [r for r in rows if r.get("suspiciously_cheap") and r["loc_price"] is not None]
    if deals:
        best = deals[0]
        src_note = f" via {best['price_src']}" if best.get("price_src") else ""
        loc_id = best["airport"] or best["location_key"]
        print(f"\n  ★ Best nearby deal: {best['name']} ({loc_id})  "
              f"price=${best['loc_price']:.2f}{src_note}  cab≈${best['cab']:.0f}  "
              f"net save=${best['net_saving']:.2f}")
    else:
        print("\n  No nearby location offers a net saving above the threshold "
              f"(${MIN_SAVING:.0f}) after cab fare.")
    if suspicious:
        print(f"\n  ⚠ Suspicious prices (< 25% of booked ${booked:.0f}) — verify manually:")
        for r in suspicious:
            loc_id = r["airport"] or r["location_key"]
            print(f"    {loc_id}  {r['name'][:30]}  ${r['loc_price']:.2f}  "
                  f"(only {r['loc_price']/booked*100:.0f}% of booked price — may be wrong car class)")
    print(f"{sep}\n")
    return rows


def _quiet_exception_handler(loop, ctx):
    """Suppress noisy TargetClosedError futures from cancelled Playwright tasks."""
    exc = ctx.get("exception")
    msg = str(exc) if exc else ctx.get("message", "")
    if "TargetClosedError" in msg or "Target page" in msg:
        return  # expected when a browser is closed mid-operation
    loop.default_exception_handler(ctx)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_quiet_exception_handler)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
