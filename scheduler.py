"""
scheduler.py
============
Runs continuously and price-checks all active bookings on a rolling interval.
Uses supabase-py for DB queries (same credentials as price_monitor.py).

Usage:
    python scheduler.py
"""

import os
import sys
import time
import logging
import subprocess
from datetime import datetime
from pathlib import Path

# Load .env if present (before any os.environ.get calls)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass  # python-dotenv not installed — rely on env vars being set externally

from supabase import create_client

# ── Configuration ─────────────────────────────────────────────────────────────
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = os.environ.get("SUPABASE_SERVICE_KEY", "")
CHECK_INTERVAL_HOURS = 2
MIN_RECHECK_HOURS   = 1.5   # skip bookings checked more recently than this
GAP_BETWEEN_RUNS    = 30    # seconds between consecutive booking checks

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "scheduler.log"),
        logging.StreamHandler(),
    ],
)

SCRIPT_DIR = Path(__file__).parent


def get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_bookings_to_check() -> list[dict]:
    """Return active bookings that have never been checked or were checked
    more than MIN_RECHECK_HOURS ago, and whose pickup date is in the future."""
    sb = get_supabase()

    # All active future bookings
    bookings_resp = (
        sb.table("bookings")
        .select("id, airport_code, provider, pickup_date")
        .eq("status", "active")
        .gte("pickup_date", datetime.utcnow().date().isoformat())
        .execute()
    )
    all_bookings = bookings_resp.data or []

    if not all_bookings:
        return []

    # Latest checked_at per booking
    results_resp = (
        sb.table("price_results")
        .select("booking_id, checked_at")
        .execute()
    )
    latest: dict[str, str] = {}
    for row in (results_resp.data or []):
        bid = row["booking_id"]
        if bid not in latest or row["checked_at"] > latest[bid]:
            latest[bid] = row["checked_at"]

    due = []
    for b in all_bookings:
        bid = b["id"]
        last = latest.get(bid)
        if last is None:
            due.append({**b, "last_checked": None})
            continue
        # Parse ISO timestamp and compare
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        age_hours = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds() / 3600
        if age_hours >= MIN_RECHECK_HOURS:
            due.append({**b, "last_checked": last})

    return due


def run_check(booking_id: str) -> bool:
    logging.info(f"  Running price_monitor.py --booking-id {booking_id}")
    result = subprocess.run(
        [sys.executable, "price_monitor.py", "--booking-id", booking_id],
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode == 0:
        logging.info(f"  OK Booking {booking_id} completed successfully")
        return True
    else:
        logging.error(f"  FAIL Booking {booking_id} failed with exit code {result.returncode}")
        return False


def main():
    logging.info("=" * 60)
    logging.info("Scheduler started — checking every %sh (min recheck: %sh)",
                 CHECK_INTERVAL_HOURS, MIN_RECHECK_HOURS)
    logging.info("=" * 60)

    while True:
        try:
            bookings = get_bookings_to_check()
            logging.info("Found %d booking(s) due for a check", len(bookings))

            for booking in bookings:
                logging.info(
                    "  -> %s at %s  (last checked: %s)",
                    booking["provider"],
                    booking["airport_code"],
                    booking["last_checked"] or "never",
                )
                run_check(str(booking["id"]))
                if len(bookings) > 1:
                    time.sleep(GAP_BETWEEN_RUNS)

        except Exception as e:
            logging.error("Scheduler error: %s", e, exc_info=True)

        logging.info("Next check in %s hours", CHECK_INTERVAL_HOURS)
        time.sleep(CHECK_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
