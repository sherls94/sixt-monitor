"""
openflights_validate.py
-----------------------
1. Downloads OpenFlights airports.dat (~14k IATA airports, free/public domain).
2. Validates every airport_code in rental_locations against official IATA coords.
   - Location lat/lng >50 km from official airport  → clear airport_code.
3. Forward-matches: assigns IATA codes to airport entries missing them.
4. Fixes country field where OpenFlights gives authoritative data.
5. Re-imports cleaned locations_db.json to Supabase.
6. Rebuilds canonical_locations view.
"""
import csv
import io
import json
import math
import os
import time
import urllib.request
from collections import Counter
from pathlib import Path

import psycopg2

DB_URL   = "postgresql://postgres:Sh3rling!123@db.kqdraiypfajtbltgiggd.supabase.co:5432/postgres"
LOC_FILE = Path("/root/sixt-monitor/locations_db.json")

# ── 1. Download OpenFlights ──────────────────────────────────────────────────
print("Downloading OpenFlights airports.dat...")
url = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
with urllib.request.urlopen(url, timeout=30) as resp:
    raw_csv = resp.read().decode("utf-8", errors="replace")
print(f"  Downloaded {len(raw_csv):,} bytes")

# Fields: id, name, city, country, IATA, ICAO, lat, lng, ...
iata_by_code = {}  # IATA → {name, city, country, lat, lng}
iata_by_name = {}  # lowercase word → IATA  (for forward-matching)

for row in csv.reader(io.StringIO(raw_csv)):
    if len(row) < 8:
        continue
    iata = row[4].strip().upper()
    if not iata or iata == r"\N" or len(iata) != 3 or not iata.isalpha():
        continue
    try:
        lat = float(row[6])
        lng = float(row[7])
    except ValueError:
        continue
    rec = {"name": row[1].strip(), "city": row[2].strip(),
           "country": row[3].strip(), "lat": lat, "lng": lng}
    iata_by_code[iata] = rec
    # Index significant words for name matching
    for part in rec["name"].lower().replace("airport", "").replace("international", "").split():
        if len(part) > 3:
            iata_by_name.setdefault(part, iata)

print(f"  Loaded {len(iata_by_code):,} IATA airport records")


# ── 2. Haversine ─────────────────────────────────────────────────────────────
def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 3. Load & validate ───────────────────────────────────────────────────────
db   = json.loads(LOC_FILE.read_text(encoding="utf-8"))
locs = db["locations"]
print(f"\nValidating {len(locs):,} locations...")

THRESHOLD_KM = 50
cleared = 0
country_fixed = 0
already_ok = 0

for loc in locs:
    code = (loc.get("airport_code") or "").strip().upper()
    lat  = float(loc.get("lat") or 0)
    lng  = float(loc.get("lng") or 0)

    if not code:
        continue

    official = iata_by_code.get(code)
    if not official:
        # Unknown IATA code — clear it
        loc["airport_code"] = ""
        cleared += 1
        continue

    if lat and lng:
        dist = haversine_km(lat, lng, official["lat"], official["lng"])
        if dist > THRESHOLD_KM:
            loc["airport_code"] = ""
            loc["is_airport"] = False
            cleared += 1
        else:
            already_ok += 1
            # Fix country if we have authoritative data and current value is suspect
            if (not loc.get("country")
                    or loc.get("country") in ("US", "??", "None", "")):
                if official["country"] not in ("United States",):
                    loc["country"] = official["country"]
                    country_fixed += 1
    else:
        # No coords — fix country from OpenFlights anyway
        if (not loc.get("country")
                or loc.get("country") in ("US", "??", "None", "")):
            if official["country"] not in ("United States",):
                loc["country"] = official["country"]
                country_fixed += 1

print(f"  Cleared {cleared:,} mismatched / unknown codes  (>{THRESHOLD_KM} km off)")
print(f"  Verified {already_ok:,} correct codes")
print(f"  Country fixed via OpenFlights: {country_fixed:,}")


# ── 4. Forward-match: airports missing a code ────────────────────────────────
print("\nForward-matching airports without codes...")
assigned = 0
for loc in locs:
    if loc.get("airport_code"):
        continue
    if not loc.get("is_airport"):
        continue
    lat = float(loc.get("lat") or 0)
    lng = float(loc.get("lng") or 0)
    if not lat or not lng:
        continue
    name = (loc.get("name") or "").lower()
    best_code, best_dist = None, 999.0
    for word in name.replace("airport", "").replace("international", "").split():
        if len(word) < 4:
            continue
        candidate = iata_by_name.get(word)
        if not candidate:
            continue
        official = iata_by_code[candidate]
        dist = haversine_km(lat, lng, official["lat"], official["lng"])
        if dist < best_dist:
            best_dist, best_code = dist, candidate
    if best_code and best_dist < 30:
        loc["airport_code"] = best_code
        official = iata_by_code[best_code]
        if (not loc.get("country")
                or loc.get("country") in ("US", "??", "None", "")):
            if official["country"] not in ("United States",):
                loc["country"] = official["country"]
        assigned += 1

