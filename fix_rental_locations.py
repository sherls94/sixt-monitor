import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

conn = psycopg2.connect(os.environ['SUPABASE_DB_URL'])
cur = conn.cursor()

cur.execute("""
    delete from rental_locations
    where name ilike '%box truck%'
    or name ilike '%exotic collection%'
    or name ilike '%exotics%'
    or name ilike '%carshare%'
    or name ilike '%zipcar%'
    or name ilike '%car2go%'
    or name ilike '%getaround%'
    or name ilike '%university%'
    or name ilike '%college%'
    or name ilike '%body shop%'
    or name ilike '%collision%'
    or name ilike '%autobody%'
    or name ilike '%dealership%'
    or name ilike '%sales only%'
    or name ilike '%info_tech%'
    or name ilike '%remote employee%'
    or name ilike '%personal property%'
    or name ilike '%2 stations%'
    or name ilike '%downtown only%'
""")
print(f"Deleted {cur.rowcount} non-rental entries")
conn.commit()
conn.close()
print("Done")
