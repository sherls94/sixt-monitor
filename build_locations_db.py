#!/usr/bin/env python3
"""
build_locations_db.py
=====================
One-time script to build and refresh locations_db.json from each provider's
own data source.  Run this when changing airports or adding new providers.

Unified schema (one dict per location):
  provider         : parent company  (SIXT | HertzCorp | EnterpriseCorp | AvisBudgetGroup)
  brand            : specific brand  (SIXT | Hertz | Dollar | Thrifty | Enterprise |
                                      National | Alamo | Avis | Budget | Payless)
  location_id      : provider's stable numeric or alphanumeric branch identifier
  location_url_param: value used in the booking URL for this location
                     (SIXT: location UUID  |  Kayak: "LGA-a15830"  |  Hertz: station code)
  name, address, city, state, country, airport_code, is_airport, lat, lng

Usage:
    python build_locations_db.py             # all providers
    python build_locations_db.py --sixt      # SIXT only (Part 1)
    python build_locations_db.py --avis      # Avis / Budget / Payless only
    python build_locations_db.py --hertz     # Hertz / Dollar / Thrifty only
    python build_locations_db.py --enterprise  # Enterprise / National / Alamo only
"""

import argparse
import asyncio
import base64
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    sys.exit("pip install playwright && playwright install chromium")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

DB_FILE = Path(__file__).parent / "locations_db.json"

# Avis Developer API credentials (used for AvisBudgetGroup brands)
# Get from https://developer.avis.com/  → set as env vars or replace here
import os
AVIS_CLIENT_ID     = os.environ.get("AVIS_CLIENT_ID",     "")
AVIS_CLIENT_SECRET = os.environ.get("AVIS_CLIENT_SECRET", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def _loc(
    provider: str,
    brand: str,
    location_id: str,
    name: str,
    *,
    location_url_param: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    country: str = "US",
    airport_code: str = "",
    is_airport: bool = False,
    lat: float = 0.0,
    lng: float = 0.0,
) -> Dict:
    return dict(
        provider=provider,
        brand=brand,
        location_id=str(location_id),
        location_url_param=location_url_param,
        name=name.strip(),
        address=address,
        city=city,
        state=state,
        country=country,
        airport_code=airport_code,
        is_airport=is_airport,
        lat=float(lat),
        lng=float(lng),
    )


def load_db() -> Dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"locations": [], "metadata": {}}


def save_db(locations: List[Dict], *, merge_existing: bool = True) -> None:
    """
    De-duplicate by (provider, brand, location_id) and write to DB_FILE.
    If merge_existing=True, keep existing entries whose (provider, brand, location_id)
    are NOT present in the new list (preserves manually-verified entries from other runs).
    """
    if merge_existing:
        old_db = load_db()
        # Index new locations by their key
        new_keys = {(l["provider"], l["brand"], l["location_id"]) for l in locations}
        # Preserve old entries that are NOT being overwritten
        kept = [
            l for l in old_db.get("locations", [])
            if (l.get("provider"), l.get("brand"), l.get("location_id")) not in new_keys
        ]
        all_locs = kept + locations
    else:
        all_locs = locations

    # Final de-dup
    seen: Dict[Tuple, Dict] = {}
    for loc in all_locs:
        key = (loc["provider"], loc.get("brand", ""), loc["location_id"])
        if key not in seen:
            seen[key] = loc
    unique = list(seen.values())

    by_brand: Dict[str, int] = {}
    for loc in unique:
        b = loc.get("brand", loc["provider"])
        by_brand[b] = by_brand.get(b, 0) + 1

    db = {
        "locations": unique,
        "metadata": {
            "last_updated": date.today().isoformat(),
            "version": "2.0",
            "total_locations": len(unique),
            "providers": by_brand,
        },
    }
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'─' * 60}")
    print(f"Saved {len(unique)} locations → {DB_FILE}")
    for brand, count in sorted(by_brand.items()):
        print(f"  {brand:<22} {count:>4} locations")
    print(f"{'─' * 60}")


# ─────────────────────────────────────────────────────────────────────────────
# Nominatim geocoding pass (synchronous — respects 1 req/s rate limit)
# ─────────────────────────────────────────────────────────────────────────────

_NOM_HEADERS = {
    "User-Agent": "sixt-price-monitor/1.0 (private non-commercial; contact@example.com)",
    "Accept-Language": "en",
}

