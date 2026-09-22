"""
price_monitor.py
================
Car rental price monitor — any airport, any dates.

Checks Full Size SUV prices across 7 major providers, each via a direct
lightweight HTTP call to the provider's own booking API — no browser
automation. Update BOOKING["airport_code"] and the provider-specific
location configs below to point at a new airport. Results are compared
against a reference booking price and logged to CSV.

Providers: SIXT, Hertz, Enterprise, National, Alamo, Dollar, Thrifty.
(Avis/Budget are DataDome-protected and not currently supported; Kayak
is descoped.)

Usage:
    python price_monitor.py

Dependencies:
    pip install httpx
"""

import argparse
import asyncio
import builtins
import csv
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
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

from typing import Dict, List, Optional, Set

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

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
    "pickup_date":   "2026-12-02",
    "pickup_time":   "12:00",
    "return_date":   "2026-12-06",
    "return_time":   "12:00",
    "booked_price":  636.73,
    "driver_age":    31,
    # payment_type maps to HERTZ_RATE_TYPE / EHI_CHARGE_KEY (see PAYMENT TYPE section below).
    "payment_type":       "PAY_LATER",   # "PREPAID" | "PAY_LATER" | None
    "free_cancellation":  True,          # informational only — not applied as an API filter
    "unlimited_mileage":  False,         # informational only — not applied as an API filter
    "transmission":       "AUTOMATIC",   # "AUTOMATIC" | "MANUAL" | None — informational only
    "min_passengers":     5,             # informational only — not applied as an API filter
    "ac_required":        True,          # informational only
    "providers_to_check": None,          # None = check all providers; list of names to restrict
    "additional_classes": None,          # None = only check booked class; list of ACRISS codes for extra class checks
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

# Hertz — station code used by the vehicle-rates API.
_hertz_loc = _db_lookup("Hertz", BOOKING["airport_code"])
HERTZ_STATION_CODE: Optional[str] = _hertz_loc["station_code"] if _hertz_loc else None
if not HERTZ_STATION_CODE:
    print(f"  [DB] No Hertz entry for {BOOKING['airport_code']} — Hertz will return N/A.")

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

# After _check_ehi_all() runs Enterprise/National, the browser/page/ctx are kept
# alive here so fetch_nearby_ehi_prices() can reuse them without re-loading
# enterprise.com.  Closed at the END of fetch_nearby_ehi_prices().
_ehi_enterprise_shared: Dict = {"browser": None, "page": None, "ctx": None}


def _get_ehi_lock() -> "asyncio.Lock":
    global _ehi_lock
    if _ehi_lock is None:
        _ehi_lock = asyncio.Lock()
    return _ehi_lock

# ─────────────────────────────────────────────────────────────────────────────
# PROVIDERS & URLS
# ─────────────────────────────────────────────────────────────────────────────

PROVIDERS = [
    "SIXT", "Hertz", "Enterprise", "National", "Alamo", "Dollar", "Thrifty",
]

PROVIDER_URLS = {
    "SIXT":       "https://www.sixt.com",
    "Hertz":      "https://www.hertz.com",
    "National":   "https://www.nationalcar.com",
    "Enterprise": "https://www.enterprise.com",
    "Alamo":      "https://www.alamo.com",
    "Dollar":     "https://www.dollar.com",
    "Thrifty":    "https://www.thrifty.com",
}

# SIXT — betafunnel URL is built at runtime inside check_sixt() because the
# offer_location_uuid is session-specific (fetched fresh from the SelectLocation
# API on every check).  No module-level URL constant is needed.

# Dollar / Thrifty — same Hertz Holdings API as Hertz, but each has its OWN
# station codes, stored separately in locations_db.json (they frequently
# differ from Hertz's station codes, e.g. LGA: Hertz=LGAT01, Dollar=LGAO01).
_dollar_loc  = _db_lookup("Dollar",  BOOKING["airport_code"])
_thrifty_loc = _db_lookup("Thrifty", BOOKING["airport_code"])
DOLLAR_STATION_CODE:  Optional[str] = _dollar_loc["station_code"]  if _dollar_loc  else None
THRIFTY_STATION_CODE: Optional[str] = _thrifty_loc["station_code"] if _thrifty_loc else None
if not DOLLAR_STATION_CODE:
    print(f"  [DB] No Dollar entry for {BOOKING['airport_code']} — Dollar will return N/A.")
if not THRIFTY_STATION_CODE:
    print(f"  [DB] No Thrifty entry for {BOOKING['airport_code']} — Thrifty will return N/A.")

# CSV log file path
LOG_FILE = "price_log.csv"

# Realistic Chrome user-agent used on every outbound HTTP call.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────────────────────────
# BRIGHT DATA ISP PROXY
# ─────────────────────────────────────────────────────────────────────────────
# Hertz Holdings (Hertz/Dollar/Thrifty) blocks datacenter IPs at the WAF layer
# (both the OAuth token endpoint and vehicle-rates return a 403/Incapsula
# challenge from the VPS's IP). Routing those calls through a residential ISP
# proxy resolves it. Enterprise/National/Alamo and SIXT do NOT need a proxy —
# their APIs accept calls straight from the server.
#
# Set via .env / environment: BD_ISP_PROXY="http://user:pass@host:port"
BD_ISP_PROXY: str = os.environ.get("BD_ISP_PROXY", "")
if not BD_ISP_PROXY:
    print("  [Config] BD_ISP_PROXY not set — Hertz/Dollar/Thrifty will fail from a datacenter IP.")

# ─────────────────────────────────────────────────────────────────────────────
# HERTZ DIRECT API — Hertz Holdings platform (Hertz / Dollar / Thrifty)
# ─────────────────────────────────────────────────────────────────────────────
# Hertz uses a standard OAuth2 client_credentials flow.  The client_id/secret
# are embedded in hertz.com's JS bundle (public, unauthenticated scope).
# Token is valid 1799 s (~30 min) and is cached module-wide so all calls
# within a run (main check + all nearby stations) share one token fetch.
#
# Flow discovered via browser network capture (2026-05-08):
#   POST https://api.hertz.io/api/login/token
#     Authorization: Basic <base64(client_id:secret)>
#     Content-Type:  application/x-www-form-urlencoded
#     Body:          grant_type=client_credentials
#   → { access_token, expires_in: 1799, scope: "prod unauthenticated" }
#
#   GET https://api.hertz.io/vehicle-rates?...
#     Authorization: Bearer <access_token>
#     client-id: 6L2J
#     correlation-id: <uuid4>
#   → [ { sipp_code, vehicle_display_name, pricing: { ... } }, ... ]
# ─────────────────────────────────────────────────────────────────────────────

