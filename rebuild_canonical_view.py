"""
rebuild_canonical_view.py
Rebuilds the canonical_locations view in Supabase via direct psycopg2 connection.
Run after any import to rental_locations.
"""
import os
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

conn = psycopg2.connect(os.environ['SUPABASE_DB_URL'])
conn.autocommit = True
cur = conn.cursor()

print("Rebuilding canonical_locations view...")

cur.execute("""
create or replace view canonical_locations as
with ranked as (
  select
    airport_code,
    name,
    city,
    country,
    is_airport,
    lat,
    lng,
    provider,
    row_number() over (
      partition by airport_code
      order by
        case when name ~* 'T[0-9][-T0-9]*$|AP T[0-9]' then 1 else 0 end,
        length(name)
    ) as rn
  from rental_locations
  where lat is not null
  and lng is not null
  and airport_code is not null
),
airports as (
  select airport_code, name, city, country, is_airport, lat, lng, provider
  from ranked where rn = 1
),
city_branches as (
  select
    null::text as airport_code,
    name,
    city,
    country,
    is_airport,
    lat,
    lng,
    provider,
    row_number() over (
      partition by name, city, country
      order by length(name)
    ) as rn
  from rental_locations
  where lat is not null
  and lng is not null
  and (airport_code is null or airport_code = '')
)
select airport_code, name, city, country, is_airport, lat, lng, provider
from airports
union all
select airport_code, name, city, country, is_airport, lat, lng, provider
from city_branches where rn = 1
""")
print("View created.")

cur.execute("grant select on canonical_locations to anon, authenticated")
print("Grants applied.")

cur.execute("select count(*) from canonical_locations")
total = cur.fetchone()[0]
cur.execute("select count(*) from canonical_locations where is_airport = true")
airports = cur.fetchone()[0]
print(f"canonical_locations: {total:,} rows ({airports:,} airports, {total - airports:,} city branches)")

# Spot-check key airports
cur.execute("""
    select airport_code, name, country
    from canonical_locations
    where airport_code in ('LHR','JFK','LAX','CDG','FRA','SYD','NRT','DXB')
    order by airport_code
""")
print("\nSpot-check:")
for r in cur.fetchall():
    print(f"  {r[0]}  {r[1][:40]:40s}  {r[2]}")

cur.close()
conn.close()
print("\nDone.")
