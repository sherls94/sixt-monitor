from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import json
import asyncio
import os
from pathlib import Path
from datetime import datetime

import price_monitor

# ── Pure ASGI middleware — sits outside the FastAPI router entirely ────────────
# BaseHTTPMiddleware (@app.middleware) in Starlette 1.x doesn't intercept
# requests that are short-circuited by FastAPI's built-in OPTIONS handler.
# A raw ASGI class wrapping the whole app has no such limitation.
_CORS_HEADERS = [
    (b"access-control-allow-origin",          b"*"),
    (b"access-control-allow-methods",         b"GET, POST, PUT, DELETE, OPTIONS"),
    (b"access-control-allow-headers",         b"*"),
    (b"access-control-allow-private-network", b"true"),
    (b"access-control-max-age",               b"86400"),
]


class _PrivateNetworkMiddleware:
    """ASGI middleware that handles CORS + Chrome Private Network Access preflights."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            # Respond immediately — don't forward to the app at all.
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": _CORS_HEADERS,
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # For all other methods, inject CORS headers into the outgoing response.
        async def _send_with_cors(event):
            if event["type"] == "http.response.start":
                # Merge: keep existing headers, add ours (overwrite if key clashes)
                existing = {k: v for k, v in event.get("headers", [])}
                for k, v in _CORS_HEADERS:
                    existing[k] = v
                event = {**event, "headers": list(existing.items())}
            await send(event)

        await self._app(scope, receive, _send_with_cors)


_fastapi_app = FastAPI()


def _make_app():
    return _PrivateNetworkMiddleware(_fastapi_app)


app = _make_app()
# Route decorators must target the inner FastAPI instance
_r = _fastapi_app

RESULTS_FILE = Path(__file__).parent / "latest_results.json"
API_KEY = os.environ.get("CARDVAULT_API_KEY", "changeme")
RUNNING = False


@_r.get("/health")
async def health():
    return {"status": "ok"}


@_r.get("/results")
async def get_results():
    if not RESULTS_FILE.exists():
        return JSONResponse({"error": "No results yet — run a check first"}, status_code=404)
    return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))


@_r.post("/run")
async def trigger_run(x_api_key: str = Header(None), booking_id: str | None = None):
    global RUNNING
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if RUNNING:
        return {"status": "already_running"}
    RUNNING = True
    asyncio.create_task(_run_monitor(booking_id))
    return {"status": "started"}


@_r.post("/bookings/{booking_id}/check")
async def trigger_booking_check(booking_id: str, authorization: str = Header(None)):
    """
    User-facing equivalent of /run, scoped to one booking. Unlike /run (a
    static API key with no ownership check — unsafe to embed in a public
    mobile app, since it would let anyone trigger a check on any booking_id),
    this verifies the caller's Supabase auth token and that they own the
    booking before starting a check.
    """
    global RUNNING
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    sb = price_monitor.get_supabase()
    try:
        user_resp = sb.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = getattr(user_resp, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    booking = sb.table("bookings").select("id,user_id").eq("id", booking_id).maybe_single().execute()
    if not booking.data or booking.data.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    if RUNNING:
        return {"status": "already_running"}
    RUNNING = True
    asyncio.create_task(_run_monitor(booking_id))
    return {"status": "started"}


@_r.get("/status")
async def get_status():
    return {
        "running": RUNNING,
        "last_results_at": datetime.fromtimestamp(
            RESULTS_FILE.stat().st_mtime
        ).isoformat() if RESULTS_FILE.exists() else None,
    }


class SearchRequest(BaseModel):
    airport_code: str
    pickup_date: str    # "YYYY-MM-DD"
    pickup_time: str = "12:00"
    return_date: str    # "YYYY-MM-DD"
    return_time: str = "12:00"
    driver_age: int = 30
    acriss_code: str | None = None   # defaults to Full Size SUV (GFAR) if omitted


_search_lock = asyncio.Lock()


@_r.post("/search")
async def live_search(req: SearchRequest):
    """
    Ad-hoc multi-provider price search for arbitrary dates/location — not tied
    to a stored booking. Reuses price_monitor.py's provider-check functions,
    which read from its module-level BOOKING dict, so concurrent searches are
    serialized behind a lock to avoid two requests stomping each other's state.
    """
    async with _search_lock:
        saved_booking = dict(price_monitor.BOOKING)
        try:
            price_monitor.BOOKING.update({
                "airport_code": req.airport_code,
                "pickup_date":  req.pickup_date,
                "pickup_time":  req.pickup_time,
                "return_date":  req.return_date,
                "return_time":  req.return_time,
                "driver_age":   req.driver_age,
                "acriss_code":  req.acriss_code or "GFAR",
                "booked_price": 0,
            })
            price_monitor._reinitialize_location_constants()

            results = await asyncio.gather(
                *[price_monitor.check_provider(p) for p in price_monitor.PROVIDERS],
                return_exceptions=True,
            )
            clean = [
                r if not isinstance(r, Exception)
                else price_monitor.make_result(p, error=str(r)[:150])
                for p, r in zip(price_monitor.PROVIDERS, results)
            ]
            clean.sort(key=lambda r: (r.get("price") is None, r.get("price") or 0))
            return {"results": clean}
        finally:
            price_monitor.BOOKING.clear()
            price_monitor.BOOKING.update(saved_booking)
            price_monitor._reinitialize_location_constants()


async def _run_monitor(booking_id: str | None = None):
    global RUNNING
    try:
        cmd = ["python3", "price_monitor.py"]
        if booking_id:
            cmd += ["--booking-id", booking_id]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=Path(__file__).parent,
        )
        await proc.wait()
    finally:
        RUNNING = False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000)