_HERTZ_OAUTH_URL  = "https://api.hertz.io/api/login/token"
_HERTZ_RATES_URL  = "https://api.hertz.io/vehicle-rates"
# base64( client_id : client_secret ) — embedded in hertz.com JS bundle
_HERTZ_BASIC_AUTH = (
    "NjM5U0pXeTVUM2xJOXdpeHdVOUE3Q1JRUXpzQVpsT0RqclJ6dDdQeU1FRTpfYUR4UlA3ZVFORGNHLWt5"
    "LWY1MDhHSjlaQVdvRHoyMll2YjNqREZKZ2lv"
)
_HERTZ_CLIENT_ID  = "6L2J"
# Module-level token cache — shared across all calls within a run
_hertz_token_cache: Dict = {"token": None, "expires_at": 0.0}


def _hertz_get_token() -> str:
    """
    Return a valid Hertz Bearer token, fetching a fresh one when the cached
    token has expired (or has never been fetched).  Thread-safe enough for
    asyncio.to_thread() use: worst case two threads fetch simultaneously and
    the second write wins — both tokens are equally valid.

    Routed through BD_ISP_PROXY: this endpoint 403s (Incapsula) when called
    from a datacenter IP.
    """
    now = time.time()
    if _hertz_token_cache["token"] and now < _hertz_token_cache["expires_at"]:
        return _hertz_token_cache["token"]

    with httpx.Client(proxy=BD_ISP_PROXY or None, timeout=20) as client:
        resp = client.post(
            _HERTZ_OAUTH_URL,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": "Basic " + _HERTZ_BASIC_AUTH,
                "Content-Type":  "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent":    USER_AGENT,
            },
        )
        resp.raise_for_status()
        tok = resp.json()

    access_token = tok["access_token"]
    expires_in   = int(tok.get("expires_in", 1799))
    _hertz_token_cache["token"]      = access_token
    _hertz_token_cache["expires_at"] = now + expires_in - 30  # 30 s safety margin
    print(f"  [Hertz/API] Token obtained (valid {expires_in}s, scope={tok.get('scope')})")
    return access_token


def _hertz_direct_rates(
    station: str,
    pickup_dt: str,
    return_dt: str,
    age: int,
    brand: str = "HERTZ",
) -> list:
    """
    Call api.hertz.io/vehicle-rates directly and return the raw vehicle list.
    Covers Hertz, Dollar, and Thrifty (same Hertz Holdings platform — only the
    `brand` param and station code differ).

    Args:
        station   : OAG station code (e.g. "LGAT01") or plain airport code
                    for Dollar, which the API expects unprefixed (e.g. "LGA").
        pickup_dt : ISO datetime string "YYYY-MM-DDTHH:MM:00"
        return_dt : ISO datetime string "YYYY-MM-DDTHH:MM:00"
        age       : driver age (integer)
        brand     : "HERTZ", "DOLLAR", or "THRIFTY"

    Returns:
        list of vehicle dicts (may be empty if no availability).

    Raises:
        httpx.HTTPStatusError / Exception on network/API failure.
    """
    token  = _hertz_get_token()
    params = {
        "rental_type":           "LEISURE",
        "brand":                 brand,
        "country_code":          "US",
        "drop_off_location":     station,
        "drop_off_time":         return_dt,
        "min_customer_age":      str(age),
        "pick_up_location":      station,
        "pick_up_time":          pickup_dt,
        "embed":                 "PRICING",
        "customer_country_code": "US",
    }
    headers = {
        "Authorization":             "Bearer " + token,
        "client-id":                 _HERTZ_CLIENT_ID,
        "Accept":                    "*/*",
        "Accept-Language":           "en-US",
        "Content-Type":              "application/json",
        "Origin":                    f"https://www.{brand.lower()}.com",
        "Referer":                   f"https://www.{brand.lower()}.com/",
        "User-Agent":                USER_AGENT,
        "correlation-id":            str(uuid.uuid4()),
        "platform-name":             "GBP",
        "vehicle-smartpick-enabled": "false",
    }
    with httpx.Client(proxy=BD_ISP_PROXY or None, timeout=30) as client:
        resp = client.get(_HERTZ_RATES_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else [data]


def _hertz_holdings_booking_url(domain: str, station: str) -> str:
    """Deep link into a Hertz Holdings site's results, prepopulated with this booking's dates/age."""
    return (
        f"https://www.{domain}/us/en/book/vehicles"
        f"?pid={station}"
        f"&pdate={BOOKING['pickup_date']}T{BOOKING['pickup_time']}:00"
        f"&did={station}"
        f"&ddate={BOOKING['return_date']}T{BOOKING['return_time']}:00"
        f"&pCountryCode=US"
        f"&age={BOOKING['driver_age']}"
    )


async def check_hertz() -> Dict:
    """Hertz Full Size SUV price via the direct OAuth2 + vehicle-rates API."""
    if not should_check_provider("Hertz"):
        return make_result("Hertz", na=True, error="Not in providers_to_check")
    if not HERTZ_STATION_CODE:
        airport = BOOKING["airport_code"]
        return make_result("Hertz", na=True, error=f"No Hertz station for {airport}")

    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}:00"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}:00"
    age       = int(BOOKING["driver_age"])

    try:
        vehicle_data = await asyncio.to_thread(
            _hertz_direct_rates, HERTZ_STATION_CODE, pickup_dt, return_dt, age, "HERTZ"
        )
    except Exception as exc:
        return make_result("Hertz", error=f"API error: {str(exc)[:150]}")

    print(f"  [Hertz] {len(vehicle_data)} vehicles returned")
    best_price, best_name = _hertz_extract_best(vehicle_data)
    if best_price is None:
        sipp_list = [v.get("sipp_code") for v in vehicle_data]
        return make_result("Hertz", error=f"No Full Size SUV — SIPP codes seen: {sipp_list[:8]}")

    print(f"  [Hertz] Best: {best_name} @ ${best_price:.2f}")
    return make_result(
        "Hertz",
        car_class="Full Size SUV",
        model=best_name,
        price=best_price,
        url=_hertz_holdings_booking_url("hertz.com", HERTZ_STATION_CODE),
    )