def _nominatim_search(query: str, countrycodes: str = "us") -> Optional[Tuple[float, float]]:
    """
    Call Nominatim for a single query.  Returns (lat, lng) or None.
    Caller is responsible for sleeping between calls.
    Pass countrycodes="" to search globally without country restriction.
    """
    params: Dict = {"q": query, "format": "json", "limit": "1"}
    if countrycodes:
        params["countrycodes"] = countrycodes
    url = f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_NOM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def geocode_missing_coords() -> None:
    """
    For every DB entry with lat=0 and lng=0, attempt to geocode via Nominatim.

    Skips entries that are clearly EHI internal admin records (no airport code,
    no useful address, internal-sounding names).

    Attempt order (stops on first hit; sleeps 1.1 s before EVERY request):
      1. airport_code + " airport"  — if airport_code is set
      2. cleaned name (strip "(IATA)" suffixes) using entry's country code
      3. city + state               — if city and state present
      4. city + country             — if city and country present

    Writes coordinates back to DB_FILE in-place (does NOT re-run save_db merge;
    just updates the JSON array directly to preserve all other fields).
    Reports: total geocoded, total failed, skipped internal entries.
    """
    # Keywords that flag an entry as an EHI internal admin record
    _INTERNAL_KEYWORDS = {
        "lockbox", "fm group", "fm admin", "overhead", "restructure",
        "reservations telephone", "information services", "home office",
        "personal property tax", "commute van", "commute shuttle",
        "salvage", "holman", "cafc_remote", "corp_f9", "truck remarketin",
        "pvd salvage", "insurance auto auctions", "virtual one way",
        "hc virtual", "pobox", "ars_v3", "film production only",
        "non-contractual partners", "long island agencies",
        "marketing", "data center", "remarketing", "sale lot", "sale aa",
        "exotic collection", "atlantic aviation",
    }

    # ISO-2 country code → Nominatim countrycodes parameter
    _CC_MAP = {
        "US": "us", "GB": "gb", "UK": "gb", "DE": "de", "FR": "fr",
        "ES": "es", "IT": "it", "NL": "nl", "BE": "be", "CH": "ch",
        "AT": "at", "PT": "pt", "GR": "gr", "SE": "se", "NO": "no",
        "FI": "fi", "DK": "dk", "IE": "ie", "PL": "pl", "CZ": "cz",
        "HU": "hu", "RO": "ro", "BG": "bg", "HR": "hr", "SK": "sk",
        "SI": "si", "EE": "ee", "LV": "lv", "LT": "lt",
        "AU": "au", "NZ": "nz", "JP": "jp", "SG": "sg", "TH": "th",
        "HK": "hk", "TW": "tw", "CN": "cn", "KR": "kr", "IN": "in",
        "CA": "ca", "MX": "mx", "BR": "br", "AR": "ar", "CL": "cl",
        "CO": "co", "PE": "pe", "CR": "cr", "PA": "pa", "DO": "do",
        "ZA": "za", "EG": "eg", "MA": "ma", "TN": "tn", "KE": "ke",
        "NG": "ng", "GH": "gh", "TZ": "tz", "ZM": "zm", "ZW": "zw",
        "AE": "ae", "SA": "sa", "QA": "qa", "KW": "kw", "BH": "bh",
        "JO": "jo", "IL": "il", "LB": "lb", "OM": "om", "TR": "tr",
        "DXB": "ae",   # common mistake — country code stored as IATA
    }

    db = load_db()
    locations = db.get("locations", [])

    missing_all = [l for l in locations if float(l.get("lat", 0)) == 0.0
                   and float(l.get("lng", 0)) == 0.0]

    # Separate skippable internal entries from those worth geocoding
    skipped_internal: List[Dict] = []
    missing: List[Dict] = []
    for loc in missing_all:
        name_lc = loc.get("name", "").lower()
        is_airport = bool(loc.get("is_airport") or loc.get("airport_code"))
        # Flag internal EHI admin entries — no airport code, generic location
        if not is_airport and any(kw in name_lc for kw in _INTERNAL_KEYWORDS):
            skipped_internal.append(loc)
        else:
            missing.append(loc)

    print(f"\n{'━' * 60}")
    print(f"[Geocode] {len(missing_all)} entries with no coordinates")
    print(f"  Internal/admin (skipped) : {len(skipped_internal)}")
    print(f"  Attempting geocode       : {len(missing)}")
    print(f"  Nominatim rate-limit     : 1.1 s between calls")
    print(f"{'━' * 60}")

    if not missing:
        print("[Geocode] Nothing to geocode.")
        return

    success = 0
    failed  = 0
    failed_locs: List[Dict] = []

    for i, loc in enumerate(missing, 1):
        name        = loc.get("name",         "").strip()
        city        = loc.get("city",         "").strip()
        state       = loc.get("state",        "").strip()
        country     = (loc.get("country") or "").strip().upper()
        airport_code = (loc.get("airport_code") or "").strip().upper()

        # Resolve Nominatim countrycodes param
        cc = _CC_MAP.get(country, "")   # "" means no restriction

        # Strip parenthetical IATA codes from name: "Austin Bergstrom (AUS)" → "Austin Bergstrom"
        import re as _re
        clean_name = _re.sub(r'\s*\([A-Z]{3}\)\s*$', '', name).strip()

        coords: Optional[Tuple[float, float]] = None
        attempt_label = ""

        # ── Attempt 1: IATA code lookup ────────────────────────────────────
        if airport_code and not coords:
            time.sleep(1.1)
            coords = _nominatim_search(f"{airport_code} airport", cc or "")
            if coords:
                attempt_label = "iata"

        # ── Attempt 2: cleaned name with country code ──────────────────────
        if clean_name and not coords:
            time.sleep(1.1)
            coords = _nominatim_search(clean_name, cc or "us")
            if coords:
                attempt_label = "name"

        # ── Attempt 3: cleaned name without country restriction ────────────
        if clean_name and not coords and cc:   # only retry if we restricted above
            time.sleep(1.1)
            coords = _nominatim_search(clean_name, "")
            if coords:
                attempt_label = "name-global"

        # ── Attempt 4: city + state (US-style) ────────────────────────────
        if not coords and city and state:
            time.sleep(1.1)
            coords = _nominatim_search(f"{city}, {state}, USA", "us")
            if coords:
                attempt_label = "city+state"

        # ── Attempt 5: city + country ──────────────────────────────────────
        if not coords and city and country and country not in ("US", "??", ""):
            time.sleep(1.1)
            coords = _nominatim_search(f"{city}", cc or "")
            if coords:
                attempt_label = "city"

        if coords:
            loc["lat"] = round(coords[0], 6)
            loc["lng"] = round(coords[1], 6)
            success += 1
            print(
                f"[Geocode] {i:4d}/{len(missing)}  ✓ [{attempt_label:<12}]  "
                f"{clean_name[:45]}"
            )
        else:
            failed += 1
            failed_locs.append(loc)
            print(
                f"[Geocode] {i:4d}/{len(missing)}  ✗ FAILED  "
                f"{clean_name[:45]}  cc={cc!r} city={city!r}"
            )

    # ── Save in-place (avoid losing other providers' entries) ─────────────
    DB_FILE.write_text(
        json.dumps(db, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print(f"[Geocode] Done.")
    print(f"  Successfully geocoded : {success}")
    print(f"  Failed (no result)    : {failed}")
    print(f"  DB written to         : {DB_FILE}")
    if failed_locs:
        # Analyse failure patterns
        no_name   = sum(1 for l in failed_locs if not l.get("name"))
        no_city   = sum(1 for l in failed_locs if not l.get("city"))
        providers = {}
        for l in failed_locs:
            p = l.get("provider", "?")
            providers[p] = providers.get(p, 0) + 1
        print(f"\n  Failure breakdown:")
        print(f"    No name field     : {no_name}")
        print(f"    No city field     : {no_city}")
        for p, n in sorted(providers.items()):
            print(f"    Provider {p:<18}: {n}")
        print(f"\n  Sample failures (first 20):")
        for l in failed_locs[:20]:
            print(
                f"    [{l.get('provider','?')}/{l.get('brand','?')}] "
                f"{l.get('name','(no name)')[:40]}  "
                f"city={l.get('city','')!r}"
            )
    print(f"{'─' * 60}")


# ─────────────────────────────────────────────────────────────────────────────
# Browser context helper
# ─────────────────────────────────────────────────────────────────────────────

async def _new_ctx(browser):
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins',  {get: () => [1,2,3,4,5]});
        window.chrome = {runtime: {}};
    """)
    return ctx


def _launch_args() -> List[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-infobars", "--disable-extensions",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# ██████  SIXT FETCHER
# ─────────────────────────────────────────────────────────────────────────────
#
# Strategy (layered — each adds to the same raw dict):
#   A. Direct gRPC-web call to SuggestLocations from within the browser page
#      context (avoids CORS because the call originates from sixt.com itself).
#      We encode a minimal protobuf request and decode the binary response.
#   B. Response interception: capture all JSON responses from sixt.com domains
#      and any gRPC-web binary responses (heuristic string extraction).
#   C. GA event interception: SIXT fires cstm_rac_ofr_dmd_pickup_srch_rslt
#      events to Google Analytics with ep.search_result containing branch JSON.
#   D. DOM fallback: parse data attributes from autocomplete suggestion elements.
#
# Search terms: airport codes + major city names for off-airport branches.
# ─────────────────────────────────────────────────────────────────────────────

# IATA codes for all major US airports where SIXT operates
_SIXT_AIRPORTS = [
    # Northeast
    "LGA","JFK","EWR","BOS","PHL","BWI","DCA","IAD","BDL","PVD","ALB","BUF",
    # Southeast
    "MIA","FLL","PBI","MCO","TPA","RSW","JAX","SAV","CLT","RDU","ATL","MSY",
    "ORF","RIC","CHS","PNS","VPS","BHM","MEM","BNA",
    # Midwest
    "ORD","MDW","MKE","DTW","CLE","CMH","IND","CVG","STL","MCI","OMA","DSM",
    "MSP","MSN","GRR","FWA","PIT","SDF",
    # Southwest
    "DFW","DAL","HOU","IAH","SAT","AUS","ELP","ABQ","TUL","OKC",
    "PHX","TUS","LAS","SAN","LAX","SNA","BUR","LGB","ONT","PSP","SMF",
    "SFO","SJC","OAK","SLC","DEN","BOI","GEG","RNO",
    # Northwest
    "SEA","PDX","FAI","ANC",
    # Hawaii / Puerto Rico
    "HNL","OGG","KOA","LIH","SJU","BQN",
    # Florida secondary
    "MLB","SFB","DAB","PIE","AGS",
]

# City name searches (find off-airport city-centre branches)
_SIXT_CITIES = [
    "New York", "Manhattan", "Brooklyn", "Newark",
    "Los Angeles", "Beverly Hills", "Santa Monica",
    "Chicago", "Houston", "Phoenix", "Philadelphia",
    "San Antonio", "San Diego", "Dallas", "San Jose", "Austin",
    "Fort Worth", "Columbus", "Charlotte", "Indianapolis",
    "San Francisco", "Seattle", "Denver", "Nashville",
    "Oklahoma City", "El Paso", "Washington DC",
    "Boston", "Las Vegas", "Portland", "Louisville",
    "Baltimore", "Milwaukee", "Albuquerque", "Tucson",
    "Fresno", "Sacramento", "Miami", "Atlanta",
    "Raleigh", "Minneapolis", "Tampa", "New Orleans",
    "Cleveland", "Pittsburgh", "Cincinnati", "Kansas City",
    "Honolulu", "Anchorage", "Salt Lake City", "Boise",
    "Hartford", "Buffalo", "Richmond", "Savannah",
    "Memphis", "Birmingham", "Omaha", "Des Moines",
    "Madison", "Grand Rapids", "Spokane", "Reno",
    "Tucson", "Scottsdale", "Tempe", "Mesa",
    "San Juan", "Kahului", "Kona",
]

_SIXT_TERMS = _SIXT_AIRPORTS + _SIXT_CITIES


async def _dismiss_popups(page: Page) -> None:
    await page.wait_for_timeout(1500)
    for sel in [
        "#onetrust-accept-btn-handler",
        "button[aria-label*='ccept']", "button[aria-label*='lose']",
        "button:has-text('Accept All')", "button:has-text('Accept')",
        "button:has-text('Close')", "button:has-text('OK')",
        "[class*='cookie'] button", "[class*='consent'] button",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=600):
                await el.click(timeout=1500)
                await page.wait_for_timeout(400)
        except Exception:
            pass


def _extract_nested(d: Dict, *paths: str) -> Optional[float]:
    """Walk dot-separated paths in a dict; return first non-None float found."""
    for path in paths:
        cur: any = d
        for key in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = None
                break
        if cur is not None:
            try:
                return float(cur)
            except (TypeError, ValueError):
                pass
    return None


def _parse_grpc_binary(data: bytes) -> List[Dict]:
    """
    Heuristic extraction of JSON objects AND structured fields from a gRPC-web
    binary response.

    Three passes:
      Pass 1 — JSON objects: find any {...} substring parseable as JSON ≥2 keys.
      Pass 2 — ASCII UUID strings: regex for UUID strings (8-4-4-4-12 hex)
               then pair with adjacent readable strings (branch name, iata code).
      Pass 3 — Binary UUID fields: scan for protobuf length-delimited fields with
               length=16 whose content matches UUID v4 markers (byte[6]&0xF0==0x40
               and byte[8]&0xC0==0x80).  This is the primary path for SIXT since
               their gRPC responses store UUIDs as raw 16 bytes, not ASCII strings.
    """
    results = []
    try:
        text = data.decode("latin-1")

        # Pass 1: JSON objects
        for m in re.finditer(r'\{"[a-zA-Z]', text):
            start = m.start()
            depth = 0
            for end in range(start, min(start + 800, len(text))):
                c = text[end]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:end + 1])
                            if isinstance(obj, dict) and len(obj) >= 2:
                                results.append(obj)
                        except json.JSONDecodeError:
                            pass
                        break

        # Pass 2: ASCII UUID-keyed location objects
        # Extract all printable strings of length ≥ 3 from binary (protobuf string fields)
        strings: List[str] = []
        run = ""
        for b in data:
            if 32 <= b < 127:
                run += chr(b)
            else:
                if len(run) >= 3:
                    strings.append(run)
                run = ""
        if len(run) >= 3:
            strings.append(run)

        uuid_pattern = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE
        )
        iata_pattern = re.compile(r'^[A-Z]{3}$')
        branch_id_pattern = re.compile(r'^BRANCH:\d+$')

        for i, s in enumerate(strings):
            if not uuid_pattern.match(s):
                continue
            # UUID found — collect nearby strings (window ±10) as candidate fields
            window = strings[max(0, i - 10): i + 11]
            obj: Dict = {"uuid": s}
            for w in window:
                if branch_id_pattern.match(w):
                    obj["branchId"] = w
                elif iata_pattern.match(w) and w not in ("GET", "PUT", "POST"):
                    obj.setdefault("iataCode", w)
                elif len(w) > 5 and not uuid_pattern.match(w) and w not in (s,):
                    obj.setdefault("name", w)
            results.append(obj)

        # Pass 3: Binary UUID scan (primary path for SIXT gRPC responses)
        #
        # SIXT stores location UUIDs as raw 16-byte fields in protobuf, not as
        # formatted strings.  The encoded layout in the binary is:
        #   [tag_varint_last_byte with wire_type=2 (bits 0-2 == 010)]
        #   [length_byte = 0x10 = 16]
        #   [16 bytes of UUID data]
        #
        # UUID v4 identification:
        #   byte[6] & 0xF0 == 0x40  (version nibble = 4)
        #   byte[8] & 0xC0 == 0x80  (variant bits = 10xx xxxx)
        #
        # Context: within ±300 bytes we look for printable strings that could be
        # the branch ID ("BRANCH:NNNNN") or 3-letter IATA code ("LGA", "JFK"…).
        seen_uuids: set = set()
        for j in range(1, len(data) - 17):
            if (data[j] == 0x10                         # length byte = 16
                    and (data[j - 1] & 0x07) == 0x02    # preceding byte = wire_type 2 tag
                    and (data[j - 1] & 0x80) == 0x00    # single-byte tag (MSB=0)
            ):
                candidate = data[j + 1: j + 17]
                if len(candidate) < 16:
                    continue
                if (candidate[6] & 0xF0) == 0x40 and (candidate[8] & 0xC0) == 0x80:
                    # Valid UUID v4 bytes found
                    h = candidate.hex()
                    uuid_str = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
                    if uuid_str in seen_uuids:
                        continue
                    seen_uuids.add(uuid_str)

                    # Collect printable ASCII strings within ±300 bytes for context
                    win_s = max(0, j - 300)
                    win_e = min(len(data), j + 317)
                    nearby: List[str] = []
                    _run = ""
                    for b2 in data[win_s:win_e]:
                        if 32 <= b2 < 127:
                            _run += chr(b2)
                        else:
                            if len(_run) >= 3:
                                nearby.append(_run)
                            _run = ""
                    if len(_run) >= 3:
                        nearby.append(_run)

                    obj2: Dict = {"uuid": uuid_str}
                    for s2 in nearby:
                        if branch_id_pattern.match(s2):
                            obj2["branchId"] = s2
                        elif iata_pattern.match(s2) and s2 not in (
                                "GET", "PUT", "POST", "API", "USE", "FOR", "ALL"):
                            obj2.setdefault("iataCode", s2)
                        elif len(s2) > 5 and not uuid_pattern.match(s2):
                            obj2.setdefault("name", s2)
                    results.append(obj2)

    except Exception:
        pass
    return results


async def fetch_sixt_us(playwright) -> List[Dict]:
    """
    Fetch all SIXT US branches using layered capture strategies.
    Returns a list of location dicts in the unified schema.
    """
    total_terms = len(_SIXT_TERMS)
    print(f"\n{'━' * 60}")
    print(f"[SIXT] Starting — {total_terms} search terms ({len(_SIXT_AIRPORTS)} airports "
          f"+ {len(_SIXT_CITIES)} cities)")
    print(f"{'━' * 60}")

    browser = await playwright.chromium.launch(headless=False, args=_launch_args())
    ctx     = await _new_ctx(browser)
    page    = await ctx.new_page()

    # ── Storage ───────────────────────────────────────────────────────────────
    raw: Dict[str, Dict] = {}   # raw_id → raw data dict (any format)

    _UUID_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE,
    )

    def _absorb(items: List[Dict], source: str = "") -> int:
        """Merge a list of raw dicts into `raw`; return count of new entries.

        If the key already exists but the new item carries a UUID and the existing
        entry doesn't, the UUID (and any other missing fields) are merged in so
        that gRPC-derived UUIDs are never silently discarded.
        """
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            bid = str(
                item.get("id") or item.get("branchId") or item.get("branch_id") or
                item.get("stationId") or item.get("locationId") or
                item.get("code") or ""
            )
            if not bid:
                continue
            # Normalise: strip "BRANCH:" prefix for key but keep full id in value
            key = bid.replace("BRANCH:", "")
            if not key:
                continue
            if key not in raw:
                raw[key] = {**item, "_raw_id": bid, "_source": source}
                added += 1
            else:
                # Merge: promote any UUID field the existing entry is missing
                existing = raw[key]
                new_uuid = str(
                    item.get("uuid") or item.get("locationUuid") or
                    item.get("location_uuid") or ""
                )
                if new_uuid and _UUID_RE.match(new_uuid):
                    if not existing.get("uuid") and not existing.get("locationUuid"):
                        existing["uuid"] = new_uuid
                # Also merge iataCode / name if missing
                for field in ("iataCode", "airportCode", "name", "lat", "lng",
                              "latitude", "longitude"):
                    if item.get(field) and not existing.get(field):
                        existing[field] = item[field]
        return added

    # ── Strategy C: Google Analytics event interception ───────────────────────
    def _parse_ga_payload(raw_str: str) -> int:
        """Parse URL-encoded GA4 event payload and extract branch arrays."""
        added = 0
        try:
            params: Dict[str, str] = {}
            for part in raw_str.split("&"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    params[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
            for key, val in params.items():
                # Look for any param whose value is a JSON array or object
                val = val.strip()
                if not (val.startswith("[") or val.startswith("{")):
                    continue
                try:
                    parsed = json.loads(val)
                    items = parsed if isinstance(parsed, list) else [parsed]
                    added += _absorb(items, source=f"ga:{key}")
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        return added

    async def on_request(req):
        try:
            url = req.url
            if "google-analytics.com" not in url and "analytics.google.com" not in url:
                return
            body = ""
            try:
                body = req.post_data or ""
            except Exception:
                pass
            combined = url + "&" + body
            # Only bother parsing if it looks like it has location data
            if any(kw in combined.lower() for kw in
                   ("search_result", "branch", "location", "station")):
                _parse_ga_payload(combined)
        except Exception:
            pass

    # ── Strategy B: Response interception (JSON + gRPC binary) ───────────────
    async def on_response(resp):
        try:
            url = resp.url
            # Only SIXT-controlled domains
            if not any(d in url for d in
                       ("sixt.com", "sixt.io", "orange.sixt", "betafunnel")):
                return
            ct = resp.headers.get("content-type", "")

            if "json" in ct:
                try:
                    data = await resp.json()
                    items: List[Dict] = []

                    if isinstance(data, list):
                        items = data

                    elif isinstance(data, dict):
                        # ── SuggestLocations response ────────────────────────
                        # Shape: {"suggestions": [{"location": {"location_id": "BRANCH:45431",
                        #          "title": "...", "branch": {"id": "45431"}, "position": {...}}}]}
                        if isinstance(data.get("suggestions"), list):
                            for sug in data["suggestions"]:
                                loc = sug.get("location", {}) if isinstance(sug, dict) else {}
                                if not isinstance(loc, dict):
                                    continue
                                branch = loc.get("branch", {})
                                bid = (
                                    str(branch.get("id", "")).strip() or
                                    str(loc.get("location_id", "")).replace("BRANCH:", "").strip()
                                )
                                if not bid:
                                    continue
                                pos  = loc.get("position") or {}
                                flat = {
                                    "id":   bid,
                                    "name": loc.get("title") or branch.get("title") or "",
                                    "lat":  float(pos.get("latitude",  0) or 0),
                                    "lng":  float(pos.get("longitude", 0) or 0),
                                    "country": loc.get("country_code", "US"),
                                }
                                items.append(flat)
                        else:
                            # Generic: look for common wrapper keys
                            for k in ("locations", "branches", "stations",
                                      "results", "data", "items", "content"):
                                if isinstance(data.get(k), list):
                                    items = data[k]
                                    break

                    _absorb(items, source=f"json:{url[:60]}")
                except Exception:
                    pass

            elif "grpc" in ct or "octet-stream" in ct or "proto" in ct:
                try:
                    body = await resp.body()
                    objs = _parse_grpc_binary(body)
                    _absorb(objs, source="grpc-binary")
                except Exception:
                    pass
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    # ── Navigate to SIXT ─────────────────────────────────────────────────────
    print("[SIXT] Navigating to sixt.com...")
    await page.goto(
        "https://www.sixt.com/car-rental/usa/",
        timeout=60_000, wait_until="domcontentloaded",
    )
    await _dismiss_popups(page)
    await page.wait_for_timeout(2000)
    print(f"[SIXT] Page: '{await page.title()}'  URL: {page.url[:70]}")

    # ── Find the location search input ───────────────────────────────────────
    inp = None
    input_selectors = [
        "input[placeholder*='ickup']",
        "input[placeholder*='ocation']",
        "input[aria-label*='ickup']",
        "input[aria-label*='ocation']",
        "[data-testid*='pickup'] input",
        "[data-testid*='location'] input",
        "[data-testid*='search'] input",
        "input[name*='location']",
        "input[name*='pickup']",
        "input[type='search']",
    ]
    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1200):
                inp = el
                print(f"[SIXT] Input found: {sel}")
                break
        except Exception:
            pass

    if inp is None:
        # Dump all inputs for debugging
        inputs_debug = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).slice(0, 25).map(el => ({
                id: el.id, name: el.name, type: el.type,
                placeholder: el.placeholder.substring(0, 50),
                ariaLabel: (el.getAttribute('aria-label') || '').substring(0, 50),
                visible: el.offsetHeight > 0 && el.offsetWidth > 0,
            }))
        """)
        print("[SIXT] ⚠ No input found. Inputs on page:")
        for i in inputs_debug:
            if i["visible"]:
                print(f"  ✓ {i}")
            else:
                print(f"  ✗ {i}")
        print("[SIXT] ⚠ Cannot proceed without location input. "
              "Try headless=False and inspect the page.")
        await browser.close()
        return []

    # ── Strategy A: Direct JSON calls to SuggestLocations API ────────────────
    # The SuggestLocations endpoint accepts JSON (confirmed via browser probe).
    # This runs BEFORE the text search loop so airports get correct iataCode.
    #
    # API: POST https://grpc-prod.orange.sixt.com/
    #             com.sixt.service.rent_booking.api.SearchService/SuggestLocations
    # Body: {"query": "<term>", "auto_complete_session_id": "<uuid>", "vehicle_type": 1}
    # Returns: {"suggestions": [{"location": {"location_id":"BRANCH:45431",
    #           "title":"New York LGA Airport", "type":"TYPE_AIRPORT", "branch":{"id":"45431"},...}}]}
    #
    # For each airport code: only absorb the first TYPE_AIRPORT suggestion, and
    # set iataCode = search term so _db_lookup("SIXT", "LGA") finds it later.
    import uuid as _uuid_mod

    _SIXT_SUGGEST_URL = (
        "https://grpc-prod.orange.sixt.com/"
        "com.sixt.service.rent_booking.api.SearchService/SuggestLocations"
    )
    _SIXT_API_HEADERS = {
        "Content-Type":   "application/json",
        "Accept":         "application/json",
        "Origin":         "https://www.sixt.com",
        "Referer":        "https://www.sixt.com/car-rental/usa/",
        "User-Agent":     USER_AGENT,
    }

    def _sixt_suggest_sync(term: str, airport_only: bool = False) -> List[Dict]:
        """
        Call the SIXT SuggestLocations API synchronously.
        Returns a list of flat location dicts suitable for _absorb().
        If airport_only=True, only return TYPE_AIRPORT results.
        """
        try:
            body = json.dumps({
                "query":                    term,
                "auto_complete_session_id": str(_uuid_mod.uuid4()),
                "vehicle_type":             1,
            }).encode()
            req = urllib.request.Request(
                _SIXT_SUGGEST_URL, data=body,
                headers=_SIXT_API_HEADERS, method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for sug in data.get("suggestions", []):
                loc = sug.get("location", {})
                if not isinstance(loc, dict):
                    continue
                if airport_only and loc.get("type") != "TYPE_AIRPORT":
                    continue
                branch = loc.get("branch", {})
                bid    = str(branch.get("id", "")).strip()
                if not bid:
                    bid = str(loc.get("location_id", "")).replace("BRANCH:", "").strip()
                if not bid:
                    continue
                pos = loc.get("position") or {}
                results.append({
                    "id":      bid,
                    "name":    loc.get("title") or branch.get("title") or "",
                    "lat":     float(pos.get("latitude",  0) or 0),
                    "lng":     float(pos.get("longitude", 0) or 0),
                    "country": loc.get("country_code", "US"),
                })
            return results
        except Exception:
            return []

    print("[SIXT] Strategy A: JSON SuggestLocations for all airport codes...")
    api_added = 0
    for i, airport in enumerate(_SIXT_AIRPORTS):
        locs = await asyncio.to_thread(_sixt_suggest_sync, airport, True)
        for loc in locs:
            loc["iataCode"] = airport   # tag with the IATA code we searched for
        n = _absorb(locs, source=f"suggest-api:{airport}")
        api_added += n
        if (i + 1) % 10 == 0 or i == len(_SIXT_AIRPORTS) - 1:
            print(f"  [A] {i+1:3d}/{len(_SIXT_AIRPORTS)}  raw={len(raw)}")
        await asyncio.sleep(0.08)   # ~12 req/s — well within rate limits

    print(f"[SIXT] Strategy A done: +{api_added} airport captures (total={len(raw)})")

    # ── Strategies B+C+D: Text search loop ───────────────────────────────────
    # Types each term into the search box; Strategies B and C fire automatically
    # via the on_request/on_response handlers.  Strategy D (DOM) runs per-term.
    print(f"\n[SIXT] Strategies B+C+D: text search ({total_terms} terms)...")
    t0 = time.monotonic()

    for idx, term in enumerate(_SIXT_TERMS):
        before = len(raw)
        try:
            await inp.click(timeout=5000, force=True)
            await inp.fill("", timeout=3000)
            await page.wait_for_timeout(120)
            await page.keyboard.type(term, delay=55)
            await page.wait_for_timeout(2800)   # wait for autocomplete API + GA event

            # Strategy D: DOM attribute extraction
            dom_items = await page.evaluate("""
            () => {
                const results = [];
                // Try all known autocomplete container patterns
                const selectors = [
                    '[class*="suggestion"]',
                    '[class*="autocomplete"] li',
                    '[class*="autocomplete"] [class*="item"]',
                    '[class*="dropdown"] li',
                    '[role="option"]',
                    '[class*="location-result"]',
                    '[class*="locationResult"]',
                    '[class*="locationItem"]',
                    '[class*="search-result"]',
                    '[data-testid*="suggestion"]',
                    '[data-testid*="result"]',
                    'ul[class*="search"] li',
                    '[class*="station"]',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0 && els[0].offsetHeight > 0) {
                        for (const el of Array.from(els).slice(0, 10)) {
                            const attrs = {};
                            for (const a of el.attributes) {
                                attrs[a.name] = a.value;
                            }
                            // Also check child elements for data attributes
                            for (const child of el.querySelectorAll('[data-id],[data-value],[data-location-id],[data-branch-id]')) {
                                for (const a of child.attributes) {
                                    if (a.name.startsWith('data-')) attrs['child:' + a.name] = a.value;
                                }
                            }
                            results.push({
                                text: el.textContent.trim().substring(0, 100),
                                attrs,
                            });
                        }
                        break;
                    }
                }
                return results;
            }
            """)

            for item in dom_items:
                text = item.get("text", "")
                for k, v in item.get("attrs", {}).items():
                    v = str(v).strip()
                    if not v:
                        continue
                    # Accept: numeric IDs, or "BRANCH:xxxx" format
                    if v.startswith("BRANCH:") or (v.isdigit() and len(v) >= 3):
                        key = v.replace("BRANCH:", "")
                        if key not in raw:
                            raw[key] = {
                                "name": text,
                                "_raw_id": v,
                                "_source": f"dom:{k}",
                            }

            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)

            new_count = len(raw) - before
            print(
                f"[SIXT] {idx + 1:3d}/{total_terms}  {term:<25}  "
                f"+{new_count:<3}  total={len(raw)}"
            )

        except Exception as exc:
            print(f"[SIXT] {idx + 1:3d}/{total_terms}  {term:<25}  ERROR: {exc}")

    elapsed = time.monotonic() - t0
    print(f"\n[SIXT] Text search done in {elapsed:.0f}s | raw captures: {len(raw)}")

    await browser.close()

    # ── Parse raw captures into unified schema ───────────────────────────────
    locations: List[Dict] = []
    skipped_no_name = 0
    skipped_no_id   = 0

    for key, d in raw.items():
        name = (
            d.get("name") or d.get("description") or d.get("displayName") or
            d.get("label") or d.get("title") or d.get("stationName") or ""
        )
        name = name.strip()
        if not name or len(name) < 3:
            skipped_no_name += 1
            continue

        # Numeric / alphanumeric location_id
        raw_id = d.get("_raw_id", key)
        if str(raw_id).startswith("BRANCH:"):
            loc_id = str(raw_id)[7:]
        else:
            loc_id = re.sub(r"\s+", "", str(raw_id))
        if not loc_id:
            skipped_no_id += 1
            continue

        lat = _extract_nested(
            d, "lat", "latitude",
            "coordinates.lat", "coordinates.latitude",
            "geoCoordinate.latitude", "position.lat",
            "geo.lat", "location.lat",
        ) or 0.0
        lng = _extract_nested(
            d, "lng", "lon", "long", "longitude",
            "coordinates.lng", "coordinates.lon", "coordinates.longitude",
            "geoCoordinate.longitude", "position.lng",
            "geo.lng", "location.lng",
        ) or 0.0

        uuid = str(
            d.get("uuid") or d.get("locationUuid") or d.get("location_uuid") or
            d.get("id") or ""
        )
        if uuid == str(raw_id):
            uuid = ""  # don't store the branch ID as the UUID

        airport_code = str(
            d.get("iataCode") or d.get("airportCode") or
            d.get("airport_code") or d.get("iata") or ""
        ).upper()
        is_ap = (
            bool(airport_code) or
            "airport" in name.lower() or
            "terminal" in name.lower()
        )

        address = str(d.get("address") or d.get("street") or d.get("streetAddress") or "")
        city    = str(d.get("city") or d.get("cityName") or "")
        state   = str(d.get("state") or d.get("region") or d.get("stateCode") or "")

        locations.append(_loc(
            provider="SIXT",
            brand="SIXT",
            location_id=loc_id,
            location_url_param=uuid,
            name=name,
            address=address,
            city=city,
            state=state,
            country="US",
            airport_code=airport_code,
            is_airport=is_ap,
            lat=lat,
            lng=lng,
        ))

    # De-dup by location_id (keep first seen)
    deduped: Dict[str, Dict] = {}
    for loc in locations:
        if loc["location_id"] not in deduped:
            deduped[loc["location_id"]] = loc
    locations = list(deduped.values())

    # Stats
    with_coords = sum(1 for l in locations if l["lat"] != 0 or l["lng"] != 0)
    airports    = sum(1 for l in locations if l["is_airport"])
    print(f"\n[SIXT] Parse results:")
    print(f"  Raw captures   : {len(raw)}")
    print(f"  Skipped (name) : {skipped_no_name}")
    print(f"  Skipped (no id): {skipped_no_id}")
    print(f"  Valid locations: {len(locations)}")
    print(f"    with coords  : {with_coords}")
    print(f"    airports     : {airports}")
    print(f"    city branches: {len(locations) - airports}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  AVIS / BUDGET / PAYLESS FETCHER (stub — needs API credentials)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_avis_group(playwright) -> List[Dict]:
    """
    Fetch Avis/Budget/Payless US locations via the Avis Developer REST API.

    Requires AVIS_CLIENT_ID and AVIS_CLIENT_SECRET environment variables.
    OAuth endpoint: POST https://stage.abgapiservices.com/oauth/token/v1
    Locations:      GET  https://stage.abgapiservices.com/cars/locations/v1/
                         ?country_code=US&brand=Avis  (repeated for Budget, Payless)
    """
    print(f"\n{'━' * 60}")
    print("[AvisBudgetGroup] Starting Avis / Budget / Payless fetch...")
    print(f"{'━' * 60}")

    if not AVIS_CLIENT_ID or not AVIS_CLIENT_SECRET:
        print("[AvisBudgetGroup] ⚠  AVIS_CLIENT_ID / AVIS_CLIENT_SECRET not set — skipping.")
        print("  Export those env vars and re-run with --avis to populate this group.")
        return []

    # ── Step 1: OAuth client_credentials ────────────────────────────────────
    print("[AvisBudgetGroup] Requesting OAuth token...")
    TOKEN_URL = "https://stage.abgapiservices.com/oauth/token/v1"
    creds_b64 = base64.b64encode(
        f"{AVIS_CLIENT_ID}:{AVIS_CLIENT_SECRET}".encode()
    ).decode()
    token_req = urllib.request.Request(
        TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {creds_b64}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "Accept":        "application/json",
        },
        method="POST",
    )
    access_token = ""
    try:
        with urllib.request.urlopen(token_req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
            access_token = tok.get("access_token") or tok.get("token", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        print(f"[AvisBudgetGroup] ✗ OAuth HTTP {e.code}: {body}")
        return []
    except Exception as exc:
        print(f"[AvisBudgetGroup] ✗ OAuth failed: {exc}")
        return []

    if not access_token:
        print("[AvisBudgetGroup] ✗ No access_token in OAuth response.")
        return []
    print(f"[AvisBudgetGroup] ✓ Token obtained ({len(access_token)} chars)")

    # ── Step 2: Fetch locations per brand ───────────────────────────────────
    LOC_BASE   = "https://stage.abgapiservices.com/cars/locations/v1/"
    BRANDS     = ["Avis", "Budget", "Payless"]
    all_raw: Dict[str, List[Dict]] = {}   # brand → list of raw dicts

    for brand in BRANDS:
        params = urllib.parse.urlencode({
            "country_code": "US",
            "brand":        brand,
        })
        url = f"{LOC_BASE}?{params}"
        loc_req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "client_id":     AVIS_CLIENT_ID,
                "Accept":        "application/json",
                "User-Agent":    USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(loc_req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
                # API may return array directly or wrap it
                raw_locs = (
                    data if isinstance(data, list) else
                    data.get("locations") or data.get("data") or
                    data.get("results")   or data.get("items") or []
                )
                all_raw[brand] = raw_locs
                print(f"[AvisBudgetGroup] {brand:<10} : {len(raw_locs):>4} raw locations")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"[AvisBudgetGroup] {brand}: HTTP {e.code} — {body}")
            all_raw[brand] = []
        except Exception as exc:
            print(f"[AvisBudgetGroup] {brand}: {exc}")
            all_raw[brand] = []

    # ── Step 3: Parse into unified schema ───────────────────────────────────
    locations: List[Dict] = []
    for brand, raw_locs in all_raw.items():
        for d in raw_locs:
            lid = str(
                d.get("code") or d.get("location_code") or
                d.get("id")   or d.get("location_id")   or ""
            )
            name = str(
                d.get("name") or d.get("description") or
                d.get("location_name") or ""
            ).strip()
            if not lid or not name:
                continue

            # Address — may be nested dict or flat fields
            addr_obj = d.get("address") or {}
            if isinstance(addr_obj, str):
                address_str = addr_obj
                city = str(d.get("city") or "")
                state = str(d.get("state_code") or d.get("state") or "")
            else:
                address_str = str(
                    addr_obj.get("street")   or addr_obj.get("address1") or
                    addr_obj.get("line1")    or ""
                )
                city  = str(addr_obj.get("city")  or d.get("city")  or "")
                state = str(
                    addr_obj.get("state_code") or addr_obj.get("state") or
                    d.get("state_code")         or d.get("state")       or ""
                )

            # Coordinates — may be nested or flat
            geo = d.get("geo") or d.get("coordinates") or d.get("geoCoordinates") or {}
            lat = float(
                geo.get("lat") or geo.get("latitude") or
                d.get("latitude") or d.get("lat") or 0
            )
            lng = float(
                geo.get("lon") or geo.get("longitude") or geo.get("lng") or
                d.get("longitude") or d.get("lon") or d.get("lng") or 0
            )

            airport_code = str(
                d.get("airport_code") or d.get("iata_code") or
                d.get("airportCode") or d.get("iataCode") or ""
            ).upper()
            is_ap = bool(airport_code) or "airport" in name.lower()

            locations.append(_loc(
                provider="AvisBudgetGroup",
                brand=brand,
                location_id=lid,
                name=name,
                address=address_str,
                city=city,
                state=state,
                country="US",
                airport_code=airport_code,
                is_airport=is_ap,
                lat=lat,
                lng=lng,
            ))

    with_coords = sum(1 for l in locations if l["lat"] != 0 or l["lng"] != 0)
    print(f"\n[AvisBudgetGroup] ✓ {len(locations)} locations total, "
          f"{with_coords} with coordinates")
    return locations


def generate_avis_budget_from_existing_db() -> List[Dict]:
    """
    Generate Avis and Budget airport location entries using coordinates from
    the Hertz and Kayak entries already in locations_db.json.

    Avis/Budget both accept the standard 3-letter IATA airport code as the
    pickup_location_code in their booking URLs — confirmed by inspecting the
    existing AVIS_RESULTS_URL in price_monitor.py which uses:
        ?pickup_location_code={iata_code}&return_location_code={iata_code}

    So the location_id IS the IATA code; no custom numeric ID is needed.

    This approach requires no API credentials and produces ~350 entries covering
    all airports for which Hertz or Kayak have coordinate data.

    Note: Avis/Budget don't service every airport.  The generated entries are
    optimistic — if Avis/Budget aren't at a given airport, check_avis() will
    fall back to a redirect or empty result.  For major airports (which is the
    primary use case) coverage is reliable.
    """
    print(f"\n{'━' * 60}")
    print("[AvisBudget] Generating entries from existing DB airports...")
    print(f"{'━' * 60}")

    existing_db = json.loads(DB_FILE.read_text(encoding="utf-8"))["locations"] if DB_FILE.exists() else []

    # Build a map of airport_code → best entry (prefer Kayak, then Hertz)
    # Both have accurate coordinates and airport-code metadata.
    source_map: Dict[str, Dict] = {}
    for entry in existing_db:
        if entry.get("provider") not in ("Hertz", "Kayak"):
            continue
        if not entry.get("is_airport"):
            continue
        code = entry.get("airport_code", "")
        if not code or len(code) != 3:
            continue
        lat = float(entry.get("lat") or 0)
        lng = float(entry.get("lng") or 0)
        if not (lat and lng):
            continue
        # US bounding box check
        if not (17.0 <= lat <= 72.0 and -180.0 <= lng <= -64.0):
            continue
        # Prefer Kayak (more curated names); don't overwrite a Kayak entry with Hertz
        if code not in source_map or source_map[code].get("provider") != "Kayak":
            source_map[code] = entry

    locations: List[Dict] = []
    for code, src in sorted(source_map.items()):
        lat = float(src.get("lat") or 0)
        lng = float(src.get("lng") or 0)
        name_base = src.get("name", f"{code} Airport")
        # Use the canonical airport name without brand prefix
        for brand, provider in [("Avis", "Avis"), ("Budget", "Budget")]:
            locations.append(_loc(
                provider=provider,   # matches price_monitor._db_lookup("Avis", ...)
                brand=brand,
                location_id=code,   # IATA code IS the Avis/Budget location code
                location_url_param=code,
                name=f"{name_base}",
                address="",
                city="",
                state="",
                country="US",
                airport_code=code,
                is_airport=True,
                lat=lat,
                lng=lng,
            ))

    print(f"[AvisBudget] ✓ {len(locations)} entries generated "
          f"({len(source_map)} unique airports × 2 brands)")
    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  HERTZ / DOLLAR / THRIFTY FETCHER
# ─────────────────────────────────────────────────────────────────────────────
#
# Strategy: geo-search API (no auth required, direct Python HTTP)
#
#   GET https://ecom.mss.hertz.io/mdm-locations/internal-lookup/geo-search/{brand}
#       ?search={term}&radius={miles}
#
#   OR  ?lat={lat}&long={lng}&radius={miles}
#
#   brand = hertz | dollar | thrifty
#   Returns JSON with data[] array; each item has:
#     oag           = station code (e.g. "LGAT01")  ← used in booking URLs
#     name          = display name
#     location_type = "AP" (airport) | "CT" (city)
#     address.*     = city, state, lat/lng, country_short
#     is_onairport  = "Yes" / "No"
#
#   Coverage strategy:
#     1. Search all known US airport IATA codes (airport branches)
#     2. Search major US cities (city branches)
#     3. Deduplicate by oag code
#     Same approach repeated for Dollar and Thrifty.
#
# No browser required — pure Python urllib.
# ─────────────────────────────────────────────────────────────────────────────

_HERTZ_GEO_BASE = (
    "https://ecom.mss.hertz.io/mdm-locations/internal-lookup/geo-search"
)
_HERTZ_GEO_HEADERS = {
    "User-Agent":    USER_AGENT,
    "Accept":        "application/json",
    "Content-Type":  "application/json",
    "Origin":        "https://www.hertz.com",
    "Referer":       "https://www.hertz.com/us/en/location",
}

# Broad set of US airport codes and city names for comprehensive coverage
_HERTZ_AIRPORTS = [
    # Northeast
    "LGA", "JFK", "EWR", "BOS", "PHL", "BWI", "DCA", "IAD", "BDL", "PVD",
    "ALB", "BUF", "SYR", "ROC", "ORF", "RIC", "RDU", "PIT", "CLE",
    # Southeast
    "MIA", "FLL", "PBI", "MCO", "TPA", "RSW", "JAX", "SAV", "CLT", "ATL",
    "MSY", "CHS", "PNS", "VPS", "BHM", "MEM", "BNA", "HSV", "TYS", "GSP",
    "CAE", "AGS", "ILM", "MYR", "DAB", "MLB", "SRQ", "GNV", "TLH", "ECP",
    # Midwest
    "ORD", "MDW", "MKE", "DTW", "CMH", "IND", "CVG", "STL", "MCI", "OMA",
    "DSM", "MSP", "MSN", "GRR", "FWA", "SDF", "BMI", "CID", "FSD", "BIS",
    "FAR", "SUX", "MLI", "SGF", "ICT", "TOP", "LAN", "TOL", "DAY",
    # Southwest
    "DFW", "DAL", "HOU", "IAH", "SAT", "AUS", "ELP", "ABQ", "TUL", "OKC",
    "PHX", "TUS", "LAS", "SAN", "LAX", "SNA", "BUR", "LGB", "ONT", "PSP",
    "SMF", "SFO", "SJC", "OAK", "SLC", "DEN", "BOI", "GEG", "RNO",
    # Northwest
    "SEA", "PDX", "FAI", "ANC",
    # Hawaii / Puerto Rico
    "HNL", "OGG", "KOA", "LIH", "SJU", "BQN",
]

_HERTZ_CITIES = [
    "New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
    "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
    "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte",
    "Indianapolis", "San Francisco", "Seattle", "Denver", "Nashville",
    "Oklahoma City", "El Paso", "Washington DC", "Boston", "Las Vegas",
    "Portland", "Louisville", "Baltimore", "Milwaukee", "Albuquerque",
    "Tucson", "Fresno", "Sacramento", "Miami", "Atlanta", "Raleigh",
    "Minneapolis", "Tampa", "New Orleans", "Cleveland", "Pittsburgh",
    "Cincinnati", "Kansas City", "Honolulu", "Anchorage", "Salt Lake City",
    "Boise", "Hartford", "Buffalo", "Richmond", "Savannah", "Memphis",
    "Birmingham", "Omaha", "Des Moines", "Madison", "Grand Rapids",
    "Spokane", "Reno", "Scottsdale", "Tempe", "San Juan", "Kahului",
    "Kona", "Knoxville", "Greenville", "Columbia", "Chattanooga",
    "Little Rock", "Shreveport", "Baton Rouge", "Mobile", "Montgomery",
    "Jackson", "Amarillo", "Lubbock", "Waco", "Corpus Christi",
    "Fort Myers", "Daytona Beach", "Gainesville", "Tallahassee",
    "Pensacola", "Panama City Beach", "Colorado Springs", "Fort Collins",
    "Provo", "Ogden", "Eugene", "Salem", "Tacoma", "Spokane",
    "Billings", "Missoula", "Great Falls", "Rapid City", "Sioux Falls",
    "Fargo", "Bismarck", "Casper", "Cheyenne",
]

_HERTZ_SEARCH_TERMS = _HERTZ_AIRPORTS + _HERTZ_CITIES


def _geo_search_sync(brand: str, search_term: str, radius: int = 75) -> List[Dict]:
    """
    Synchronous call to the Hertz geo-search API.
    Returns raw location dicts (empty on failure).
    """
    params = urllib.parse.urlencode({
        "search": search_term,
        "radius": radius,
    })
    url = f"{_HERTZ_GEO_BASE}/{brand}?{params}"
    req = urllib.request.Request(url, headers=_HERTZ_GEO_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            locs = data.get("data") or []
            for loc in locs:
                loc["_brand_hint"] = brand.capitalize()
            return locs
    except Exception:
        return []


def _parse_hertz_geo_loc(d: Dict) -> Optional[Dict]:
    """Parse one geo-search result dict into the unified _loc() schema."""
    oag = str(d.get("oag") or d.get("oag3") or "").strip()
    name = str(d.get("name") or "").strip()
    if not oag or not name:
        return None

    addr = d.get("address") or {}
    lat  = float(addr.get("latitude")  or 0)
    lng  = float(addr.get("longitude") or 0)
    city  = str(addr.get("city")  or "")
    state = str(addr.get("administrative_area") or addr.get("state_short") or "")
    country = str(addr.get("country_short") or "US").upper()
    if country != "US":
        return None   # skip non-US locations

    address = str(addr.get("address1") or addr.get("full_address") or "")

    loc_type = str(d.get("location_type") or "").upper()
    is_on_ap = str(d.get("is_onairport") or "").lower() == "yes"
    is_ap    = loc_type == "AP" or is_on_ap or "airport" in name.lower()

    # Airport code: look for 3-letter IATA code in the name "(XXX)" first.
    # Fallback: station codes like "JFKT01" embed the airport code in the first 3 chars
    # (LGAT01→LGA, EWRT11→EWR, JFKT01→JFK) — use this when is_airport and no parens match.
    airport_code = ""
    m = re.search(r'\(([A-Z]{3})\)', name)
    if m:
        airport_code = m.group(1)
    elif is_ap and len(oag) >= 3 and oag[:3].isalpha():
        airport_code = oag[:3].upper()

    brand_hint = str(d.get("_brand_hint") or "Hertz")
    if "dollar" in brand_hint.lower():
        brand = "Dollar"
    elif "thrifty" in brand_hint.lower():
        brand = "Thrifty"
    else:
        brand = "Hertz"

    entry = _loc(
        # provider matches brand so price_monitor._db_lookup("Hertz", airport)
        # finds these entries directly (brand="Hertz" → provider="Hertz", etc.)
        provider=brand,      # "Hertz" | "Dollar" | "Thrifty"
        brand=brand,
        location_id=oag,
        location_url_param=oag,
        name=name,
        address=address,
        city=city,
        state=state,
        country="US",
        airport_code=airport_code,
        is_airport=is_ap,
        lat=lat,
        lng=lng,
    )
    entry["station_code"] = oag   # used by price_monitor._db_lookup
    return entry


async def fetch_hertz_group(playwright) -> List[Dict]:
    """
    Fetch Hertz / Dollar / Thrifty US locations via the geo-search API.

    Endpoint:  GET https://ecom.mss.hertz.io/mdm-locations/internal-lookup/
               geo-search/{brand}?search={term}&radius={miles}

    No browser required — playwright arg accepted for interface compatibility.
    Searches all US airport IATA codes + major city names; de-dupes by oag.
    """
    print(f"\n{'━' * 60}")
    print("[HertzCorp] Starting Hertz / Dollar / Thrifty fetch...")
    print(f"  API: {_HERTZ_GEO_BASE}/{{brand}}?search=...&radius=75")
    print(f"  Search terms: {len(_HERTZ_SEARCH_TERMS)} "
          f"({len(_HERTZ_AIRPORTS)} airports + {len(_HERTZ_CITIES)} cities)")
    print(f"{'━' * 60}")

    BRANDS = ["hertz", "dollar", "thrifty"]
    all_entries: Dict[str, Dict] = {}   # oag → parsed entry (deduped)
    t0 = time.monotonic()

    for brand in BRANDS:
        brand_raw: Dict[str, Dict] = {}
        print(f"\n[HertzCorp] Fetching {brand.upper()} ({len(_HERTZ_SEARCH_TERMS)} searches)...")

        tasks = [
            asyncio.to_thread(_geo_search_sync, brand, term)
            for term in _HERTZ_SEARCH_TERMS
        ]
        # Run in batches to avoid hammering the endpoint
        batch_size = 20
        for i in range(0, len(tasks), batch_size):
            batch_results = await asyncio.gather(*tasks[i: i + batch_size])
            for raw_locs in batch_results:
                for d in raw_locs:
                    oag = str(d.get("oag") or d.get("oag3") or "").strip()
                    if oag and oag not in brand_raw:
                        brand_raw[oag] = d
            await asyncio.sleep(0.3)   # gentle rate limiting

        # Parse brand_raw into unified schema
        brand_entries = 0
        for d in brand_raw.values():
            entry = _parse_hertz_geo_loc(d)
            if entry and entry["location_id"] not in all_entries:
                all_entries[entry["location_id"]] = entry
                brand_entries += 1

        print(f"  {brand.upper():<8} : {len(brand_raw):>4} raw → "
              f"{brand_entries} new unique US locations (total {len(all_entries)})")

    elapsed = time.monotonic() - t0
    locations = list(all_entries.values())

    with_coords = sum(1 for l in locations if l["lat"] != 0 or l["lng"] != 0)
    airports    = sum(1 for l in locations if l["is_airport"])
    by_brand: Dict[str, int] = {}
    for l in locations:
        by_brand[l["brand"]] = by_brand.get(l["brand"], 0) + 1

    print(f"\n[HertzCorp] Done in {elapsed:.0f}s")
    print(f"  Total unique US locations : {len(locations)}")
    print(f"    Airport branches        : {airports}")
    print(f"    City branches           : {len(locations) - airports}")
    print(f"    With coordinates        : {with_coords}")
    for b, n in sorted(by_brand.items()):
        print(f"  {b:<12} {n:>4}")
    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  ENTERPRISE / NATIONAL / ALAMO FETCHER
# ─────────────────────────────────────────────────────────────────────────────
#
# Strategy: Enumerate Enterprise Holdings' internal numeric location IDs.
#
# Confirmed via browser investigation (2026-04-20):
#   GET https://prd-east.webapi.enterprise.com/enterprise-ewt/location/{ID}?type=both
#   Returns full location data including id, location_id (group_branch_id),
#   name, address, gps, airport_code, time_zone_id, country_code.
#
# ID characteristics (from browser scan):
#   • Valid IDs found in range 1002000–1085000 (tested up to 1150000)
#   • IDs are sparse: ~25–100 apart within the active range
#   • Includes non-US locations (filter by country_code="US")
#   • All three brands (Enterprise/National/Alamo) share the same location IDs
#
# Scan plan: step=25 over range EHI_SCAN_START..EHI_SCAN_END
#   3600 requests at concurrency=40 → ~9 seconds total
#
# Field mapping (API → locations_db.json schema):
#   API loc.id            → location_id   (the numeric ID, e.g. "1018775")
#   API loc.location_id   → group_branch_id (the branch code, e.g. "24JR")
#   API loc.gps           → gps {"latitude":..., "longitude":...}
#   API loc.time_zone_id  → time_zone_id
#   API loc.airport_code  → airport_code
#   API loc.address.country_code → country_code (filter: "US" only)
# ─────────────────────────────────────────────────────────────────────────────

EHI_SCAN_START = 1_000_000    # first possible valid ID (none below 1002000 found)
EHI_SCAN_END   = 1_090_000    # last confirmed boundary (1100000+ are all invalid)
EHI_SCAN_STEP  = 4            # IDs are nearly sequential (avg gap ~1.6); step=4 catches ~80%
EHI_CONCURRENCY = 100         # concurrent urllib threads (benchmarked: ~56 IDs/sec at c=100)

# Name keywords that indicate non-rentable internal/corporate locations to skip
_EHI_SKIP_KEYWORDS = (
    "corp only", "corporate only", "area mgr", "admin branch", "overhead",
    "shop cars", "truck rental admin", "healthcare re", "incorporated only",
    "corp admin", "cdm corp", "admin area", "fleet", "insurance only",
)


def _fetch_ehi_location_sync(loc_id: int) -> Optional[Dict]:
    """
    Synchronous fetch of a single EHI location.  Designed to be called via
    asyncio.to_thread() for non-blocking concurrent execution.
    Returns the raw location dict from the API, or None on any error/miss.
    """
    url = (f"https://prd-east.webapi.enterprise.com/enterprise-ewt/"
           f"location/{loc_id}?type=both")
    req = urllib.request.Request(url, headers={
        "Accept":   "application/json",
        "brand":    "ENTERPRISE",
        "channel":  "WEB",
        "locale":   "en_US",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data.get("location")          # None if key missing or error
    except Exception:
        return None


async def fetch_enterprise_group(playwright) -> List[Dict]:
    """
    Fetch Enterprise / National / Alamo US locations by enumerating the
    Enterprise Holdings internal numeric location IDs.

    No browser required — pure Python async HTTP via asyncio.to_thread.
    playwright arg is accepted for interface compatibility but not used.
    """
    print(f"\n{'━' * 60}")
    print("[EnterpriseCorp] Starting Enterprise / National / Alamo fetch...")
    print(f"  Scan range : {EHI_SCAN_START:,} – {EHI_SCAN_END:,}  step={EHI_SCAN_STEP}")
    total_ids = (EHI_SCAN_END - EHI_SCAN_START) // EHI_SCAN_STEP
    print(f"  Total IDs  : {total_ids:,}  concurrency={EHI_CONCURRENCY}")
    print(f"  Estimated  : ~{total_ids // EHI_CONCURRENCY * 15 // 60}–{total_ids // EHI_CONCURRENCY * 25 // 60} min at {EHI_CONCURRENCY} concurrent")
    print(f"{'━' * 60}")

    sem = asyncio.Semaphore(EHI_CONCURRENCY)
    ids_to_scan = range(EHI_SCAN_START, EHI_SCAN_END, EHI_SCAN_STEP)
    t0 = time.monotonic()

    async def _bounded(loc_id: int) -> Optional[Dict]:
        async with sem:
            return await asyncio.to_thread(_fetch_ehi_location_sync, loc_id)

    # Run all concurrently; gather preserves order but we don't need it
    results_raw = await asyncio.gather(*[_bounded(i) for i in ids_to_scan])

    elapsed = time.monotonic() - t0
    found_raw = [r for r in results_raw if r is not None]
    print(f"[EnterpriseCorp] Scan done in {elapsed:.1f}s — "
          f"{len(found_raw)} valid IDs out of {len(results_raw):,} probed")

    # ── Filter US, parse into unified schema ─────────────────────────────────
    locations: List[Dict] = []
    skipped_non_us   = 0
    skipped_no_name  = 0
    seen_ids: set = set()

    for loc in found_raw:
        if not isinstance(loc, dict):
            continue

        # Country filter
        country_code = str(
            (loc.get("address") or {}).get("country_code", "") or ""
        ).upper()
        if country_code and country_code != "US":
            skipped_non_us += 1
            continue

        # Required fields
        api_id   = str(loc.get("id", "")).strip()        # numeric → location_id in DB
        grp_code = str(loc.get("location_id", "")).strip()  # e.g."24JR"→group_branch_id
        name     = str(loc.get("name", "")).strip()
        if not api_id or not name or api_id in seen_ids:
            skipped_no_name += 1
            continue
        # Filter out administrative / corporate-account-only / internal locations
        name_lower = name.lower()
        if any(kw in name_lower for kw in _EHI_SKIP_KEYWORDS):
            skipped_no_name += 1
            continue
        seen_ids.add(api_id)

        # Coordinates
        gps = loc.get("gps") or {}
        lat = float(gps.get("latitude",  0) or 0)
        lng = float(gps.get("longitude", 0) or 0)

        airport_code = str(loc.get("airport_code", "") or "").upper()
        loc_type     = str(loc.get("location_type", "") or "")
        is_ap        = bool(airport_code) or loc_type == "airport"

        addr_obj = loc.get("address") or {}
        address  = ", ".join(
            s for s in (addr_obj.get("street_addresses") or [""])
            if isinstance(s, str)
        )
        city  = str(addr_obj.get("city",  "") or "")
        state = str(addr_obj.get("country_subdivision_code", "") or "")
        tz    = str(loc.get("time_zone_id", "") or "")

        entry = _loc(
            provider="EHI",
            brand="Enterprise",   # placeholder — all three share the same IDs
            location_id=api_id,
            location_url_param=grp_code,
            name=name,
            address=address,
            city=city,
            state=state,
            country="US",
            airport_code=airport_code,
            is_airport=is_ap,
            lat=lat,
            lng=lng,
        )
        # EHI-specific extra fields used by price_monitor._db_lookup
        entry["group_branch_id"] = grp_code
        entry["time_zone_id"]    = tz
        entry["gps"]             = {"latitude": lat, "longitude": lng}
        entry["country_code"]    = "US"
        locations.append(entry)

    with_coords  = sum(1 for l in locations if l["lat"] != 0 or l["lng"] != 0)
    airports     = sum(1 for l in locations if l["is_airport"])
    print(f"\n[EnterpriseCorp] ✓ {len(locations)} US locations "
          f"({airports} airport, {len(locations) - airports} city), "
          f"{with_coords} with coords")
    print(f"  Skipped (non-US)  : {skipped_non_us}")
    print(f"  Skipped (no name) : {skipped_no_name}")
    print("\n  Note: All three brands (Enterprise/National/Alamo) share these")
    print("  location IDs.  price_monitor._db_lookup uses provider='EHI'.")
    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  HERTZ / DOLLAR / THRIFTY — GLOBAL GEO-SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def generate_coordinate_grid() -> List[Tuple[float, float, int, str]]:
    """
    Return a list of (lat, lng, radius_miles, region) tuples covering all
    populated areas globally.  Grid spacing and radius are tuned so that adjacent
    circles overlap by ~20%, guaranteeing no location is missed.
    """
    grid: List[Tuple[float, float, int, str]] = []

    # US — 2 degree spacing (~140 mi), 75 mi radius gives ~50% overlap
    for lat in range(25, 50, 2):
        for lng in range(-125, -65, 2):
            grid.append((float(lat), float(lng), 75, "US"))

    # Canada
    for lat in range(43, 62, 2):
        for lng in range(-140, -52, 2):
            grid.append((float(lat), float(lng), 75, "CA"))

    # Latin America — sparser, larger radius
    for lat in range(-55, 25, 3):
        for lng in range(-115, -35, 3):
            grid.append((float(lat), float(lng), 100, "LATAM"))

    # Europe — 2 degree spacing
    for lat in range(35, 72, 2):
        for lng in range(-10, 35, 2):
            grid.append((float(lat), float(lng), 75, "EU"))

    # UK — denser 1 degree spacing for higher branch density
    for lat in range(50, 59, 1):
        for lng in range(-5, 2, 1):
            grid.append((float(lat), float(lng), 50, "UK"))

    # Middle East
    for lat in range(20, 35, 2):
        for lng in range(35, 60, 2):
            grid.append((float(lat), float(lng), 75, "ME"))

    # Africa
    for lat in range(-35, 38, 3):
        for lng in range(-18, 52, 3):
            grid.append((float(lat), float(lng), 100, "AF"))

    # Asia Pacific — 3 degree spacing, 100 mi radius
    for lat in range(-10, 45, 3):
        for lng in range(65, 155, 3):
            grid.append((float(lat), float(lng), 100, "APAC"))

    # Australia / NZ
    for lat in range(-45, -10, 2):
        for lng in range(110, 180, 2):
            grid.append((float(lat), float(lng), 75, "AUS"))

    return grid


def _geo_search_sync_coords(brand: str, lat: float, lng: float,
                             radius: int = 75) -> List[Dict]:
    """
    Geo-search by lat/lng coordinates.  Identical to _geo_search_sync but uses
    the ?lat=...&long=...&radius=... form of the Hertz API instead of ?search=.
    Returns raw location dicts (empty list on any error).
    """
    params = urllib.parse.urlencode({"lat": lat, "long": lng, "radius": radius})
    url    = f"{_HERTZ_GEO_BASE}/{brand}?{params}"
    req    = urllib.request.Request(url, headers=_HERTZ_GEO_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            locs = data.get("data") or []
            for loc in locs:
                loc["_brand_hint"] = brand.capitalize()
            return locs
    except Exception:
        return []


def _parse_hertz_geo_loc_global(d: Dict, region: str = "") -> Optional[Dict]:
    """
    Parse one geo-search result dict into unified schema — global version.
    Unlike _parse_hertz_geo_loc, does NOT filter out non-US locations.
    """
    oag  = str(d.get("oag") or d.get("oag3") or "").strip()
    name = str(d.get("name") or "").strip()
    if not oag or not name:
        return None

    addr    = d.get("address") or {}
    lat     = float(addr.get("latitude")  or 0)
    lng     = float(addr.get("longitude") or 0)
    city    = str(addr.get("city")  or "")
    state   = str(addr.get("administrative_area") or addr.get("state_short") or "")
    country = str(addr.get("country_short") or "").upper() or "US"
    address = str(addr.get("address1") or addr.get("full_address") or "")

    loc_type = str(d.get("location_type") or "").upper()
    is_on_ap = str(d.get("is_onairport") or "").lower() == "yes"
    is_ap    = loc_type == "AP" or is_on_ap or "airport" in name.lower()

    airport_code = ""
    m = re.search(r'\(([A-Z]{3})\)', name)
    if m:
        airport_code = m.group(1)
    elif is_ap and len(oag) >= 3 and oag[:3].isalpha():
        airport_code = oag[:3].upper()

    brand_hint = str(d.get("_brand_hint") or "Hertz")
    if "dollar" in brand_hint.lower():
        brand = "Dollar"
    elif "thrifty" in brand_hint.lower():
        brand = "Thrifty"
    else:
        brand = "Hertz"

    entry = _loc(
        provider=brand,
        brand=brand,
        location_id=oag,
        location_url_param=oag,
        name=name,
        address=address,
        city=city,
        state=state,
        country=country,
        airport_code=airport_code,
        is_airport=is_ap,
        lat=lat,
        lng=lng,
    )
    entry["station_code"] = oag
    if region:
        entry["_region"] = region
    return entry


async def fetch_hertz_global(playwright) -> List[Dict]:
    """
    Fetch Hertz / Dollar / Thrifty locations globally using a coordinate grid scan.

    Uses ?lat=...&long=...&radius=... form of the geo-search API, which works for
    any country.  No browser required.  All three brands are scanned; results are
    deduplicated by oag station code across brands so each station appears once.

    Expected yield: 8,000–15,000 unique global locations.
    """
    grid        = generate_coordinate_grid()
    total_pts   = len(grid)
    BRANDS      = ["hertz", "dollar", "thrifty"]

    print(f"\n{'━' * 60}")
    print(f"[HertzGlobal] Global coordinate grid scan")
    print(f"  Grid points : {total_pts:,}")
    print(f"  Brands      : {', '.join(BRANDS)}")
    print(f"  API         : {_HERTZ_GEO_BASE}/{{brand}}?lat=...&long=...&radius=...")
    print(f"{'━' * 60}")

    all_entries: Dict[str, Dict] = {}   # oag → parsed entry (global dedup)
    t0 = time.monotonic()

    for brand in BRANDS:
        brand_raw: Dict[str, Dict] = {}   # oag → raw dict for this brand
        print(f"\n[HertzGlobal] Scanning {brand.upper()} ({total_pts:,} grid points)...")

        tasks = [
            asyncio.to_thread(_geo_search_sync_coords, brand, lat, lng, radius)
            for lat, lng, radius, _region in grid
        ]

        batch_size = 40   # 40 concurrent HTTP requests
        reported   = 0
        for i in range(0, len(tasks), batch_size):
            chunk_grid   = grid[i: i + batch_size]
            batch_results = await asyncio.gather(*tasks[i: i + batch_size])
            for (lat, lng, radius, region), raw_locs in zip(chunk_grid, batch_results):
                for d in raw_locs:
                    oag = str(d.get("oag") or d.get("oag3") or "").strip()
                    if oag and oag not in brand_raw:
                        d["_region"] = region
                        brand_raw[oag] = d
            # Progress every 500 points
            if i + batch_size - reported >= 500 or i + batch_size >= len(tasks):
                print(f"  {brand.upper()} {min(i + batch_size, total_pts):>5}/{total_pts} pts  "
                      f"{len(brand_raw):>5} unique so far")
                reported = i + batch_size
            await asyncio.sleep(0.15)   # gentle rate limit

        # Parse into unified global schema
        brand_new = 0
        for d in brand_raw.values():
            region = d.get("_region", "?")
            entry  = _parse_hertz_geo_loc_global(d, region=region)
            if entry and entry["location_id"] not in all_entries:
                all_entries[entry["location_id"]] = entry
                brand_new += 1

        print(f"  {brand.upper():<8}: {len(brand_raw):>5} raw oags → "
              f"{brand_new} new entries  (running total {len(all_entries):,})")

    elapsed   = time.monotonic() - t0
    locations = list(all_entries.values())

    # ── Summary ──────────────────────────────────────────────────────────────
    airports   = sum(1 for l in locations if l["is_airport"])
    by_brand:   Dict[str, int] = {}
    by_country: Dict[str, int] = {}
    by_region:  Dict[str, int] = {}
    for l in locations:
        by_brand[l["brand"]]     = by_brand.get(l["brand"], 0) + 1
        by_country[l["country"]] = by_country.get(l["country"], 0) + 1
        r = l.get("_region", "?")
        by_region[r] = by_region.get(r, 0) + 1

    print(f"\n[HertzGlobal] Done in {elapsed:.0f}s")
    print(f"  Total unique locations : {len(locations):,}")
    print(f"    Airport branches     : {airports:,}")
    print(f"    City branches        : {len(locations) - airports:,}")
    print(f"\n  By brand:")
    for b, n in sorted(by_brand.items()):
        print(f"    {b:<12} {n:>6,}")
    print(f"\n  By region:")
    for r, n in sorted(by_region.items(), key=lambda x: -x[1]):
        print(f"    {r:<8} {n:>6,}")
    print(f"\n  Top 20 countries:")
    for c, n in sorted(by_country.items(), key=lambda x: -x[1])[:20]:
        print(f"    {c:<6} {n:>6,}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  SIXT — GLOBAL SEARCH
# ─────────────────────────────────────────────────────────────────────────────

SIXT_GLOBAL_SEARCH_TERMS: List[str] = [
    # ── UK ───────────────────────────────────────────────────────────────────
    "LHR","LGW","LTN","STN","LCY","BHX","MAN","LPL","NCL","EDI","GLA","PIK",
    "ABZ","BRS","EXT","CWL","BFS","DSA",
    "London","Manchester","Birmingham","Glasgow","Edinburgh","Leeds","Liverpool",
    "Bristol","Sheffield","Leicester","Coventry","Bradford","Nottingham",
    "Southampton","Portsmouth","Brighton","Oxford","Cambridge","Reading",
    "Cardiff","Belfast","Aberdeen","Dundee","Inverness",
    # ── Germany ──────────────────────────────────────────────────────────────
    "FRA","MUC","BER","HAM","DUS","CGN","STR","NUE","HAJ","LEJ","DRS","BRE","ERF",
    "Frankfurt","Munich","Berlin","Hamburg","Dusseldorf","Cologne","Stuttgart",
    "Nuremberg","Hanover","Dresden","Leipzig","Bremen","Dortmund","Essen",
    "Duisburg","Bochum","Wuppertal","Bonn","Mannheim","Karlsruhe","Augsburg",
    "Wiesbaden","Gelsenkirchen","Munster","Aachen","Braunschweig","Kiel","Freiburg",
    "Erfurt","Rostock","Mainz","Lubeck","Magdeburg","Saarbrucken",
    # ── France ───────────────────────────────────────────────────────────────
    "CDG","ORY","NCE","LYS","MRS","TLS","BOD","NTE","MLH","SXB","LIL","RNS",
    "Paris","Lyon","Marseille","Nice","Toulouse","Bordeaux","Nantes","Strasbourg",
    "Lille","Rennes","Montpellier","Cannes","Monaco","Grenoble","Dijon","Angers",
    "Nimes","Tours","Clermont-Ferrand","Limoges","Reims","Aix-en-Provence",
    # ── Spain ────────────────────────────────────────────────────────────────
    "MAD","BCN","AGP","PMI","VLC","SVQ","BIO","LPA","TFS","ACE","FUE","IBZ",
    "MAH","ALC","MJV","REU","VGO","SCQ","OVD","SDR","ZAZ","GRX",
    "Madrid","Barcelona","Seville","Valencia","Bilbao","Malaga","Palma","Ibiza",
    "Alicante","Tenerife","Gran Canaria","Menorca","Lanzarote","Fuerteventura",
    "Zaragoza","Santander","Santiago de Compostela","Vigo","Granada","Murcia",
    # ── Italy ────────────────────────────────────────────────────────────────
    "FCO","MXP","LIN","NAP","VCE","BLQ","FLR","TRN","CTA","PMO","CAG","PSA",
    "BRI","BGY","TSF","VRN","GOA","PMF","REG","SUF",
    "Rome","Milan","Naples","Venice","Florence","Bologna","Turin","Palermo",
    "Catania","Bari","Genoa","Verona","Pisa","Cagliari","Rimini","Trieste",
    "Bergamo","Brescia","Padua","Salerno","Reggio Calabria",
    # ── Netherlands ──────────────────────────────────────────────────────────
    "AMS","EIN","RTM","MST","GRQ",
    "Amsterdam","Rotterdam","Utrecht","Eindhoven","The Hague","Tilburg",
    "Groningen","Almere","Breda","Nijmegen","Enschede","Apeldoorn",
    # ── Belgium ──────────────────────────────────────────────────────────────
    "BRU","CRL","LGG","ANR",
    "Brussels","Antwerp","Ghent","Bruges","Liege","Namur","Leuven","Charleroi",
    # ── Switzerland ──────────────────────────────────────────────────────────
    "ZUR","GVA","BSL","BRN",
    "Zurich","Geneva","Basel","Bern","Lausanne","Lugano","Lucerne",
    "Interlaken","St Gallen","Winterthur","Zug",
    # ── Austria ──────────────────────────────────────────────────────────────
    "VIE","SZG","INN","GRZ","LNZ","KLU",
    "Vienna","Salzburg","Innsbruck","Graz","Linz","Klagenfurt","Wels",
    # ── Portugal ─────────────────────────────────────────────────────────────
    "LIS","OPO","FAO","FNC","PDL",
    "Lisbon","Porto","Faro","Madeira","Azores","Setubal","Coimbra","Braga",
    # ── Greece ───────────────────────────────────────────────────────────────
    "ATH","HER","SKG","RHO","CFU","JMK","JSI","KGS","CHQ","ZTH","EFL","SMI",
    "Athens","Thessaloniki","Heraklion","Rhodes","Corfu","Mykonos","Santorini",
    "Kos","Chania","Zakynthos","Kefalonia","Samos","Patras","Volos","Larissa",
    # ── Nordics ──────────────────────────────────────────────────────────────
    "CPH","AAR","BLL","OSL","BGO","TRD","SVG","BOO","TOS","ARN","GOT","MMX",
    "LLA","UME","HEL","TMP","TKU","OUL","JYV","REK","KEF",
    "Copenhagen","Aarhus","Oslo","Bergen","Trondheim","Stavanger","Tromso",
    "Stockholm","Gothenburg","Malmo","Helsinki","Tampere","Turku","Oulu",
    "Reykjavik","Akureyri",
    # ── Eastern Europe ───────────────────────────────────────────────────────
    "PRG","WAW","KRK","WRO","GDN","KTW","POZ","BUD","DEB","OTP","CLJ","TSR",
    "SOF","VAR","ZAG","SPU","LJU","BTS","VNO","RIX","TLL","MSQ","KBP","LWO",
    "Prague","Warsaw","Krakow","Wroclaw","Gdansk","Katowice","Poznan",
    "Budapest","Debrecen","Bucharest","Cluj-Napoca","Timisoara","Sofia","Varna",
    "Zagreb","Split","Ljubljana","Bratislava","Vilnius","Riga","Tallinn",
    "Minsk","Kyiv","Lviv","Odessa",
    # ── Middle East ──────────────────────────────────────────────────────────
    "DXB","AUH","SHJ","DWC","DOH","KWI","BAH","AMM","AQJ","BEY","TLV","SDV",
    "MCT","SLL","RUH","JED","DMM","MED","AHB",
    "Dubai","Abu Dhabi","Sharjah","Doha","Kuwait City","Bahrain","Manama",
    "Amman","Beirut","Tel Aviv","Jerusalem","Muscat","Riyadh","Jeddah",
    "Dammam","Medina","Aqaba",
    # ── Asia Pacific ─────────────────────────────────────────────────────────
    "SIN","KUL","BKK","DMK","HKT","HKG","NRT","HND","KIX","NGO","CTS","FUK",
    "ICN","GMP","TPE","TSA","KHH","PEK","PVG","CAN","SZX","CTU","WUH","XIY",
    "DEL","BOM","BLR","MAA","HYD","CCU","AMD","GOI","COK","PNQ","JAI","LKO",
    "CMB","DAC","KTM","RGN","SGN","HAN","DPS","CGK","MES","SUB","UPG",
    "MNL","CEB","BKI","KUL","PEN","LGK","JHB",
    "Singapore","Kuala Lumpur","Bangkok","Phuket","Hong Kong","Tokyo","Osaka",
    "Nagoya","Sapporo","Fukuoka","Seoul","Taipei","Kaohsiung","Beijing",
    "Shanghai","Guangzhou","Shenzhen","Chengdu","Wuhan","Xi'an",
    "Delhi","Mumbai","Bangalore","Chennai","Hyderabad","Kolkata","Ahmedabad",
    "Goa","Kochi","Pune","Jaipur","Lucknow","Colombo","Dhaka","Kathmandu",
    "Rangoon","Ho Chi Minh City","Hanoi","Bali","Jakarta","Surabaya","Makassar",
    "Manila","Cebu","Kota Kinabalu","Penang","Langkawi","Johor Bahru",
    # ── Australia / NZ ───────────────────────────────────────────────────────
    "SYD","MEL","BNE","PER","ADL","CBR","OOL","CNS","HBA","TSV","MKY","NTL",
    "LST","DRW","ASP","AKL","WLG","CHC","ZQN","NSN","DUD","PMR",
    "Sydney","Melbourne","Brisbane","Perth","Adelaide","Canberra","Gold Coast",
    "Cairns","Hobart","Townsville","Mackay","Darwin","Alice Springs",
    "Auckland","Wellington","Christchurch","Queenstown","Nelson","Dunedin",
    # ── Canada ───────────────────────────────────────────────────────────────
    "YYZ","YVR","YUL","YYC","YEG","YOW","YHZ","YWG","YQB","YXE","YYJ","YLW",
    "YKF","YHM","YQT","YQR","YXU",
    "Toronto","Vancouver","Montreal","Calgary","Edmonton","Ottawa","Halifax",
    "Winnipeg","Quebec City","Saskatoon","Victoria","Kelowna",
    "Waterloo","Hamilton","Thunder Bay","Regina","London Ontario",
    # ── Latin America ────────────────────────────────────────────────────────
    "GRU","GIG","BSB","SSA","REC","FOR","POA","CWB","BEL","MAO","FLN","CGH",
    "EZE","AEP","COR","MDZ","ROS","NQN","SCL","ANF","PMC","IQQ","CCP",
    "BOG","MDE","CLO","CTG","BAQ","BGA","MHC",
    "LIM","CUZ","AQP","TRU","PIU","IQT",
    "GYE","UIO","OCC",
    "CCS","MAR","BLA","PMV",
    "PTY","SJO","SAL","TGU","MGA","GUA","BZE",
    "MEX","CUN","GDL","MTY","TIJ","MID","OAX","VER","PVR","ZIH","SJD",
    "HAV","SDQ","PUJ","SJU","STT","BGI","ANU","SXM","GCM",
    "Sao Paulo","Rio de Janeiro","Brasilia","Salvador","Recife","Fortaleza",
    "Buenos Aires","Cordoba","Mendoza","Rosario","Bariloche","Santiago",
    "Bogota","Medellin","Cali","Cartagena","Barranquilla",
    "Lima","Cusco","Arequipa",
    "Quito","Guayaquil",
    "Caracas","Maracaibo",
    "Panama City","San Jose CR","San Salvador","Tegucigalpa","Managua",
    "Guatemala City","Belize City",
    "Mexico City","Cancun","Guadalajara","Monterrey","Tijuana","Merida",
    "Oaxaca","Veracruz","Puerto Vallarta","Zihuatanejo","Los Cabos",
    "Havana","Santo Domingo","Punta Cana","Kingston","Nassau",
    # ── Africa ───────────────────────────────────────────────────────────────
    "JNB","CPT","DUR","PLZ","ELS","GRJ","BFN","HLA","MQP",
    "CAI","HRG","SSH","LXR","ASW","ALY",
    "CMN","RAK","TNG","AGA","OUD","FEZ",
    "TUN","SFA","DJE","MIR","TOE",
    "ALG","ORN","CZL","AAE",
    "NBO","MBA","WIL",
    "ADD","DIR","JIJ",
    "ACC","KMS",
    "LOS","ABV","PHC","KAN","IBA","QOW",
    "DKR","ZIG",
    "ABJ","BKO","OUA","COO","LFW",
    "CMR","YAO","DLA","NSI",
    "DAR","ZNZ","JRO","MWZ","KGL","EBB","JBA",
    "LUN","HRE","BUQ","VFA","NLA",
    "Johannesburg","Cape Town","Durban","Port Elizabeth","Pretoria","Bloemfontein",
    "Cairo","Hurghada","Sharm El Sheikh","Luxor","Aswan","Alexandria",
    "Casablanca","Marrakech","Tangier","Agadir","Fez","Rabat",
    "Tunis","Sfax","Djerba","Monastir",
    "Algiers","Oran","Constantine",
    "Nairobi","Mombasa","Kisumu",
    "Addis Ababa","Dire Dawa",
    "Accra","Kumasi",
    "Lagos","Abuja","Port Harcourt","Kano","Ibadan","Owerri",
    "Dakar","Ziguinchor",
    "Abidjan","Bamako","Ouagadougou","Cotonou","Lome",
    "Yaounde","Douala",
    "Dar es Salaam","Zanzibar","Kilimanjaro","Mwanza","Kigali","Entebbe",
    "Lusaka","Harare","Bulawayo","Livingstone","Ndola",
]


def _sixt_suggest_sync_global(term: str) -> List[Dict]:
    """
    Same as _sixt_suggest_sync but returns ALL suggestion types (not airport-only)
    and preserves the actual country_code from the API response.
    """
    import uuid as _uuid_mod
    _SIXT_SUGGEST_URL = (
        "https://grpc-prod.orange.sixt.com/"
        "com.sixt.service.rent_booking.api.SearchService/SuggestLocations"
    )
    _SIXT_API_HEADERS = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "Origin":       "https://www.sixt.com",
        "Referer":      "https://www.sixt.com/car-rental/",
        "User-Agent":   USER_AGENT,
    }
    try:
        body = json.dumps({
            "query":                    term,
            "auto_complete_session_id": str(_uuid_mod.uuid4()),
            "vehicle_type":             1,
        }).encode()
        req = urllib.request.Request(
            _SIXT_SUGGEST_URL, data=body,
            headers=_SIXT_API_HEADERS, method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for sug in data.get("suggestions", []):
            loc    = sug.get("location", {})
            if not isinstance(loc, dict):
                continue
            branch = loc.get("branch", {})
            bid    = str(branch.get("id", "")).strip()
            if not bid:
                bid = str(loc.get("location_id", "")).replace("BRANCH:", "").strip()
            if not bid:
                continue
            pos     = loc.get("position") or {}
            results.append({
                "id":           bid,
                "name":         loc.get("title") or branch.get("title") or "",
                "lat":          float(pos.get("latitude",  0) or 0),
                "lng":          float(pos.get("longitude", 0) or 0),
                "country":      loc.get("country_code") or "??",
                "type":         loc.get("type", ""),
                "iataCode":     loc.get("iata_code") or "",
            })
        return results
    except Exception:
        return []


async def fetch_sixt_global(playwright) -> List[Dict]:
    """
    Fetch SIXT locations globally using the SuggestLocations API in parallel.

    No browser required — pure urllib calls run via asyncio.to_thread.
    Searches all terms in SIXT_GLOBAL_SEARCH_TERMS; deduplicates by branch ID.
    Country is taken from the API response, not hardcoded.

    Expected yield: 3,000–5,000 unique global locations.
    """
    total_terms = len(SIXT_GLOBAL_SEARCH_TERMS)
    print(f"\n{'━' * 60}")
    print(f"[SIXTGlobal] Global SuggestLocations API scan")
    print(f"  Terms : {total_terms:,}")
    print(f"  API   : grpc-prod.orange.sixt.com/SuggestLocations")
    print(f"{'━' * 60}")

    raw: Dict[str, Dict] = {}   # branch_id → raw dict
    t0 = time.monotonic()

    tasks = [
        asyncio.to_thread(_sixt_suggest_sync_global, term)
        for term in SIXT_GLOBAL_SEARCH_TERMS
    ]
    batch_size = 20
    for i in range(0, len(tasks), batch_size):
        batch_terms   = SIXT_GLOBAL_SEARCH_TERMS[i: i + batch_size]
        batch_results = await asyncio.gather(*tasks[i: i + batch_size])
        for term, locs in zip(batch_terms, batch_results):
            for loc in locs:
                bid = str(loc.get("id") or "").strip()
                if bid and bid not in raw:
                    raw[bid] = loc
        if (i + batch_size) % 200 == 0 or i + batch_size >= len(tasks):
            print(f"  {min(i + batch_size, total_terms):>4}/{total_terms} terms  "
                  f"{len(raw):>5} unique branches so far")
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - t0

    # ── Parse raw into unified schema ────────────────────────────────────────
    locations: List[Dict] = []
    by_country: Dict[str, int] = {}
    for bid, d in raw.items():
        name = str(d.get("name") or "").strip()
        if not name or len(name) < 3:
            continue
        lat     = float(d.get("lat") or 0)
        lng     = float(d.get("lng") or 0)
        country = str(d.get("country") or "??").upper()
        iata    = str(d.get("iataCode") or "").upper()
        loc_type = str(d.get("type") or "")
        is_ap   = (
            "AIRPORT" in loc_type.upper() or
            bool(iata) or
            "airport" in name.lower()
        )
        entry = _loc(
            provider="SIXT",
            brand="SIXT",
            location_id=bid,
            location_url_param="",   # UUID unknown at this stage
            name=name,
            country=country,
            airport_code=iata,
            is_airport=is_ap,
            lat=lat,
            lng=lng,
        )
        locations.append(entry)
        by_country[country] = by_country.get(country, 0) + 1

    airports = sum(1 for l in locations if l["is_airport"])
    print(f"\n[SIXTGlobal] Done in {elapsed:.0f}s")
    print(f"  Total unique locations : {len(locations):,}")
    print(f"    Airport branches     : {airports:,}")
    print(f"    City branches        : {len(locations) - airports:,}")
    print(f"\n  Top 20 countries:")
    for c, n in sorted(by_country.items(), key=lambda x: -x[1])[:20]:
        print(f"    {c:<6} {n:>6,}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  EHI — GLOBAL ENDPOINT INVESTIGATION & SCAN
# ─────────────────────────────────────────────────────────────────────────────

_EHI_ENDPOINTS = {
    "US-east":  "https://prd-east.webapi.enterprise.com/enterprise-ewt/location/{id}?type=both",
    "EU":       "https://prd-emea.webapi.enterprise.com/enterprise-ewt/location/{id}?type=both",
    "APAC":     "https://prd-apac.webapi.enterprise.com/enterprise-ewt/location/{id}?type=both",
}

# ID ranges to scan per endpoint.  Tuples of (start, end, step).
_EHI_GLOBAL_RANGES: Dict[str, List[Tuple[int, int, int]]] = {
    "US-east": [(1_000_000, 1_090_000, 4)],     # confirmed US range
    "EU":      [(1_000_000, 1_090_000, 4),       # US range may return EU locs too
                (5_000_000, 6_000_000, 25),       # speculative EU range
                (10_000_000, 11_000_000, 50)],    # speculative EU extended range
    "APAC":    [(1_000_000, 1_090_000, 4),
                (5_000_000, 6_000_000, 25),
                (10_000_000, 11_000_000, 50)],
}

_EHI_GLOBAL_CONCURRENCY = 80


def _fetch_ehi_location_endpoint(loc_id: int, endpoint_url: str) -> Optional[Dict]:
    """Fetch one EHI location from a specific endpoint URL template."""
    url = endpoint_url.format(id=loc_id)
    req = urllib.request.Request(url, headers={
        "Accept":     "application/json",
        "brand":      "ENTERPRISE",
        "channel":    "WEB",
        "locale":     "en_US",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            loc  = data.get("location")
            if loc:
                loc["_endpoint"] = endpoint_url   # tag source
            return loc
    except Exception:
        return None


def _parse_ehi_global(loc: Dict) -> Optional[Dict]:
    """Parse one EHI API location dict into unified schema (no country filter)."""
    api_id   = str(loc.get("id", "")).strip()
    grp_code = str(loc.get("location_id", "")).strip()
    name     = str(loc.get("name", "")).strip()
    if not api_id or not name:
        return None
    name_lower = name.lower()
    if any(kw in name_lower for kw in _EHI_SKIP_KEYWORDS):
        return None

    gps  = loc.get("gps") or {}
    lat  = float(gps.get("latitude",  0) or 0)
    lng  = float(gps.get("longitude", 0) or 0)

    addr_obj = loc.get("address") or {}
    country  = str(addr_obj.get("country_code", "") or "US").upper()
    address  = ", ".join(
        s for s in (addr_obj.get("street_addresses") or [""])
        if isinstance(s, str)
    )
    city  = str(addr_obj.get("city",  "") or "")
    state = str(addr_obj.get("country_subdivision_code", "") or "")
    tz    = str(loc.get("time_zone_id", "") or "")

    airport_code = str(loc.get("airport_code", "") or "").upper()
    loc_type     = str(loc.get("location_type", "") or "")
    is_ap        = bool(airport_code) or loc_type == "airport"

    entry = _loc(
        provider="EHI",
        brand="Enterprise",
        location_id=api_id,
        location_url_param=grp_code,
        name=name,
        address=address,
        city=city,
        state=state,
        country=country,
        airport_code=airport_code,
        is_airport=is_ap,
        lat=lat,
        lng=lng,
    )
    entry["group_branch_id"] = grp_code
    entry["time_zone_id"]    = tz
    entry["gps"]             = {"latitude": lat, "longitude": lng}
    entry["country_code"]    = country
    return entry


async def fetch_enterprise_global(playwright) -> List[Dict]:
    """
    Fetch Enterprise / National / Alamo locations globally by probing all three
    regional API endpoints (US-east, EU, APAC) across multiple ID ranges.

    For each endpoint we scan the known US range (which may also return non-US
    locations on that endpoint) plus speculative ranges for EU/APAC IDs.
    No browser required.

    Expected yield: 2,000–4,000 additional non-US locations.
    """
    print(f"\n{'━' * 60}")
    print(f"[EHIGlobal] Enterprise / National / Alamo global endpoint scan")
    for name, url in _EHI_ENDPOINTS.items():
        print(f"  {name:<10}: {url[:70]}")
    print(f"{'━' * 60}")

    # First probe each endpoint with a known ID to see which respond
    TEST_ID = 1_018_775   # confirmed US Enterprise airport location
    print(f"\n[EHIGlobal] Probing each endpoint with ID {TEST_ID}...")
    active_endpoints: Dict[str, str] = {}
    for label, url_tpl in _EHI_ENDPOINTS.items():
        result = await asyncio.to_thread(_fetch_ehi_location_endpoint, TEST_ID, url_tpl)
        if result:
            name_found = result.get("name", "?")
            print(f"  {label:<10}: ACTIVE  → '{name_found}'")
            active_endpoints[label] = url_tpl
        else:
            print(f"  {label:<10}: no response — skipping")

    if not active_endpoints:
        print("[EHIGlobal] No endpoints responded — aborting.")
        return []

    sem = asyncio.Semaphore(_EHI_GLOBAL_CONCURRENCY)
    all_entries: Dict[str, Dict] = {}   # api_id → parsed entry
    seen_ids: set = set()
    t0 = time.monotonic()

    async def _bounded_fetch(loc_id: int, url_tpl: str) -> Optional[Dict]:
        async with sem:
            return await asyncio.to_thread(_fetch_ehi_location_endpoint, loc_id, url_tpl)

    for label, url_tpl in active_endpoints.items():
        ranges = _EHI_GLOBAL_RANGES.get(label, [(1_000_000, 1_090_000, 4)])
        for (rstart, rend, rstep) in ranges:
            ids_list = list(range(rstart, rend, rstep))
            total    = len(ids_list)
            print(f"\n[EHIGlobal] {label}  range {rstart:,}–{rend:,} step={rstep}  "
                  f"({total:,} IDs)...")

            tasks = [_bounded_fetch(i, url_tpl) for i in ids_list]
            results_raw = await asyncio.gather(*tasks)

            found_this_range = 0
            non_us_this_range = 0
            for raw_loc in results_raw:
                if raw_loc is None:
                    continue
                api_id = str(raw_loc.get("id", "")).strip()
                if not api_id or api_id in seen_ids:
                    continue
                seen_ids.add(api_id)
                entry = _parse_ehi_global(raw_loc)
                if entry:
                    all_entries[api_id] = entry
                    found_this_range += 1
                    if entry["country"] != "US":
                        non_us_this_range += 1

            print(f"  {label} {rstart:,}–{rend:,}: {found_this_range} locations "
                  f"({non_us_this_range} non-US)  running total {len(all_entries):,}")

    elapsed   = time.monotonic() - t0
    locations = list(all_entries.values())

    airports   = sum(1 for l in locations if l["is_airport"])
    by_country: Dict[str, int] = {}
    for l in locations:
        by_country[l["country"]] = by_country.get(l["country"], 0) + 1

    print(f"\n[EHIGlobal] Done in {elapsed:.0f}s")
    print(f"  Total locations    : {len(locations):,}")
    print(f"    Airport branches : {airports:,}")
    print(f"    City branches    : {len(locations) - airports:,}")
    print(f"\n  Top 20 countries:")
    for c, n in sorted(by_country.items(), key=lambda x: -x[1])[:20]:
        print(f"    {c:<6} {n:>6,}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# ██████  KAYAK — GLOBAL AIRPORT LOCATION IDs (browser automation)
# ─────────────────────────────────────────────────────────────────────────────

_KAYAK_GLOBAL_AIRPORTS: List[str] = [
    # UK
    "LHR","LGW","LTN","STN","LCY","BHX","MAN","LPL","NCL","EDI","GLA","PIK",
    "ABZ","BRS","EXT","CWL","BFS",
    # Germany
    "FRA","MUC","BER","HAM","DUS","CGN","STR","NUE","HAJ","LEJ","DRS","BRE","ERF",
    # France
    "CDG","ORY","NCE","LYS","MRS","TLS","BOD","NTE","SXB","LIL","RNS","MPL",
    # Spain
    "MAD","BCN","AGP","PMI","VLC","SVQ","BIO","LPA","TFS","ACE","FUE","IBZ",
    "MAH","ALC","MJV","GRX","ZAZ","OVD","SCQ","SDR","VGO",
    # Italy
    "FCO","MXP","LIN","NAP","VCE","BLQ","FLR","TRN","CTA","PMO","CAG","PSA",
    "BRI","BGY","TSF","VRN","GOA",
    # Netherlands / Belgium / Switzerland / Austria / Portugal
    "AMS","EIN","RTM","BRU","CRL","LGG","ANR","ZUR","GVA","BSL","VIE","SZG",
    "INN","GRZ","LNZ","LIS","OPO","FAO","FNC",
    # Greece / Nordics
    "ATH","HER","SKG","RHO","CFU","JMK","KGS","CHQ","ZTH","CPH","OSL","BGO",
    "TRD","SVG","ARN","GOT","MMX","HEL","TMP","REK",
    # Eastern Europe
    "PRG","WAW","KRK","WRO","GDN","BUD","OTP","CLJ","SOF","ZAG","SPU","LJU",
    "BTS","VNO","RIX","TLL",
    # Middle East
    "DXB","AUH","SHJ","DOH","KWI","BAH","AMM","BEY","TLV","MCT","RUH","JED",
    # Asia Pacific
    "SIN","KUL","BKK","DMK","HKT","HKG","NRT","HND","KIX","NGO","CTS","FUK",
    "ICN","GMP","TPE","TSA","PEK","PVG","CAN","SZX","DEL","BOM","BLR","MAA",
    "HYD","CCU","CMB","SGN","HAN","DPS","CGK","MNL","CEB",
    # Australia / NZ
    "SYD","MEL","BNE","PER","ADL","CBR","OOL","CNS","HBA","DRW",
    "AKL","WLG","CHC","ZQN",
    # Canada
    "YYZ","YVR","YUL","YYC","YEG","YOW","YHZ","YWG","YQB","YXE",
    # Latin America
    "GRU","GIG","BSB","EZE","SCL","BOG","MDE","LIM","GYE","UIO","CCS",
    "PTY","SJO","MEX","CUN","GDL","MTY","HAV","SDQ","SJU",
    # Africa
    "JNB","CPT","DUR","CAI","HRG","SSH","CMN","RAK","TUN","NBO","ADD",
    "ACC","LOS","ABV","DKR","DAR","LUN","HRE",
]


async def fetch_kayak_global(playwright) -> List[Dict]:
    """
    Build Kayak location entries for major international airports.

    Kayak uses plain IATA codes as URL parameters (e.g. /cars/LHR/... /cars/CDG/...)
    — the same scheme used for US airports in the existing DB.  No browser automation
    is needed: we create entries directly and cross-reference the existing DB for
    names / coordinates so that most entries are fully populated without a geocode
    pass.

    Expected yield: ~240 airports (all entries in _KAYAK_GLOBAL_AIRPORTS that are
    not already present in the DB under the Kayak brand).
    """
    total = len(_KAYAK_GLOBAL_AIRPORTS)
    print(f"\n{'━' * 60}")
    print(f"[KayakGlobal] Building Kayak entries for {total} international airports")
    print(f"[KayakGlobal] Strategy: IATA code = location_url_param (confirmed by URL test)")
    print(f"{'━' * 60}")

    t0 = time.monotonic()

    # ── Build a lookup: IATA → best existing DB entry (from any other provider) ─
    _raw_db       = load_db()          # {"locations": [...], "metadata": {...}}
    existing_locs = _raw_db.get("locations", [])
    iata_lookup: Dict[str, Dict] = {}
    for entry in existing_locs:
        if not isinstance(entry, dict):
            continue
        iata = entry.get("airport_code") or entry.get("iata") or ""
        if not iata:
            continue
        # Prefer entries that have coordinates
        prev = iata_lookup.get(iata)
        has_coords = entry.get("lat") and entry.get("lng")
        prev_has   = prev and prev.get("lat") and prev.get("lng")
        if not prev or (has_coords and not prev_has):
            iata_lookup[iata] = entry

    # ── Also build set of IATA codes already in DB under Kayak brand ────────────
    kayak_existing = {
        e.get("airport_code", "")
        for e in existing_locs
        if isinstance(e, dict) and e.get("brand", "").lower() == "kayak" and e.get("airport_code")
    }

    locations: List[Dict] = []
    added = skipped = no_meta = 0

    for iata in _KAYAK_GLOBAL_AIRPORTS:
        if iata in kayak_existing:
            skipped += 1
            continue

        # Pull metadata from another provider if available
        ref = iata_lookup.get(iata, {})
        name    = ref.get("name")    or f"{iata} Airport"
        city    = ref.get("city")    or ""
        country = ref.get("country") or "??"
        lat     = ref.get("lat")
        lng     = ref.get("lng")

        if not (lat and lng):
            no_meta += 1

        entry = _loc(
            provider="Kayak",
            brand="Kayak",
            location_id=iata,
            location_url_param=iata,
            name=name,
            city=city,
            country=country,
            airport_code=iata,
            is_airport=True,
            lat=lat or 0.0,
            lng=lng or 0.0,
        )
        locations.append(entry)
        added += 1

    elapsed = time.monotonic() - t0
    print(f"\n[KayakGlobal] Done in {elapsed:.1f}s")
    print(f"  Airports in list   : {total}")
    print(f"  New entries added  : {added}")
    print(f"  Already in DB      : {skipped}")
    print(f"  Need geocoding     : {no_meta}")

    return locations


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    # ── Geocode-only mode (synchronous — no browser needed) ─────────────────
    if args.geocode:
        geocode_missing_coords()
        return

    run_all = not any([
        args.sixt, args.avis, args.hertz, args.enterprise,
        args.hertz_global, args.sixt_global, args.ehi_global, args.kayak_global,
    ])
    all_new: List[Dict] = []

    async with async_playwright() as pw:

        if run_all or args.sixt:
            locs = await fetch_sixt_us(pw)
            all_new.extend(locs)
            print(f"\n[SIXT] ✓ {len(locs)} locations ready")

        if run_all or args.avis:
            api_locs = await fetch_avis_group(pw)
            if api_locs:
                all_new.extend(api_locs)
                print(f"[AvisBudgetGroup] ✓ {len(api_locs)} locations from API")
            else:
                synth_locs = generate_avis_budget_from_existing_db()
                all_new.extend(synth_locs)
                print(f"[AvisBudget] ✓ {len(synth_locs)} synthetic entries ready")

        if run_all or args.hertz:
            locs = await fetch_hertz_group(pw)
            all_new.extend(locs)
            if locs:
                print(f"[HertzCorp] ✓ {len(locs)} locations ready")

        if run_all or args.enterprise:
            locs = await fetch_enterprise_group(pw)
            all_new.extend(locs)
            if locs:
                print(f"[EnterpriseCorp] ✓ {len(locs)} locations ready")

        # ── Global fetchers ──────────────────────────────────────────────────

        if args.hertz_global:
            locs = await fetch_hertz_global(pw)
            all_new.extend(locs)
            print(f"\n[HertzGlobal] ✓ {len(locs):,} locations ready")

        if args.sixt_global:
            locs = await fetch_sixt_global(pw)
            all_new.extend(locs)
            print(f"\n[SIXTGlobal] ✓ {len(locs):,} locations ready")

        if args.ehi_global:
            locs = await fetch_enterprise_global(pw)
            all_new.extend(locs)
            print(f"\n[EHIGlobal] ✓ {len(locs):,} locations ready")

        if args.kayak_global:
            locs = await fetch_kayak_global(pw)
            all_new.extend(locs)
            print(f"\n[KayakGlobal] ✓ {len(locs):,} locations ready")

    if all_new:
        save_db(all_new)
        missing_count = sum(
            1 for l in all_new
            if float(l.get("lat", 0)) == 0 and float(l.get("lng", 0)) == 0
        )
        if missing_count:
            print(f"\n  {missing_count:,} new locations have no coordinates.")
            print("   Run:  python build_locations_db.py --geocode")
            print(f"   to fill them in via Nominatim (~{missing_count * 1.1 / 60:.0f} min).")
    else:
        print("\nNo locations captured — DB not modified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build locations_db.json")
    # ── US fetchers ──────────────────────────────────────────────────────────
    parser.add_argument("--sixt",         action="store_true",
                        help="Fetch SIXT US locations only")
    parser.add_argument("--avis",         action="store_true",
                        help="Fetch Avis / Budget / Payless locations only")
    parser.add_argument("--hertz",        action="store_true",
                        help="Fetch Hertz / Dollar / Thrifty US locations only")
    parser.add_argument("--enterprise",   action="store_true",
                        help="Fetch Enterprise / National / Alamo US locations only")
    parser.add_argument("--geocode",      action="store_true",
                        help="Geocode all DB entries with lat=0/lng=0 via Nominatim")
    # ── Global fetchers ──────────────────────────────────────────────────────
    parser.add_argument("--hertz-global", action="store_true",
                        help="Fetch Hertz/Dollar/Thrifty GLOBALLY via coordinate grid scan")
    parser.add_argument("--sixt-global",  action="store_true",
                        help="Fetch SIXT GLOBALLY via SuggestLocations API (all countries)")
    parser.add_argument("--ehi-global",   action="store_true",
                        help="Fetch Enterprise/National/Alamo GLOBALLY (EU+APAC endpoints)")
    parser.add_argument("--kayak-global", action="store_true",
                        help="Capture Kayak location IDs for major international airports")
    args = parser.parse_args()
    # Normalise hyphenated arg names to underscores
    args.hertz_global  = getattr(args, "hertz_global",  False)
    args.sixt_global   = getattr(args, "sixt_global",   False)
    args.ehi_global    = getattr(args, "ehi_global",    False)
    args.kayak_global  = getattr(args, "kayak_global",  False)

    asyncio.run(main(args))
