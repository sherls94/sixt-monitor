import psycopg2
from pathlib import Path

# Load DB URL from .env
env_path = Path('/root/sixt-monitor/.env')
db_url = None
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line.startswith('SUPABASE_DB_URL='):
        db_url = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

if not db_url:
    db_url = 'postgresql://postgres:Sh3rling!123@db.kqdraiypfajtbltgiggd.supabase.co:5432/postgres'

print(f'Connecting to: {db_url[:50]}...')
conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()

# ── Query 1: delete SIXT null-coord aggregate labels
cur.execute("""
DELETE FROM rental_locations
WHERE provider = 'SIXT' AND lat IS NULL AND lng IS NULL
""")
q1_deleted = cur.rowcount
print(f'Query 1 (SIXT null-coord aggregate labels): {q1_deleted} rows deleted')

# ── Query 2: delete East London South Africa false positives
cur.execute("""
DELETE FROM rental_locations
WHERE name ILIKE '%east london%'
  AND country IN ('ZA', 'South Africa')
""")
q2_deleted = cur.rowcount
print(f'Query 2 (East London ZA false positives):   {q2_deleted} rows deleted')

# ── Final row count
cur.execute('SELECT COUNT(*) FROM rental_locations')
total = cur.fetchone()[0]
print(f'\nFinal rental_locations count: {total:,} rows')

# ── Rebuild canonical_locations view
print('\n=== Rebuilding canonical_locations view ===')
cur.execute('DROP VIEW IF EXISTS canonical_locations CASCADE')
print('Dropped existing view (if any).')
cur.execute("""
CREATE VIEW canonical_locations AS
SELECT DISTINCT ON (
    COALESCE(airport_code, LOWER(TRIM(name)) || COALESCE(country, ''))
)
    airport_code,
    MIN(name) OVER (
        PARTITION BY COALESCE(airport_code, LOWER(TRIM(name)) || COALESCE(country, ''))
    ) AS name,
    city,
    country,
    is_airport,
    lat,
    lng
FROM rental_locations
WHERE lat IS NOT NULL AND lng IS NOT NULL
ORDER BY
    COALESCE(airport_code, LOWER(TRIM(name)) || COALESCE(country, '')),
    is_airport DESC,
    LENGTH(name) ASC
""")
cur.execute('GRANT SELECT ON canonical_locations TO anon, authenticated')
print('View created and grants applied.')

cur.execute('SELECT COUNT(*) FROM canonical_locations')
canon_total = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM canonical_locations WHERE is_airport = true')
airports = cur.fetchone()[0]
print(f'canonical_locations: {canon_total:,} rows  ({airports:,} airports, {canon_total - airports:,} city branches)')

# Spot-check key airports
cur.execute("""
    SELECT airport_code, name, country,
           ROUND(lat::numeric, 2) AS lat, ROUND(lng::numeric, 2) AS lng
    FROM canonical_locations
    WHERE airport_code IN ('LHR','LGW','JFK','LAX','CDG','FRA','SYD','DXB','NRT','SIN','AMS','YYZ','MAD','HND','FCO','MUC')
    ORDER BY airport_code
""")
print('\nKey airport spot-check (code / name / country / lat / lng):')
for r in cur.fetchall():
    print(f'  {r[0]:5s}  {str(r[1])[:38]:38s}  {str(r[2]):22s}  {str(r[3]):7}  {r[4]}')

cur.close()
conn.close()
print('\nAll done.')