async def check_dollar() -> Dict:
    """
    Dollar Full Size SUV price via the Hertz Holdings API (brand=DOLLAR).

    NOTE: the station code stored in locations_db.json (e.g. "LGAO01") is a
    display/URL code that the vehicle-rates API rejects with "INVALID PICKUP
    LOCATION". The API expects the plain IATA airport code instead — confirmed
    working for LGA. Using BOOKING["airport_code"] rather than the DB field.
    """
    if not should_check_provider("Dollar"):
        return make_result("Dollar", na=True, error="Not in providers_to_check")
    if not DOLLAR_STATION_CODE:
        airport = BOOKING["airport_code"]
        return make_result("Dollar", na=True, error=f"No Dollar station for {airport}")

    airport   = BOOKING["airport_code"]
    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}:00"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}:00"
    age       = int(BOOKING["driver_age"])

    try:
        vehicle_data = await asyncio.to_thread(
            _hertz_direct_rates, airport, pickup_dt, return_dt, age, "DOLLAR"
        )
    except Exception as exc:
        return make_result("Dollar", error=f"API error: {str(exc)[:150]}")

    print(f"  [Dollar] {len(vehicle_data)} vehicles returned")
    best_price, best_name = _dollar_thrifty_extract_best(vehicle_data)
    if best_price is None:
        cats = [v.get("vehicle_category") for v in vehicle_data]
        return make_result("Dollar", error=f"No Full Size SUV — categories seen: {cats[:8]}")

    print(f"  [Dollar] Best: {best_name} @ ${best_price:.2f}")
    return make_result(
        "Dollar",
        car_class="Full Size SUV",
        model=best_name,
        price=best_price,
        url=_hertz_holdings_booking_url("dollar.com", airport),
    )


async def check_thrifty() -> Dict:
    """
    Thrifty Full Size SUV price via the Hertz Holdings API (brand=THRIFTY).

    Thrifty has no counter at most major airports — locations_db.json only
    contains the nearest off-airport branch (e.g. LGA's nearest is a Queens
    location, closed Sundays). Returns N/A when no station exists; a business
    error like "RETURN LOCATION CLOSED" for a real but closed-at-that-time
    station is surfaced as a normal ERROR, not silently swallowed.
    """
    if not should_check_provider("Thrifty"):
        return make_result("Thrifty", na=True, error="Not in providers_to_check")
    if not THRIFTY_STATION_CODE:
        airport = BOOKING["airport_code"]
        return make_result("Thrifty", na=True, error=f"No Thrifty station near {airport}")

    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}:00"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}:00"
    age       = int(BOOKING["driver_age"])

    try:
        vehicle_data = await asyncio.to_thread(
            _hertz_direct_rates, THRIFTY_STATION_CODE, pickup_dt, return_dt, age, "THRIFTY"
        )
    except Exception as exc:
        return make_result("Thrifty", error=f"API error: {str(exc)[:150]}")

    print(f"  [Thrifty] {len(vehicle_data)} vehicles returned")
    best_price, best_name = _dollar_thrifty_extract_best(vehicle_data)
    if best_price is None:
        cats = [v.get("vehicle_category") for v in vehicle_data]
        return make_result("Thrifty", error=f"No Full Size SUV — categories seen: {cats[:8]}")

    print(f"  [Thrifty] Best: {best_name} @ ${best_price:.2f}")
    return make_result(
        "Thrifty",
        car_class="Full Size SUV",
        model=best_name,
        price=best_price,
        url=_hertz_holdings_booking_url("thrifty.com", THRIFTY_STATION_CODE),
    )


def _dollar_thrifty_extract_best(vehicle_data: list) -> tuple:
    """
    Extract cheapest Full Size SUV from Dollar/Thrifty vehicle-rates data.
    Same api.hertz.io/vehicle-rates response shape as Hertz, but their
    vehicle_category field is a composite string (e.g. "SUV Full-size")
    rather than Hertz's vehicle_body_type list — matched by substring instead.
    Returns (price_float, model_str) or (None, "").
    """
    _active_cls = CAR_CLASS_EQUIVALENTS.get(ACTIVE_CAR_CLASS, {})
    sipp_codes: Set[str] = set(_active_cls.get("hertz_sipp_codes", {"FFAR", "FFDR"}))

    def _is_fullsize(v: dict) -> bool:
        sipp = v.get("sipp_code", "")
        if sipp in sipp_codes:
            return True
        cat = (v.get("vehicle_category") or "").lower()
        return "suv" in cat and "full" in cat

    best_price: Optional[float] = None
    best_name = ""
    for v in vehicle_data:
        if not _is_fullsize(v):
            continue
        name = v.get("make_model") or v.get("sipp_code") or "Full Size SUV"
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


# ACRISS codes and class name keywords that map to Full Size SUV.
# These are checked both as substrings AND via the word-split logic in is_fullsize_suv().
FULLSIZE_SUV_KEYWORDS = [
    # Explicit full-size SUV phrases (SUV required)
    "full size suv", "fullsize suv", "full-size suv",
    "large suv", "premium suv",
    # NOTE: "full size", "fullsize", "full-size" alone are NOT listed here because
    # some providers use these labels for Full-Size Sedans (Toyota Camry class) too.
    # Matching is handled by the word-split + "suv" check in is_fullsize_suv().
    # ACRISS codes — G=Full-size, F=4WD/SUV, A=Automatic, R=A/C
    "gfar", "gpar", "gsar", "guar", "gfmr",
    "ifar", "ipar",  # Intermediate 4WD
]

# ─────────────────────────────────────────────────────────────────────────────
# CAR CLASS EQUIVALENCY
# Maps ACRISS codes → equivalent terms on each provider's website.
# Used to ensure we only compare genuinely equivalent vehicle classes.
# ─────────────────────────────────────────────────────────────────────────────

CAR_CLASS_EQUIVALENTS: Dict[str, Dict] = {
    "GFAR": {   # Full-size 4WD/SUV Automatic with A/C — e.g. SIXT FULLSIZE ELITE SUV
        "name":           "Full-size SUV",
        "acriss_regex":   r"\b[GI][FP][A-Z][A-Z]\b",  # G or I body, F or P (4WD)
        # ── Provider-specific identifiers ─────────────────────────────────────
        # Hertz/Dollar/Thrifty: SIPP codes accepted as Full Size SUV.
        #   FFAR = Full Size SUV 2WD / FFDR = Full Size SUV AWD
        "hertz_sipp_codes": {"FFAR", "FFDR"},
        # EHI: SIPP code → display name mapping used when the API doesn't
        #   supply a name; others fall through to is_fullsize_suv() text matching.
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
    image_url: str = "",
) -> Dict:
    """Return a standardised result dict.

    Set na=True for "known N/A" cases (no station at this airport, location
    permanently closed) — these show as N/A in the output rather than ERROR,
    since there is nothing wrong with the monitor; the provider simply has no
    coverage at this airport.

    model should be the actual vehicle (e.g. "Ford Expedition", "BMW X5"),
    not a generic class label — that's what actually differs between
    providers within the "same" booked class and is worth showing.
    """
    return {
        "provider":  provider,
        "car_class": car_class,
        "model":     model,
        "price":     price,
        "url":       url,
        "error":     error,
        "na":        na,
        "image_url": image_url,
    }


def should_check_provider(provider_name: str) -> bool:
    providers = BOOKING.get("providers_to_check")
    if not providers:
        return True  # None or empty = check all
    return provider_name in providers


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


async def check_sixt() -> Dict:
    """
    Fetch SIXT Full Size SUV prices via direct gRPC-JSON API calls (no browser).

    Flow:
      1. Look up the SIXT branch for BOOKING["airport_code"] in locations_db.json.
      2. SelectLocation API → session-specific location_selection_id UUID.
      3. GetOfferRecommendationsV2 API → all offers with prices.
      4. Filter for Full Size SUV (ACRISS[1] == 'F', not compact/economy/minivan).
      5. Return cheapest match.
    """
    if not should_check_provider("SIXT"):
        return make_result("SIXT", na=True, error="Not in providers_to_check")
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


def _ehi_parse_car_classes(data: dict) -> list:
    """
    Extract the car_classes list from an EHI reservations/initiate response,
    trying every known response shape (varies by brand/endpoint).
    """
    for path in (
        ("gma", "gbo", "reservation", "car_classes"),
        ("session", "gbo", "reservation", "car_classes"),
        ("session", "analytics", "gbo", "reservation", "car_classes"),
    ):
        node = data
        for key in path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, list) and node:
            return node
    return []


