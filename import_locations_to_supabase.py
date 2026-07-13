"""
import_locations_to_supabase.py
Reads locations_db.json and upserts all locations into Supabase `rental_locations`.
Run after creating the table with the SQL in create_rental_locations.sql.
"""
import json
import os
from pathlib import Path

from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars before running.")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

db = json.loads(Path("locations_db.json").read_text(encoding="utf-8"))
locations = db["locations"]

print(f"Total locations in DB: {len(locations)}")

# Deduplicate by provider + location_id
seen: set = set()
unique = []
for loc in locations:
    key = (loc.get("provider", ""), loc.get("location_id", ""))
    if key not in seen:
        seen.add(key)
        unique.append(loc)

print(f"Unique locations (deduped): {len(unique)}")

# Build rows
rows = []
for loc in unique:
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

# Truncate existing data so re-runs are idempotent
print("Truncating existing rental_locations rows...")
sb.table("rental_locations").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()

# Insert in batches of 500
BATCH = 500
total_inserted = 0
for i in range(0, len(rows), BATCH):
    batch = rows[i : i + BATCH]
    sb.table("rental_locations").insert(batch).execute()
    total_inserted += len(batch)
    pct = total_inserted / len(rows) * 100
    print(f"  Inserted {total_inserted:,}/{len(rows):,} ({pct:.0f}%)  "
          f"[batch {i//BATCH + 1}]")

# Final count from Supabase
result = sb.table("rental_locations").select("id", count="exact").execute()
print(f"\nImport complete — {result.count:,} rows in Supabase rental_locations")