print(f"  Assigned {assigned:,} new airport codes via name matching")


# ── 5. Save updated JSON ─────────────────────────────────────────────────────
LOC_FILE.write_text(
    json.dumps(db, ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
print(f"\nSaved updated locations_db.json ({len(locs):,} entries)")

# Country distribution after fix
print("\nPost-fix country distribution by provider:")
for provider in ("SIXT", "Hertz", "EHI", "Enterprise"):
    entries = [l for l in locs if l.get("provider") == provider]
    if not entries:
        continue
    countries = Counter(str(l.get("country") or "NULL") for l in entries)
    top = [f"{c}:{n}" for c, n in countries.most_common(8)]
    print(f"  {provider:12s} ({len(entries):4d})  {', '.join(top)}")


# ── 6. Re-import to Supabase ─────────────────────────────────────────────────
print("\n=== Re-importing to Supabase ===")
try:
    from supabase import create_client
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://kqdraiypfajtbltgiggd.supabase.co")
    SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPABASE_KEY:
        raise ValueError("SUPABASE_SERVICE_KEY not set")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Deduplicate
    seen: set = set()
    unique = []
    for loc in locs:
        key = (loc.get("provider", ""), loc.get("location_id", ""))
        if key not in seen:
            seen.add(key)
            unique.append(loc)
    print(f"Unique locations: {len(unique):,}")

    # Truncate then re-insert
    sb.table("rental_locations").delete().neq(
        "id", "00000000-0000-0000-0000-000000000000"
    ).execute()
    print("Truncated rental_locations")

    BATCH = 500
    inserted = 0
    for i in range(0, len(unique), BATCH):
        batch = unique[i: i + BATCH]
        rows = []
        for loc in batch:
            gps = loc.get("gps", {})
            lat = loc.get("lat") or (gps.get("latitude")  if isinstance(gps, dict) else None)
            lng = loc.get("lng") or (gps.get("longitude") if isinstance(gps, dict) else None)
            rows.append({
                "provider":           loc.get("provider", ""),
                "brand":              loc.get("brand", "") or None,
                "location_id":        loc.get("location_id", "") or None,
                "location_url_param": loc.get("location_url_param", "") or None,
                "branch_id":          loc.get("branch_id", "") or None,
                "name":               loc.get("name", ""),
                "address":            loc.get("address", "") or None,
                "city":               loc.get("city", "") or None,
                "state":              loc.get("state", "") or None,
                "country":            loc.get("country", "") or None,
                "airport_code":       loc.get("airport_code", "") or None,
                "is_airport":         bool(loc.get("is_airport", False)),
                "lat":                float(lat) if lat else None,
                "lng":                float(lng) if lng else None,
            })
        sb.table("rental_locations").insert(rows).execute()
        inserted += len(rows)
        print(f"  {inserted:,}/{len(unique):,}  ({inserted / len(unique) * 100:.0f}%)")

    result = sb.table("rental_locations").select("id", count="exact").execute()
    print(f"\nImport complete — {result.count:,} rows in Supabase")

except Exception as exc:
    print(f"Supabase import failed: {exc}")
    print("(JSON was saved — run import_locations_to_supabase.py manually)")


# ── 7. Rebuild canonical_locations view ──────────────────────────────────────
print("\n=== Rebuilding canonical_locations view ===")
conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
create or replace view canonical_locations as
select distinct on (
    coalesce(airport_code, lower(trim(name)) || coalesce(country, ''))
)
    airport_code,
    min(name) over (
        partition by coalesce(airport_code, lower(trim(name)) || coalesce(country, ''))
    ) as name,
    city,
    country,
    is_airport,
    lat,
    lng
from rental_locations
where lat is not null and lng is not null
order by
    coalesce(airport_code, lower(trim(name)) || coalesce(country, '')),
    is_airport desc,
    length(name) asc
""")
cur.execute("grant select on canonical_locations to anon, authenticated")

cur.execute("select count(*) from canonical_locations")
total = cur.fetchone()[0]
cur.execute("select count(*) from canonical_locations where is_airport = true")
airports = cur.fetchone()[0]
print(f"canonical_locations: {total:,} rows  ({airports:,} airports, {total - airports:,} city branches)")

# Spot-check key airports
cur.execute("""
    select airport_code, name, country,
           round(lat::numeric, 2) as lat, round(lng::numeric, 2) as lng
    from canonical_locations
    where airport_code in ('LHR','LGW','JFK','LAX','CDG','FRA','SYD','DXB','NRT','SIN','AMS','YYZ','MAD','HND','FCO','MUC')
    order by airport_code
""")
print("\nKey airport spot-check (code / name / country / lat / lng):")
for r in cur.fetchall():
    print(f"  {r[0]:5s}  {r[1][:38]:38s}  {str(r[2]):22s}  {r[3]:7}  {r[4]}")

cur.close()
conn.close()
print("\nAll done.")