def _ehi_class_price(cc: dict) -> Optional[float]:
    """Extract the price for EHI_CHARGE_KEY (PAYLATER/PREPAY) from one car_class dict."""
    charges = cc.get("charges", {})
    if not isinstance(charges, dict):
        return None
    entry = charges.get(EHI_CHARGE_KEY) or charges.get("PAYLATER") or charges.get("PREPAY")
    if not isinstance(entry, dict):
        return None
    amount = entry.get("total_price_view", {}).get("amount")
    try:
        return float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return None


def _ehi_vehicle_image(cc: dict) -> str:
    """Extract a real vehicle photo URL from an EHI car_class dict, if present."""
    path = cc.get("images", {}).get("ThreeQuarter", {}).get("path", "")
    if not path:
        return ""
    return path.replace("{width}", "320").replace("{quality}", "high")


def _ehi_extract_best_from_raw(car_classes: list) -> tuple:
    """
    Like _ehi_extract_best(), but reads raw EHI car_class dicts directly
    (code/name/charges) rather than the pre-flattened {name, total} shape the
    old JS-side extraction produced.

    Returns (price, class_name, vehicle_name, image_url). class_name (e.g.
    "Full Size SUV") is used for is_fullsize_suv() matching; vehicle_name
    (e.g. "Ford Expedition") is the actual car the provider will give you —
    what's shown to the user, since that's what genuinely differs between
    providers within the "same" booked class.
    """
    best_price: Optional[float] = None
    best_class_name = ""
    best_vehicle_name = ""
    best_image = ""
    for cc in car_classes:
        class_name = cc.get("name") or _EHI_CODE_NAMES.get(cc.get("code", ""), "")
        price = _ehi_class_price(cc)
        if price is None or price <= 0:
            continue
        if is_fullsize_suv(class_name):
            if best_price is None or price < best_price:
                best_price = price
                best_class_name = class_name
                best_vehicle_name = cc.get("make_model_or_similar_text") or class_name
                best_image = _ehi_vehicle_image(cc)
    return best_price, best_class_name, best_vehicle_name, best_image


def _ehi_enterprise_body(loc_cfg: Dict) -> dict:
    """Request body for enterprise-ewt/reservations/initiate (Enterprise only)."""
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
    return {
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


def _ehi_gma_body(loc_id: str) -> dict:
    """Request body for gma-national / gma-alamo /reservations/initiate."""
    return {
        "pickup_location":      {"id": loc_id},
        "return_location":      {"id": loc_id},
        "pickup_location_id":   loc_id,
        "return_location_id":   loc_id,
        "pickup_time":          f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}",
        "return_time":          f"{BOOKING['return_date']}T{BOOKING['return_time']}",
        "renter_age":           BOOKING["driver_age"],
        "rate_type":            EHI_CHARGE_KEY,
        "country_of_residence": "US",
        "locale":               "en_US",
        "cor":                  "US",
    }


# Headers required by ALL three EHI reservations/initiate endpoints. National's
# API silently 422s ("Invalid Request") without these — they weren't part of
# the old gma-national implementation, which is what caused that endpoint to
# appear broken. Enterprise/Alamo also expect them.
def _ehi_headers(brand: str) -> dict:
    return {
        "content-type": "application/json",
        "accept":       "application/json, text/plain, */*",
        "brand":        brand,
        "channel":      "WEB",
        "locale":       "en_US",
        "page_type":    "home",
        "sofresh":      "SOCLEAN",
        "User-Agent":   USER_AGENT,
    }


def _twelve_hour(time_str: str) -> str:
    """'14:00' -> '2%3A00+PM' (URL-encoded 12-hour time) for EHI deep-link fragments."""
    hh, mm = time_str.split(":")
    hh_int = int(hh)
    ampm = "AM" if hh_int < 12 else "PM"
    hh12 = hh_int % 12 or 12
    return f"{hh12}%3A{mm}+{ampm}"


def _ehi_booking_url(domain: str, airport: str) -> str:
    """
    Deep link into the provider's own search results (prepopulated with this
    booking's location/dates), not just the homepage. These sites are SPAs
    using hash-based routing — the server serves find-a-vehicle.html for any
    request to that path; the client-side router reads the #/vehicles fragment.
    """
    pu = BOOKING["pickup_date"].split("-")   # ["YYYY","MM","DD"]
    re_ = BOOKING["return_date"].split("-")
    pu_mmddyyyy = f"{pu[1]}%2F{pu[2]}%2F{pu[0]}"
    re_mmddyyyy = f"{re_[1]}%2F{re_[2]}%2F{re_[0]}"
    fragment = (
        f"#/vehicles?from={airport}&to={airport}"
        f"&pickup={pu_mmddyyyy}+{_twelve_hour(BOOKING['pickup_time'])}"
        f"&return={re_mmddyyyy}+{_twelve_hour(BOOKING['return_time'])}"
    )
    return f"https://{domain}/en/reservation/find-a-vehicle.html{fragment}"


async def _ehi_check(provider: str, brand: str, api_url: str, body: dict, domain: str) -> Dict:
    """
    Shared implementation for check_enterprise/check_national/check_alamo.
    Plain HTTP POST, no browser, no proxy — all three EHI endpoints accept
    calls straight from the server (confirmed on the production VPS).
    """
    if not should_check_provider(provider):
        return make_result(provider, na=True, error="Not in providers_to_check")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(api_url, json=body, headers=_ehi_headers(brand))
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return make_result(provider, error=f"API error: {str(exc)[:150]}")

    car_classes = _ehi_parse_car_classes(data)
    if not car_classes:
        msgs = data.get("messages", []) if isinstance(data, dict) else []
        return make_result(provider, error=f"No car classes returned — messages={msgs[:2]}")

    print(f"  [{provider}] {len(car_classes)} classes returned")
    best_price, class_name, vehicle_name, image_url = _ehi_extract_best_from_raw(car_classes)
    if best_price is None:
        codes = [c.get("code") for c in car_classes]
        return make_result(provider, error=f"No Full Size SUV — codes seen: {codes[:10]}")

    print(f"  [{provider}] Best: {vehicle_name} @ ${best_price:.2f}")
    return make_result(
        provider,
        car_class=class_name or "Full Size SUV",
        model=vehicle_name,
        price=best_price,
        url=_ehi_booking_url(domain, BOOKING["airport_code"]),
        image_url=image_url,
    )


async def check_enterprise() -> Dict:
    airport = BOOKING["airport_code"]
    loc_cfg = EH_LOCATION_CONFIG.get(airport)
    if not loc_cfg:
        return make_result("Enterprise", na=True, error=f"No EHI location for {airport}")
    return await _ehi_check(
        "Enterprise", "ENTERPRISE",
        f"{EH_API_BASE}/reservations/initiate",
        _ehi_enterprise_body(loc_cfg),
        "www.enterprise.com",
    )


async def check_national() -> Dict:
    airport = BOOKING["airport_code"]
    loc_cfg = EH_LOCATION_CONFIG.get(airport)
    if not loc_cfg:
        return make_result("National", na=True, error=f"No EHI location for {airport}")
    return await _ehi_check(
        "National", "NATIONAL",
        "https://prd-east.webapi.nationalcar.com/gma-national/reservations/initiate",
        _ehi_gma_body(loc_cfg["national_id"]),
        "www.nationalcar.com",
    )


async def check_alamo() -> Dict:
    airport = BOOKING["airport_code"]
    loc_cfg = EH_LOCATION_CONFIG.get(airport)
    if not loc_cfg:
        return make_result("Alamo", na=True, error=f"No EHI location for {airport}")
    return await _ehi_check(
        "Alamo", "ALAMO",
        "https://prd-east.webapi.alamo.com/gma-alamo/reservations/initiate",
        _ehi_gma_body(loc_cfg["alamo_id"]),
        "www.alamo.com",
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
        # Prefer the actual vehicle (e.g. "Chevrolet Tahoe") over Hertz's generic
        # class label ("Medium 7 Passenger SUV") — that's what genuinely differs
        # between providers within the "same" booked class.
        name = v.get("make_model") or v.get("vehicle_display_name") or v.get("sipp_code") or "Full Size SUV"
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


# ─────────────────────────────────────────────────────────────────────────────
# LOCATION PRICE CACHE — skip consistently overpriced nearby locations
# ─────────────────────────────────────────────────────────────────────────────
# Persisted to location_price_cache.json next to this file.  Checked before
# each nearby-location fetch.  If the last known price is > SKIP_THRESHOLD × the
# booked price we skip the check (it has never been competitive).
#
# Threshold 1.5×: a location at $900 when booked price is $600 → skipped.
# Always checks first time (no cache entry) and whenever price is competitive.
# ─────────────────────────────────────────────────────────────────────────────

_LOCATION_PRICE_CACHE_FILE = Path(__file__).parent / "location_price_cache.json"
_NEARBY_SKIP_THRESHOLD = 1.5   # skip if last_price > booked_price × threshold


def _load_location_price_cache() -> Dict:
    """Load the persisted price cache from disk."""
    try:
        if _LOCATION_PRICE_CACHE_FILE.exists():
            return json.loads(_LOCATION_PRICE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def should_check_nearby_location(location_key: str, booked_price: float) -> bool:
    """
    Return True if the nearby location should be checked this run.
    False only when cache shows the last price was > 1.5× the booked price,
    meaning it has never been competitive.
    """
    cache = _load_location_price_cache()
    if location_key not in cache:
        return True  # never seen before — always check
    last_price = cache[location_key].get("last_price")
    if last_price is None:
        return True
    if last_price > booked_price * _NEARBY_SKIP_THRESHOLD:
        print(f"  [NearbyCache] Skipping {location_key} — "
              f"last price ${last_price:.2f} > {_NEARBY_SKIP_THRESHOLD}× "
              f"booked ${booked_price:.2f}")
        return False
    return True


def update_location_price_cache(location_key: str, price: float) -> None:
    """Persist the latest price for a nearby location to disk."""
    try:
        cache = _load_location_price_cache()
        cache[location_key] = {
            "last_price":    price,
            "last_checked":  datetime.now().isoformat(timespec="seconds"),
        }
        _LOCATION_PRICE_CACHE_FILE.write_text(
            json.dumps(cache, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"  [NearbyCache] Failed to write cache: {exc!s:.60}")


async def _fetch_one_hertz_station(
    station: str,
    location_key: str,
    display_name: str = "",
) -> tuple:
    """
    Fetch Hertz vehicle-rates for a single station via direct API (no browser).
    Returns (location_key, best_price_or_None).

    Token is shared from _hertz_get_token() cache — one OAuth fetch for all stations.
    """
    label     = display_name or location_key
    pickup_dt = f"{BOOKING['pickup_date']}T{BOOKING['pickup_time']}:00"
    return_dt = f"{BOOKING['return_date']}T{BOOKING['return_time']}:00"
    age       = int(BOOKING["driver_age"])

    # Skip consistently overpriced locations
    if not should_check_nearby_location(location_key, BOOKING["booked_price"]):
        return location_key, None

    print(f"  [NearbyHertz/{label}] Direct API — station={station}")
    try:
        vehicle_data = await asyncio.to_thread(
            _hertz_direct_rates, station, pickup_dt, return_dt, age, "HERTZ"
        )
        best_price, best_name = _hertz_extract_best(vehicle_data)
        if best_price:
            print(f"  [NearbyHertz/{label}] Best: {best_name} @ ${best_price:.2f}")
            update_location_price_cache(location_key, best_price)
            return location_key, best_price
        else:
            sipp_list = [v.get("sipp_code") for v in vehicle_data]
            print(f"  [NearbyHertz/{label}] No Full Size SUV found "
                  f"(sipp codes seen: {sipp_list[:6]})")
            return location_key, None
    except Exception as exc:
        print(f"  [NearbyHertz/{label}] Error: {exc!s:.100}")
        return location_key, None


async def fetch_nearby_hertz_prices(
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch Hertz Full Size SUV prices at the closest nearby locations (airports
    and city branches) — one lightweight HTTP call per station, run in
    parallel via asyncio.gather.

    Args:
        nearby_locations : output of discover_nearby_locations() — reads "Hertz" sub-list.
    Returns:
        {location_key: cheapest_price_float}
    """
    hertz_locs = nearby_locations.get("Hertz", [])
    if not hertz_locs:
        print("  [NearbyHertz] No Hertz nearby locations to fetch.")
        return {}

    tasks = [
        _fetch_one_hertz_station(
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
    nearby_locations: Dict[str, List[Dict]],
) -> Dict[str, float]:
    """
    Fetch Enterprise Full Size SUV prices at the closest nearby locations
    (airports and city branches) — one lightweight HTTP POST per location,
    no browser, no proxy needed.

    Args:
        nearby_locations : output of discover_nearby_locations() — reads "EHI" sub-list.
    Returns:
        {location_key: cheapest_enterprise_price_float}
    """
    ehi_locs = nearby_locations.get("EHI", [])
    if not ehi_locs:
        print("  [NearbyEHI] No EHI nearby locations to fetch.")
        return {}

    api_url = f"{EH_API_BASE}/reservations/initiate"
    prices: Dict[str, float] = {}

    async def _fetch_one(loc: Dict, client: "httpx.AsyncClient") -> None:
        lkey = loc["location_key"]
        gbid = loc["group_branch_id"]
        loc_id = loc["location_id"]

        db_entry = next(
            (e for e in (_LOCATIONS_DB_CACHE or [])
             if e.get("provider") == "EHI" and e.get("group_branch_id") == gbid),
            None,
        )
        if not db_entry:
            print(f"  [NearbyEHI/{lkey}] DB entry not found for {gbid} — skipping")
            return

        loc_cfg = {
            "id":              loc_id,
            "airport_code":    loc["airport_code"],
            "gps":             db_entry.get("gps") or {
                "latitude":  db_entry.get("lat", 0),
                "longitude": db_entry.get("lng", 0),
            },
            "name":            db_entry.get("name", lkey),
            "country_code":    db_entry.get("country_code", "US"),
            "group_branch_id": gbid,
            "time_zone_id":    db_entry.get("time_zone_id", "America/New_York"),
        }
        try:
            resp = await client.post(
                api_url, json=_ehi_enterprise_body(loc_cfg), headers=_ehi_headers("ENTERPRISE"),
            )
            resp.raise_for_status()
            car_classes = _ehi_parse_car_classes(resp.json())
        except Exception as exc:
            print(f"  [NearbyEHI/{lkey}] Error: {str(exc)[:120]}")
            return

        best_price, _class_name, vehicle_name, _img = _ehi_extract_best_from_raw(car_classes)
        if best_price:
            prices[lkey] = best_price
            print(f"  [NearbyEHI/{lkey}] Best: {vehicle_name} @ ${best_price:.2f}")
        else:
            codes = [c.get("code") for c in car_classes]
            print(f"  [NearbyEHI/{lkey}] No Full Size SUV (codes: {codes[:8]})")

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*[_fetch_one(loc, client) for loc in ehi_locs])

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
# PROVIDER DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER_FUNCS = {
    "SIXT":       check_sixt,
    "Hertz":      check_hertz,
    "National":   check_national,
    "Enterprise": check_enterprise,
    "Alamo":      check_alamo,
    "Dollar":     check_dollar,
    "Thrifty":    check_thrifty,
}


async def _check_provider_once(provider: str) -> Dict:
    func = PROVIDER_FUNCS.get(provider)
    if func is None:
        return make_result(provider, error="No implementation for this provider")
    try:
        return await asyncio.wait_for(func(), timeout=60.0)
    except asyncio.TimeoutError:
        return make_result(provider, error="Timed out after 60s")
    except Exception as exc:
        return make_result(provider, error=str(exc)[:150])


async def check_provider(provider: str) -> Dict:
    """
    Run a single provider check, retrying once on failure.

    These are undocumented third-party APIs (WAF challenges, occasional 403s)
    — a single transient failure shouldn't drop a provider from the results
    when a second attempt a moment later usually succeeds. na=True results
    ("Thrifty has no station here") are legitimate answers, not failures,
    and are never retried.
    """
    result = await _check_provider_once(provider)
    if result.get("error") and not result.get("na"):
        await asyncio.sleep(2.0)
        print(f"  [{provider}] Retrying after: {result['error'][:80]}")
        result = await _check_provider_once(provider)
    return result


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
        filter_parts.append(f"prefer {pay_label}")
    filter_str = " | ".join(filter_parts) if filter_parts else "none"

    print(f"\n{sep}")
    print(f"PRICE MONITOR — {airport}  {pu_date} → {re_date}  ({days} days)")
    print(f"Your booking : {BOOKING['provider']} {BOOKING['car_class']}  ${booked:.2f}")
    print(f"Filters      : {filter_str}")
    print(sep)
    print(f"{'Provider':<14} {'Class':<20} {'Model':<24} {'Price':>8}   {'vs Booked':>10}")
    print(col_sep)

    best_saving:   Optional[float] = None
    best_provider: Optional[str]   = None

    for r in results:
        provider = r["provider"]

        if r.get("error"):
            err_short = r["error"].replace("\n", " ").replace("\r", "")[:55]
            status = "N/A" if r.get("na") else "ERROR"
            print(f"{provider:<14} {status:<20} {err_short}")
            continue

        price     = r.get("price")
        car_class = (r.get("car_class") or "Full Size SUV")[:18]
        model     = (r.get("model")     or "")[:22]

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

        print(f"{provider:<14} {car_class:<20} {model:<24} {price_str}   {saving_str}")

    print(sep)
    if best_provider and best_saving is not None:
        winner_price = next(r["price"] for r in results if r["provider"] == best_provider)
        print(f"BEST DEAL ► {best_provider} — ${winner_price:.2f}   save ${best_saving:.2f} vs your booking")
    else:
        print("No provider found a cheaper Full Size SUV than your booked price.")
    print(f"{sep}\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def _format_results_section(results: List[Dict], booked_price: float) -> List[Dict]:
    """Convert raw provider result dicts into the standardised JSON results section."""
    out = []
    for r in results:
        price = r.get("price")
        saving = round(booked_price - price, 2) if price is not None else None
        if r.get("error"):
            status = "na" if r.get("na") else "error"
        else:
            status = "ok"
        out.append({
            "provider":    r.get("provider"),
            "car_class":   r.get("car_class") or "",
            "model":       r.get("model") or "",
            "price":       round(price, 2) if price is not None else None,
            "saving":      saving,
            "source":      "direct",
            "status":      status,
            "booking_url": r.get("url") or None,
            "image_url":   r.get("image_url") or None,
        })
    return out


def send_price_alert(booking: dict, best_result: dict, nearby_deals: list) -> None:
    try:
        import resend
    except ImportError:
        print("resend not installed — skipping email alert")
        return

    resend.api_key = os.environ.get("RESEND_API_KEY")
    if not resend.api_key:
        print("RESEND_API_KEY not set — skipping email alert")
        return

    alert_email = os.environ.get("ALERT_EMAIL")
    if not alert_email:
        print("ALERT_EMAIL not set — skipping email alert")
        return

    provider    = booking.get("provider", "")
    airport     = booking.get("airport_code", "")
    pickup      = booking.get("pickup_date", "")
    return_date = booking.get("return_date", "")
    booked_price = booking.get("booked_price", 0)

    deals_html = ""
    if best_result:
        saving = booked_price - best_result["price"]
        deals_html = f"""
        <tr>
            <td style="padding:8px">{best_result['provider']}</td>
            <td style="padding:8px">{best_result.get('car_class','')}</td>
            <td style="padding:8px">${best_result['price']:.2f}</td>
            <td style="padding:8px;color:#22c55e;font-weight:bold">Save ${saving:.2f}</td>
            <td style="padding:8px">{best_result.get('source','')}</td>
        </tr>"""

    nearby_html = ""
    for deal in nearby_deals[:3]:
        nearby_html += f"""
        <tr>
            <td style="padding:8px">{deal['location_name']}</td>
            <td style="padding:8px">${deal['best_price']:.2f}</td>
            <td style="padding:8px">{deal.get('best_provider','')}</td>
            <td style="padding:8px;color:#22c55e;font-weight:bold">Net save ${deal['net_saving']:.2f}</td>
        </tr>"""

    nearby_section = ""
    if nearby_html:
        nearby_section = f"""
        <h3>Nearby location deals:</h3>
        <table style="width:100%;border-collapse:collapse">
            <tr style="background:#f3f4f6">
                <th style="padding:8px;text-align:left">Location</th>
                <th style="padding:8px;text-align:left">Price</th>
                <th style="padding:8px;text-align:left">Provider</th>
                <th style="padding:8px;text-align:left">Net Saving</th>
            </tr>
            {nearby_html}
        </table>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
        <h2 style="color:#22c55e">&#x1F4B0; Price Drop Alert</h2>
        <p>A better deal was found for your <strong>{provider}</strong> booking at <strong>{airport}</strong></p>
        <p>&#x1F4C5; {pickup} &rarr; {return_date} &nbsp;|&nbsp; Your price: <strong>${booked_price:.2f}</strong></p>

        <h3>Better prices found:</h3>
        <table style="width:100%;border-collapse:collapse">
            <tr style="background:#f3f4f6">
                <th style="padding:8px;text-align:left">Provider</th>
                <th style="padding:8px;text-align:left">Class</th>
                <th style="padding:8px;text-align:left">Price</th>
                <th style="padding:8px;text-align:left">Saving</th>
                <th style="padding:8px;text-align:left">Source</th>
            </tr>
            {deals_html}
        </table>

        {nearby_section}

        <br>
        <p style="color:#6b7280;font-size:12px">
            You can cancel your current booking and rebook at the better price.<br>
            Monitored by CardVault Price Tracker
        </p>
    </div>"""

    best_saving = booked_price - best_result["price"] if best_result else 0
    subject = (
        f"\U0001F4B0 Price drop for your {provider} booking at {airport}"
        f" — save up to ${best_saving:.2f}"
    )

    try:
        resend.Emails.send({
            "from": "CardVault <onboarding@resend.dev>",
            "to": [alert_email],
            "subject": subject,
            "html": html,
        })
        print(f"Alert email sent to {alert_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def _write_json_results(
    results: List[Dict],
    nearby_rows: List[Dict],
    total_seconds: float,
    alternative_classes: Optional[List[Dict]] = None,
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
    results_section = _format_results_section(results, booked)

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
    direct = [r for r in results_section if r["price"] is not None]

    def _best(lst):
        if not lst:
            return None, None, None
        b = max(lst, key=lambda x: (x["saving"] or -9999))
        return b["provider"], b["price"], b["saving"]

    bd_prov, bd_price, bd_save = _best(direct)

    summary_section = {
        "best_direct_provider": bd_prov,
        "best_direct_price":    round(bd_price, 2) if bd_price is not None else None,
        "best_direct_saving":   round(bd_save, 2)  if bd_save  is not None else None,
    }

    # ── assemble and write ───────────────────────────────────────────────────
    payload = {
        "booking":            booking_section,
        "last_checked":       datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "runtime_seconds":    round(total_seconds, 1),
        "results":            results_section,
        "nearby":             nearby_section,
        "summary":            summary_section,
        "alternative_classes": alternative_classes or [],
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
        "providers_to_check": b.get("providers_to_check") or None,
        "additional_classes": b.get("additional_classes") or None,
    }


def save_results_to_supabase(booking_id: str, results_data: dict) -> None:
    try:
        sb = get_supabase()
        sb.table("price_results").insert({
            "booking_id":          booking_id,
            "checked_at":          datetime.now().isoformat(),
            "runtime_seconds":     results_data.get("runtime_seconds"),
            "results":             results_data.get("results"),
            "nearby":              results_data.get("nearby"),
            "summary":             results_data.get("summary"),
            "alternative_classes": results_data.get("alternative_classes") or [],
        }).execute()
        print(f"Results saved to Supabase for booking {booking_id}")
    except Exception as e:
        print(f"Failed to save to Supabase: {e}")


def _reinitialize_location_constants() -> None:
    """Re-run all module-level location lookups after BOOKING has been updated."""
    global SIXT_LOCATION
    global HERTZ_STATION_CODE
    global DOLLAR_STATION_CODE, THRIFTY_STATION_CODE
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

    # Dollar / Thrifty
    _dollar_loc  = _db_lookup("Dollar",  airport)
    _thrifty_loc = _db_lookup("Thrifty", airport)
    DOLLAR_STATION_CODE  = _dollar_loc["station_code"]  if _dollar_loc  else None
    THRIFTY_STATION_CODE = _thrifty_loc["station_code"] if _thrifty_loc else None

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
        f"EHI={'✓' if EH_LOCATION_CONFIG else '✗'}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ALTERNATIVE CLASS CHECK
# ─────────────────────────────────────────────────────────────────────────────

async def _check_providers_for_class(acriss: str) -> List[Dict]:
    """
    Run all provider checks for a single ACRISS class, independent of the main run.
    Sets ACTIVE_CAR_CLASS, runs all providers concurrently, then restores state.
    Returns a plain list of raw result dicts in PROVIDERS order.
    """
    global ACTIVE_CAR_CLASS

    old_class = ACTIVE_CAR_CLASS
    ACTIVE_CAR_CLASS = acriss

    alt_map: Dict[str, Dict] = {}

    async def _run(provider: str) -> None:
        alt_map[provider] = await check_provider(provider)

    try:
        await asyncio.gather(*[_run(p) for p in PROVIDERS])
    finally:
        ACTIVE_CAR_CLASS = old_class

    return [alt_map[p] for p in PROVIDERS if p in alt_map]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    # ── Supabase / CLI argument handling ────────────────────────────────────
    parser = argparse.ArgumentParser(description="Car rental price monitor")
    parser.add_argument("--booking-id", type=str, default=None,
                        help="Supabase booking UUID — loads booking from Supabase instead of hardcoded BOOKING dict")
    args, _unknown = parser.parse_known_args()   # _unknown absorbs unrecognised flags

    booking_id: Optional[str] = args.booking_id

    if booking_id:
        print(f"Loading booking {booking_id} from Supabase...")
        booking_data = load_booking_from_supabase(booking_id)
        BOOKING.update(booking_data)
        _reinitialize_location_constants()
    # ─────────────────────────────────────────────────────────────────────────

    t_wall = time.monotonic()

    _pay   = BOOKING.get("payment_type") or "any"
    _fc    = "yes" if BOOKING.get("free_cancellation") else "no"
    _seats = BOOKING.get("min_passengers") or "any"
    _um    = "yes" if BOOKING.get("unlimited_mileage") else "no"
    print(f"\n>>  Price Monitor — {len(PROVIDERS)} providers | "
          f"{BOOKING['airport_code']} | "
          f"{BOOKING['pickup_date']} → {BOOKING['return_date']}")
    print(f"   Reference : {BOOKING['provider']} {BOOKING['car_class']} "
          f"@ ${BOOKING['booked_price']:.2f}")
    print(f"   Pref      : payment={_pay}  free_cancel={_fc}  seats≥{_seats}  unlimited_miles={_um}\n")

    # results_map: provider → (result_dict, elapsed_seconds)
    results_map: Dict[str, tuple] = {}

    # ── Helper: run one provider check and store result ────────────────
    async def _timed_check(provider: str) -> None:
        t0     = time.monotonic()
        result = await check_provider(provider)
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

    # ── Phase 1: all providers, in parallel — every check is a plain HTTP call ──
    print(f"Phase 1 — {len(PROVIDERS)} providers  [parallel, no browser]")
    await asyncio.gather(*[_timed_check(p) for p in PROVIDERS])
    print()

    # ── Additional class checks ───────────────────────────────────────────
    alternative_classes: List[Dict] = []
    extra_acriss = BOOKING.get("additional_classes") or []
    for acriss in extra_acriss:
        cls_info = CAR_CLASS_EQUIVALENTS.get(acriss)
        if not cls_info:
            print(f"  [alt-class] {acriss} not in CAR_CLASS_EQUIVALENTS — skipping")
            continue
        label = cls_info["name"]
        print(f"\nAlternative class — {acriss} ({label})")
        alt_results = await _check_providers_for_class(acriss)
        alternative_classes.append({
            "acriss":   acriss,
            "label":    label,
            "results":  _format_results_section(alt_results, BOOKING["booked_price"]),
        })
        print(f"  [{acriss}] done — {sum(1 for r in alt_results if r.get('price') is not None)} prices found")
    if alternative_classes:
        print()

    # ── Phase 2: nearby airport price lookups (SIXT + Hertz + EHI) ────────
    # Prices are merged into nearby_prices, taking min per airport.
    # nearby_sources tracks which provider gave the best (cheapest) price.
    nearby_prices:  Dict[str, float] = {}
    nearby_sources: Dict[str, str]   = {}
    phase2_tasks   = []
    phase2_labels  = []
    phase2_sources = []   # human-readable source name per task, same order as tasks
    if nearby_locations.get("Hertz"):
        n = len(nearby_locations["Hertz"])
        phase2_tasks.append(fetch_nearby_hertz_prices(nearby_locations))
        phase2_labels.append(f"Hertz/{n}")
        phase2_sources.append("Hertz")
    if nearby_locations.get("EHI"):
        n = len(nearby_locations["EHI"])
        phase2_tasks.append(fetch_nearby_ehi_prices(nearby_locations))
        phase2_labels.append(f"EHI/{n}")
        phase2_sources.append("EHI")
    if nearby_locations.get("SIXT"):
        n = len(nearby_locations["SIXT"])
        phase2_tasks.append(fetch_nearby_sixt_prices(nearby_locations))
        phase2_labels.append(f"SIXT/{n} via API")
        phase2_sources.append("SIXT")

    if phase2_tasks:
        print(f"Phase 2 — Nearby airport prices  [{' | '.join(phase2_labels)}]")
        phase2_results = await asyncio.gather(*phase2_tasks, return_exceptions=True)

        # Collect all per-provider results for the debug table
        all_provider_prices: Dict[str, Dict[str, float]] = {}  # {src: {code: price}}
        for pr, src in zip(phase2_results, phase2_sources):
            if isinstance(pr, Exception):
                print(f"  [Phase2] {src} raised an exception: {pr}")
                continue
            if isinstance(pr, dict):
                all_provider_prices[src] = pr
                for code, price in pr.items():
                    if code not in nearby_prices or price < nearby_prices[code]:
                        nearby_prices[code] = price
                        nearby_sources[code] = src

        # Per-provider price table — shows SIXT, Hertz, EHI separately per location
        all_codes = sorted({c for prices in all_provider_prices.values() for c in prices})
        if all_codes:
            src_cols = list(all_provider_prices.keys())
            header = f"  {'Location':<32}" + "".join(f"  {s:<12}" for s in src_cols) + "  Best"
            print(f"\n  [Phase2] Nearby airport prices by provider:")
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

        print(f"  Phase 2 done — {len(nearby_prices)} locations priced")
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
    results_data = _write_json_results(results, nearby_rows, total, alternative_classes)

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

    # ── Email alert ──────────────────────────────────────────────────────────
    savings = [
        r for r in results_data.get("results", [])
        if r.get("status") == "ok"
        and r.get("saving") is not None
        and r["saving"] >= MIN_SAVING
    ]
    nearby_deals = [
        n for n in results_data.get("nearby", [])
        if n.get("is_deal") and n.get("net_saving") is not None
    ]
    if savings or nearby_deals:
        best = min(savings, key=lambda x: x["price"]) if savings else None
        send_price_alert(BOOKING, best, nearby_deals)


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
    price found across ALL providers (Hertz, EHI, SIXT) for each location.

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


if __name__ == "__main__":
    asyncio.run(main())
